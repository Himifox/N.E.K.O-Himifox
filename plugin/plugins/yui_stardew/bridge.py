from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from websockets.asyncio.client import connect as websocket_connect
from websockets.exceptions import (
    ConnectionClosed,
    InvalidHandshake,
    InvalidURI,
    PayloadTooBig,
)


PROTOCOL_VERSION = 2
GATEWAY_PATH = "/yuitoissho/agent/v2"
MAXIMUM_REQUEST_BYTES = 8 * 1024
MAXIMUM_RESPONSE_BYTES = 32 * 1024


@dataclass(frozen=True, slots=True)
class BridgeSettings:
    endpoint: str = ""
    token: str = field(default="", repr=False)
    connect_timeout_seconds: float = 3.0
    request_timeout_seconds: float = 6.0

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "BridgeSettings":
        endpoint_value = value.get("endpoint", "")
        endpoint = endpoint_value.strip() if isinstance(endpoint_value, str) else ""
        token_value = value.get("token", "")
        token = token_value if isinstance(token_value, str) else ""
        connect_timeout = _bounded_float(
            value.get("connect_timeout_seconds"), 3.0, minimum=0.2, maximum=15.0
        )
        request_timeout = _bounded_float(
            value.get("request_timeout_seconds"), 6.0, minimum=1.0, maximum=30.0
        )
        return cls(
            endpoint=endpoint,
            token=token,
            connect_timeout_seconds=connect_timeout,
            request_timeout_seconds=request_timeout,
        )

    @property
    def configured(self) -> bool:
        return bool(self.endpoint) and 16 <= len(self.token) <= 256


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
                "请启动装有新版 YuiToIssho 的 Stardew Valley；插件会自动发现本机 Agent Gateway。",
            )

        request_id = uuid.uuid4().hex
        envelope = {
            "protocol_version": PROTOCOL_VERSION,
            "request_id": request_id,
            "token": self._settings.token,
            "action": action,
            "arguments": dict(arguments or {}),
        }
        payload = json.dumps(envelope, ensure_ascii=False, separators=(",", ":"))
        if len(payload.encode("utf-8")) > MAXIMUM_REQUEST_BYTES:
            raise BridgeError("REQUEST-TOO-LARGE", "Yui 请求超过 8 KiB 限制。")

        try:
            async with websocket_connect(
                self._settings.endpoint,
                open_timeout=self._settings.connect_timeout_seconds,
                close_timeout=1.0,
                max_size=MAXIMUM_RESPONSE_BYTES,
                ping_interval=20.0,
                ping_timeout=20.0,
            ) as websocket:
                await asyncio.wait_for(
                    websocket.send(payload),
                    timeout=self._settings.connect_timeout_seconds,
                )
                raw_response = await asyncio.wait_for(
                    websocket.recv(),
                    timeout=self._settings.request_timeout_seconds,
                )
        except TimeoutError as error:
            raise BridgeError(
                "BRIDGE-TIMEOUT", "YuiToIssho Agent Gateway 响应超时。"
            ) from error
        except PayloadTooBig as error:
            raise BridgeError(
                "INVALID-RESPONSE", "YuiToIssho 响应超过 32 KiB 限制。"
            ) from error
        except (ConnectionClosed, InvalidHandshake, InvalidURI, OSError) as error:
            raise BridgeError(
                "BRIDGE-UNAVAILABLE",
                "无法连接 YuiToIssho；请确认 Stardew Valley 与 Agent Gateway 已启动。",
            ) from error

        if not isinstance(raw_response, str):
            raise BridgeError(
                "INVALID-RESPONSE", "YuiToIssho 返回了不受支持的二进制消息。"
            )
        if len(raw_response.encode("utf-8")) > MAXIMUM_RESPONSE_BYTES:
            raise BridgeError("INVALID-RESPONSE", "YuiToIssho 响应超过 32 KiB 限制。")
        try:
            response = json.loads(raw_response)
        except json.JSONDecodeError as error:
            raise BridgeError(
                "INVALID-RESPONSE", "YuiToIssho 返回了无效 JSON。"
            ) from error
        if not isinstance(response, dict):
            raise BridgeError("INVALID-RESPONSE", "YuiToIssho 响应不是 JSON 对象。")
        if (
            response.get("protocol_version") != PROTOCOL_VERSION
            or response.get("request_id") != request_id
        ):
            raise BridgeError("RESPONSE-MISMATCH", "YuiToIssho 响应版本或请求 ID 不匹配。")
        if not isinstance(response.get("ok"), bool) or not isinstance(
            response.get("code"), str
        ):
            raise BridgeError("INVALID-RESPONSE", "YuiToIssho 响应缺少结构化结果。")
        state = response.get("state")
        if state is not None and not isinstance(state, dict):
            raise BridgeError("INVALID-RESPONSE", "YuiToIssho 状态快照格式无效。")
        return response


def _bounded_float(
    value: Any, default: float, *, minimum: float, maximum: float
) -> float:
    if isinstance(value, bool):
        return default
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if minimum <= parsed <= maximum else default
