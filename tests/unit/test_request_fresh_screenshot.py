"""Regression tests for LLMSessionManager.request_fresh_screenshot.

回归保护：前端有的截图路径（Electron 主进程直捕 captureSourceAsDataUrl）返回
原生分辨率大图，base64 可达 ~1.4MB。若 WebSocket 分支原样返回，Phase 2 直接把
它发给 vision LLM 会被代理 nginx 以 413 Request Entity Too Large 拒掉。
request_fresh_screenshot 的两条返回路径都必须统一压到 720p/JPEG-80。
"""
import asyncio
import base64
from io import BytesIO
from types import SimpleNamespace

import pytest
from PIL import Image

from main_logic.core import LLMSessionManager


def _png_b64(width: int, height: int) -> str:
    """生成一张指定尺寸的 PNG 并返回纯 base64（不含 data: 前缀）。"""
    img = Image.new("RGB", (width, height))
    # 画一点渐变，让它像真实截图而不是纯色（纯色 JPEG 会压到几乎为 0）
    px = img.load()
    for y in range(0, height, 4):
        for x in range(0, width, 4):
            px[x, y] = ((x * 255) // width, (y * 255) // height, 128)
    buf = BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("utf-8")


@pytest.mark.unit
def test_ws_screenshot_is_downscaled_to_720p():
    """WebSocket 分支返回的原生分辨率大图必须被压到 720p JPEG。"""
    big_b64 = _png_b64(2560, 1440)  # 模拟 Electron 主进程直捕的原生 1440p 大图

    async def _run() -> str:
        fake = SimpleNamespace(_screenshot_future=None, lanlan_name="桃奈")

        async def _send_json(payload):
            # send_json 在 create_future 之后、wait_for 之前被 await，
            # 此处直接 resolve future，模拟前端回传截图。
            assert payload == {"type": "request_screenshot"}
            LLMSessionManager.resolve_screenshot_request(fake, big_b64)

        fake.websocket = SimpleNamespace(send_json=_send_json)
        return (await LLMSessionManager.request_fresh_screenshot(fake, timeout=3.0)).b64

    out_b64 = asyncio.run(_run())

    assert out_b64, "应返回非空 base64"
    out_img = Image.open(BytesIO(base64.b64decode(out_b64)))
    # 必须已被归一化为 720p JPEG，而不是原样 1440p PNG
    assert out_img.format == "JPEG"
    assert out_img.height == 720
    # 字节数应远小于 nginx 默认 1MB body 限制（这正是 413 的触发线）
    assert len(base64.b64decode(out_b64)) < 1024 * 1024


@pytest.mark.unit
def test_ws_screenshot_already_small_stays_under_limit():
    """已经是小图（720p 以内）时也应正常返回，且不会被放大。"""
    small_b64 = _png_b64(1280, 720)

    async def _run() -> str:
        fake = SimpleNamespace(_screenshot_future=None, lanlan_name="桃奈")

        async def _send_json(payload):
            LLMSessionManager.resolve_screenshot_request(fake, small_b64)

        fake.websocket = SimpleNamespace(send_json=_send_json)
        return (await LLMSessionManager.request_fresh_screenshot(fake, timeout=3.0)).b64

    out_b64 = asyncio.run(_run())

    assert out_b64
    out_img = Image.open(BytesIO(base64.b64decode(out_b64)))
    assert out_img.format == "JPEG"
    assert out_img.height <= 720
    assert len(base64.b64decode(out_b64)) < 1024 * 1024


@pytest.mark.unit
@pytest.mark.parametrize(
    ('reply_kind', 'allow_fallback', 'local', 'expected_capture', 'expected_declined'),
    [
        ('declined', True, True, False, True),
        ('empty', True, True, True, False),
        ('timeout', True, True, True, False),
        ('empty', False, True, False, True),
        ('timeout', False, True, False, True),
        ('empty', True, False, False, False),
    ],
)
def test_declines_and_window_bounds_preserve_only_legal_fallback(
    monkeypatch, reply_kind, allow_fallback, local, expected_capture, expected_declined,
):
    from unittest.mock import AsyncMock, Mock
    from main_logic import core as core_facade
    from main_logic.core import proactive

    read = AsyncMock(return_value={'proactiveVisionEnabled': True})
    capture = Mock(return_value=Image.new('RGB', (320, 180), (20, 80, 180)))
    monkeypatch.setattr(core_facade, 'aload_global_conversation_settings', read)
    monkeypatch.setattr(proactive, 'capture_desktop_screenshot', capture)

    async def run():
        fake = SimpleNamespace(_screenshot_future=None, lanlan_name='Repro')

        async def send(_payload):
            if reply_kind != 'timeout':
                # Even a decline carrying nonempty data must not revive that image.
                LLMSessionManager.resolve_screenshot_request(
                    fake, 'IGNORE_THIS_IMAGE' if reply_kind == 'declined' else '',
                    declined=reply_kind == 'declined',
                )

        fake.websocket = SimpleNamespace(
            send_json=send,
            client=SimpleNamespace(host='127.0.0.1' if local else '192.0.2.1'),
        )
        result = await LLMSessionManager.request_fresh_screenshot(
            fake, timeout=0.01, allow_backend_fallback=allow_fallback,
        )
        assert fake._screenshot_future is None
        return result

    fresh = asyncio.run(run())
    assert fresh.declined is expected_declined
    assert capture.call_count == int(expected_capture)
    assert bool(fresh.b64) is expected_capture
    assert all(call.kwargs == {'strict': True} for call in read.await_args_list)
    if expected_capture:
        assert fresh.source == 'backend_fallback'
        assert Image.open(BytesIO(base64.b64decode(fresh.b64))).format == 'JPEG'


@pytest.mark.unit
@pytest.mark.parametrize('when', ['before', 'frontend_image', 'frontend_empty', 'backend_capture', 'backend_capture_error', 'read_error'])
def test_privacy_changes_and_read_errors_do_not_return_images(monkeypatch, when):
    from unittest.mock import Mock
    from main_logic import core as core_facade
    from main_logic.core import proactive

    allowed = when != 'before'

    async def read(*, strict=False):
        assert strict is True
        if when == 'read_error':
            raise OSError('synthetic preference read error')
        return {'proactiveVisionEnabled': allowed}

    def capture():
        nonlocal allowed
        if when in ('backend_capture', 'backend_capture_error'):
            allowed = False
        if when == 'backend_capture_error':
            raise OSError('synthetic capture error after privacy changed')
        return Image.new('RGB', (320, 180), (180, 80, 20))

    capture_mock = Mock(side_effect=capture)
    monkeypatch.setattr(core_facade, 'aload_global_conversation_settings', read)
    monkeypatch.setattr(proactive, 'capture_desktop_screenshot', capture_mock)
    sent = []

    async def run():
        fake = SimpleNamespace(_screenshot_future=None, lanlan_name='Repro')

        async def send(payload):
            nonlocal allowed
            sent.append(payload)
            if when in ('frontend_image', 'frontend_empty'):
                allowed = False
            LLMSessionManager.resolve_screenshot_request(
                fake, _png_b64(320, 180) if when == 'frontend_image' else '',
            )

        fake.websocket = SimpleNamespace(send_json=send, client=SimpleNamespace(host='127.0.0.1'))
        return await LLMSessionManager.request_fresh_screenshot(fake, timeout=0.01)

    fresh = asyncio.run(run())
    assert fresh.declined is True
    assert fresh.b64 == ''
    assert capture_mock.call_count == int(when in ('backend_capture', 'backend_capture_error'))
    assert len(sent) == int(when not in ('before', 'read_error'))


@pytest.mark.unit
@pytest.mark.parametrize('value,expected', [(True, True), (False, False), (None, False), ('true', False)])
def test_desktop_fallback_command_requires_a_boolean(value, expected):
    from main_logic.proactive_chat.contracts import ProactiveChatCommand

    assert ProactiveChatCommand.from_payload({}).screenshot_allow_desktop_fallback is True
    command = ProactiveChatCommand.from_payload({'screenshot_allow_desktop_fallback': value})
    assert command.screenshot_allow_desktop_fallback is expected
