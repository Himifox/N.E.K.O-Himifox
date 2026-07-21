import asyncio
import inspect
from pathlib import Path
from unittest.mock import AsyncMock

from main_logic.proactive_chat import break_reminders
from main_routers.system_router import break_reminders as compat_break_reminders

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DOMAIN_PATH = PROJECT_ROOT / "main_logic" / "proactive_chat" / "break_reminders.py"
COMPAT_PATH = PROJECT_ROOT / "main_routers" / "system_router" / "break_reminders.py"


def test_break_reminder_delivery_requires_explicit_config_manager():
    parameter = inspect.signature(
        break_reminders._deliver_break_reminder_via_llm
    ).parameters["config_manager"]

    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
    assert parameter.default is inspect.Parameter.empty


def test_break_reminder_domain_has_no_router_or_shared_state_dependency():
    source = DOMAIN_PATH.read_text(encoding="utf-8")

    assert "main_routers" not in source
    assert "fastapi" not in source.lower()
    assert "shared_state" not in source
    assert "get_config_manager" not in source
    assert 'get_module_logger(__name__, "Main")' in source


def test_router_break_reminders_preserves_legacy_signature_and_delegates(monkeypatch):
    source = COMPAT_PATH.read_text(encoding="utf-8")
    config_manager = object()
    deliver = AsyncMock(return_value=("delivered", "sid"))
    monkeypatch.setattr(
        compat_break_reminders,
        "get_config_manager",
        lambda: config_manager,
    )
    monkeypatch.setattr(
        compat_break_reminders,
        "_deliver_break_reminder_via_llm_domain",
        deliver,
    )

    assert "from main_logic.proactive_chat.break_reminders import" in source
    assert "def _render" not in source
    assert "._shared" not in source
    assert (
        "config_manager"
        not in inspect.signature(
            compat_break_reminders._deliver_break_reminder_via_llm
        ).parameters
    )

    result = asyncio.run(
        compat_break_reminders._deliver_break_reminder_via_llm(
            lanlan_name="test-character",
            mgr="manager",
            system_prompt="prompt",
            channel="work_break",
            lang="en",
            timeout_seconds=12.0,
        )
    )

    assert result == ("delivered", "sid")
    assert deliver.await_args.kwargs == {
        "lanlan_name": "test-character",
        "mgr": "manager",
        "config_manager": config_manager,
        "system_prompt": "prompt",
        "channel": "work_break",
        "lang": "en",
        "timeout_seconds": 12.0,
    }


def test_misconfigured_explicit_config_preserves_skip_contract():
    class ConfigManager:
        def get_model_api_config(self, model_type):
            assert model_type == "correction"
            return {"model": "test-model", "api_key": ""}

    result = asyncio.run(
        break_reminders._deliver_break_reminder_via_llm(
            lanlan_name="test-character",
            mgr=object(),
            config_manager=ConfigManager(),
            system_prompt="prompt",
            channel="work_break",
            lang="en",
        )
    )

    assert result == (None, None)
