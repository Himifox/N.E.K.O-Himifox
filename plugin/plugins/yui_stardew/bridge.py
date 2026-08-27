from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


PROTOCOL_VERSION = 1
DEFAULT_PORT = 48910
MAXIMUM_REQUEST_BYTES = 8 * 1024
MAXIMUM_RESPONSE_BYTES = 32 * 1024


@dataclass(frozen=True, slots=True)
class BridgeSettings:
    port: int = DEFAULT_PORT
    token: str = ""
    connect_timeout_seconds: float = 3.0
    request_timeout_seconds: float = 6.0

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "BridgeSettings":
        port = _bounded_int(value.get("port"), DEFAULT_PORT, minimum=1024, maximum=65535)
        token_value = value.get("token", "")
        token = token_value if isinstance(token_value, str) else ""
        connect_timeout = _bounded_float(
            value.get("connect_timeout_seconds"), 3.0, minimum=0.2, maximum=15.0
        )
        request_timeout = _bounded_float(
            value.get("request_timeout_seconds"), 6.0, minimum=1.0, maximum=30.0
        )
        return cls(
            port=port,
            token=token,
            connect_timeout_seconds=connect_timeout,
            request_timeout_seconds=request_timeout,
        )

    @property
    def configured(self) -> bool:
        return 16 <= len(self.token) <= 256


class BridgeError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class YuiStardewBridgeClient:
    def __init__(self, settings: BridgeSettings) -> None:
        self._settings = settings

    @property
    def configured(self) -> bool:
        return self._settings.configured

    async def request(
        self,
        action: str,
        arguments: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        if not self.configured:
            raise BridgeError(
                "BRIDGE-NOT-CONFIGURED",
                "请先为 NEKO 与 HimiYui 配置同一个 16～256 字符本机密钥。",
            )

        request_id = uuid.uuid4().hex
        envelope = {
            "protocol_version": PROTOCOL_VERSION,
            "request_id": request_id,
            "token": self._settings.token,
            "action": action,
            "arguments": dict(arguments or {}),
        }
        payload = json.dumps(envelope, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
        if len(payload) > MAXIMUM_REQUEST_BYTES:
            raise BridgeError("REQUEST-TOO-LARGE", "Yui 请求超过 8 KiB 限制。")

        writer: asyncio.StreamWriter | None = None
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection("127.0.0.1", self._settings.port, limit=MAXIMUM_RESPONSE_BYTES + 1),
                timeout=self._settings.connect_timeout_seconds,
            )
            writer.write(payload)
            await asyncio.wait_for(writer.drain(), timeout=self._settings.connect_timeout_seconds)
            response_line = await asyncio.wait_for(
                reader.readline(), timeout=self._settings.request_timeout_seconds
            )
        except TimeoutError as error:
            raise BridgeError("BRIDGE-TIMEOUT", "HimiYui 本机桥响应超时。") from error
        except (ConnectionError, OSError) as error:
            raise BridgeError(
                "BRIDGE-UNAVAILABLE",
                "无法连接 HimiYui；请确认 Stardew Valley、模组桥和端口已经启动。",
            ) from error
        finally:
            if writer is not None:
                writer.close()
                try:
                    await writer.wait_closed()
                except (ConnectionError, OSError):
                    pass

        if not response_line:
            raise BridgeError("EMPTY-RESPONSE", "HimiYui 本机桥关闭了空响应连接。")
        if len(response_line) > MAXIMUM_RESPONSE_BYTES or not response_line.endswith(b"\n"):
            raise BridgeError("INVALID-RESPONSE", "HimiYui 响应不完整或超过 32 KiB 限制。")
        try:
            response = json.loads(response_line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise BridgeError("INVALID-RESPONSE", "HimiYui 返回了无效 JSON。") from error
        if not isinstance(response, dict):
            raise BridgeError("INVALID-RESPONSE", "HimiYui 响应不是 JSON 对象。")
        if response.get("protocol_version") != PROTOCOL_VERSION or response.get("request_id") != request_id:
            raise BridgeError("RESPONSE-MISMATCH", "HimiYui 响应版本或请求 ID 不匹配。")
        if not isinstance(response.get("ok"), bool) or not isinstance(response.get("code"), str):
            raise BridgeError("INVALID-RESPONSE", "HimiYui 响应缺少结构化结果。")
        state = response.get("state")
        if state is not None and not isinstance(state, dict):
            raise BridgeError("INVALID-RESPONSE", "HimiYui 状态快照格式无效。")
        return response


def _bounded_int(value: Any, default: int, *, minimum: int, maximum: int) -> int:
    if isinstance(value, bool):
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if minimum <= parsed <= maximum else default


def _bounded_float(value: Any, default: float, *, minimum: float, maximum: float) -> float:
    if isinstance(value, bool):
        return default
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if minimum <= parsed <= maximum else default
