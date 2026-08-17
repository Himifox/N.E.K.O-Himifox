# Copyright 2025-2026 Project N.E.K.O. Team
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Built-in OpenBiliClaw Core and browser-extension bridge lifecycle.

This module deliberately lives in the first-party application runtime. It does
not register a N.E.K.O plugin or an MCP channel. The browser extension keeps its
stable loopback contract while the Core and HTTP/WebSocket adapter are owned by
the N.E.K.O main-server process.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Callable

from utils.logger_config import get_module_logger

logger = get_module_logger(__name__, "Main")

DEFAULT_OPENBILICLAW_PORT = 8420
_STARTUP_TIMEOUT_SECONDS = 15.0
_SHUTDOWN_TIMEOUT_SECONDS = 15.0


def _enabled_from_environment() -> bool:
    value = os.environ.get("NEKO_OPENBILICLAW_ENABLED", "1").strip().lower()
    return value not in {"0", "false", "no", "off"}


def _port_from_environment() -> int:
    raw = os.environ.get("NEKO_OPENBILICLAW_PORT", "").strip()
    if not raw:
        return DEFAULT_OPENBILICLAW_PORT
    try:
        port = int(raw)
    except ValueError:
        logger.warning("Ignoring invalid NEKO_OPENBILICLAW_PORT=%r", raw)
        return DEFAULT_OPENBILICLAW_PORT
    if 1 <= port <= 65535:
        return port
    logger.warning("Ignoring out-of-range NEKO_OPENBILICLAW_PORT=%r", raw)
    return DEFAULT_OPENBILICLAW_PORT


@dataclass(frozen=True)
class OpenBiliClawStatus:
    """Serializable state exposed to N.E.K.O's first-party status API."""

    enabled: bool
    state: str
    endpoint: str
    data_dir: str
    core_available: bool = False
    bridge_running: bool = False
    degraded: bool = False
    error: str = ""

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


BackendLoader = Callable[[Path, int], tuple[Any, Any]]
ServerFactory = Callable[[Any, str, int], Any]


def _openbiliclaw_provider_type(value: object) -> str:
    """Map N.E.K.O's provider vocabulary onto OpenBiliClaw adapters."""
    provider = str(value or "").strip().lower()
    aliases = {
        "anthropic": "claude",
        "google": "gemini",
    }
    provider = aliases.get(provider, provider)
    if provider in {
        "claude",
        "deepseek",
        "gemini",
        "ollama",
        "openai",
        "openai_compatible",
        "openrouter",
        "orcarouter",
    }:
        return provider
    # Qwen/free/custom endpoints exposed by N.E.K.O use the OpenAI chat wire.
    return "openai_compatible"


def _apply_neko_model_config(config: Any) -> Any:
    """Project N.E.K.O's conversation model into Core without copying secrets."""
    try:
        from utils.config_manager import get_config_manager

        model_config = get_config_manager().get_model_api_config("conversation") or {}
    except Exception:
        logger.debug("Could not resolve N.E.K.O model config for OpenBiliClaw", exc_info=True)
        return config

    model = str(model_config.get("model") or "").strip()
    base_url = str(model_config.get("base_url") or "").strip()
    if not model or not base_url:
        return config

    from openbiliclaw.config import LLMInstanceConfig

    instance = LLMInstanceConfig(
        name="N.E.K.O conversation model",
        provider_type=_openbiliclaw_provider_type(model_config.get("provider_type")),
        api_key=str(model_config.get("api_key") or "").strip(),
        model=model,
        base_url=base_url,
        enabled=True,
    )
    llm = replace(
        config.llm,
        instance_routing=True,
        instances={"neko-conversation": instance},
        default_chain=["neko-conversation"],
        fallback_provider="",
    )
    return replace(config, llm=llm)


def _load_openbiliclaw_backend(data_root: Path, port: int) -> tuple[Any, Any]:
    """Build one Core and its existing FastAPI compatibility adapter."""
    # OpenBiliClaw uses this root for config writes performed by its API. The
    # value is process-wide by design: N.E.K.O owns the sole embedded Core.
    os.environ["OPENBILICLAW_PROJECT_ROOT"] = str(data_root)

    from openbiliclaw import OpenBiliClawCore
    from openbiliclaw.api.app import create_app
    from openbiliclaw.config import load_config

    data_root.mkdir(parents=True, exist_ok=True)
    data_dir = data_root / "data"
    config = load_config(data_root / "config.toml", consult_environment=True)
    config = replace(
        config,
        data_dir=str(data_dir),
        api=replace(config.api, host="127.0.0.1", port=port),
    )
    config = _apply_neko_model_config(config)
    core = OpenBiliClawCore.create(config, allow_degraded=True)
    return core, create_app(core=core)


def _create_uvicorn_server(app: Any, host: str, port: int) -> Any:
    import uvicorn

    config = uvicorn.Config(
        app=app,
        host=host,
        port=port,
        log_level="error",
        access_log=False,
    )
    server = uvicorn.Server(config)
    # N.E.K.O owns process signals and coordinates shutdown itself.
    server.install_signal_handlers = lambda: None
    return server


class NekoOpenBiliClawRuntime:
    """Own a single embedded Core and loopback extension bridge."""

    def __init__(
        self,
        data_root: Path,
        *,
        enabled: bool | None = None,
        port: int | None = None,
        backend_loader: BackendLoader = _load_openbiliclaw_backend,
        server_factory: ServerFactory = _create_uvicorn_server,
    ) -> None:
        self.data_root = Path(data_root)
        self.enabled = _enabled_from_environment() if enabled is None else enabled
        self.port = _port_from_environment() if port is None else port
        self._backend_loader = backend_loader
        self._server_factory = server_factory
        self._lock = asyncio.Lock()
        self._core: Any | None = None
        self._server: Any | None = None
        self._server_task: asyncio.Task[None] | None = None
        self._status = self._new_status("stopped" if self.enabled else "disabled")

    def _new_status(self, state: str, **changes: object) -> OpenBiliClawStatus:
        values: dict[str, object] = {
            "enabled": self.enabled,
            "state": state,
            "endpoint": f"http://127.0.0.1:{self.port}",
            "data_dir": str(self.data_root),
        }
        values.update(changes)
        return OpenBiliClawStatus(**values)  # type: ignore[arg-type]

    @property
    def status(self) -> OpenBiliClawStatus:
        return self._status

    @property
    def core(self) -> Any | None:
        """Compatibility escape hatch for typed first-party facades."""
        return self._core

    async def start(self) -> OpenBiliClawStatus:
        """Start once; failures degrade this integration, never N.E.K.O itself."""
        async with self._lock:
            if not self.enabled:
                self._status = self._new_status("disabled")
                return self._status
            if self._server_task is not None and not self._server_task.done():
                return self._status

            self._status = self._new_status("starting")
            try:
                self._core, bridge_app = await asyncio.to_thread(
                    self._backend_loader,
                    self.data_root,
                    self.port,
                )
                self._server = self._server_factory(
                    bridge_app,
                    "127.0.0.1",
                    self.port,
                )
                self._server_task = asyncio.create_task(
                    self._server.serve(),
                    name="neko-openbiliclaw-bridge",
                )
                await self._wait_until_started()
            except Exception as exc:
                logger.exception("OpenBiliClaw built-in runtime failed to start")
                await self._cleanup_failed_start()
                self._status = self._new_status(
                    "unavailable",
                    core_available=self._core is not None,
                    error=str(exc),
                )
                return self._status

            degraded = bool(getattr(self._core, "degraded", False))
            self._status = self._new_status(
                "degraded" if degraded else "running",
                core_available=True,
                bridge_running=True,
                degraded=degraded,
            )
            logger.info(
                "OpenBiliClaw built-in bridge is ready at %s",
                self._status.endpoint,
            )
            return self._status

    async def _wait_until_started(self) -> None:
        if self._server is None or self._server_task is None:
            raise RuntimeError("OpenBiliClaw bridge server was not created")

        async def _poll() -> None:
            while not bool(getattr(self._server, "started", False)):
                if self._server_task is not None and self._server_task.done():
                    exception = self._server_task.exception()
                    if exception is not None:
                        raise exception
                    raise RuntimeError(
                        f"OpenBiliClaw bridge could not bind 127.0.0.1:{self.port}"
                    )
                await asyncio.sleep(0.02)

        await asyncio.wait_for(_poll(), timeout=_STARTUP_TIMEOUT_SECONDS)

    async def _cleanup_failed_start(self) -> None:
        task = self._server_task
        server = self._server
        if server is not None:
            server.should_exit = True
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        core = self._core
        if core is not None:
            stop = getattr(core, "stop", None)
            if callable(stop):
                try:
                    await stop()
                except Exception:
                    logger.debug("Failed to stop partially started OpenBiliClaw Core", exc_info=True)
        self._server_task = None
        self._server = None
        self._core = None

    async def stop(self) -> None:
        """Stop the bridge and let its ASGI shutdown close the embedded Core."""
        async with self._lock:
            task = self._server_task
            server = self._server
            core = self._core
            if task is None and core is None:
                self._status = self._new_status("stopped" if self.enabled else "disabled")
                return

            self._status = replace(self._status, state="stopping", bridge_running=False)
            if server is not None:
                server.should_exit = True
            if task is not None and not task.done():
                try:
                    await asyncio.wait_for(task, timeout=_SHUTDOWN_TIMEOUT_SECONDS)
                except TimeoutError:
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
                    logger.warning("Timed out stopping OpenBiliClaw bridge; task was cancelled")
            elif task is None and core is not None:
                # Construction may have completed before the ASGI server task was
                # installed. In that narrow case there is no lifespan to close it.
                stop = getattr(core, "stop", None)
                if callable(stop):
                    await stop()

            self._server_task = None
            self._server = None
            self._core = None
            self._status = self._new_status("stopped")

    async def get_profile(self) -> Any:
        return await self._require_core().get_profile()

    async def recommend(self, *, limit: int = 5) -> list[Any]:
        return list(await self._require_core().recommend(limit=limit))

    async def publish_behavior_event(self, event: dict[str, object]) -> None:
        await self._require_core().publish_event(event)

    def _require_core(self) -> Any:
        if self._core is None or not self._status.bridge_running:
            raise RuntimeError("OpenBiliClaw Core is not running")
        return self._core


_runtime: NekoOpenBiliClawRuntime | None = None


def get_openbiliclaw_runtime(config_manager: Any | None = None) -> NekoOpenBiliClawRuntime:
    """Return the process singleton, binding it to N.E.K.O's selected data root."""
    global _runtime
    if _runtime is None:
        if config_manager is None:
            from utils.config_manager import get_config_manager

            config_manager = get_config_manager()
        root = Path(config_manager.app_docs_dir) / "integrations" / "openbiliclaw"
        _runtime = NekoOpenBiliClawRuntime(root)
    return _runtime


async def start_openbiliclaw_runtime(config_manager: Any) -> OpenBiliClawStatus:
    return await get_openbiliclaw_runtime(config_manager).start()


async def stop_openbiliclaw_runtime() -> None:
    if _runtime is not None:
        await _runtime.stop()


__all__ = [
    "DEFAULT_OPENBILICLAW_PORT",
    "NekoOpenBiliClawRuntime",
    "OpenBiliClawStatus",
    "get_openbiliclaw_runtime",
    "start_openbiliclaw_runtime",
    "stop_openbiliclaw_runtime",
]
