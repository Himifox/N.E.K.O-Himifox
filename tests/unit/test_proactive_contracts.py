# -*- coding: utf-8 -*-

import pytest

from main_logic.proactive_chat import contracts


@pytest.mark.parametrize(
    ("reason_code", "expected_stage"),
    tuple(contracts._PROACTIVE_REASON_STAGE.items()),
)
def test_every_registered_reason_maps_to_its_contract_stage(
    reason_code: str,
    expected_stage: str,
) -> None:
    assert contracts._proactive_stage_for_reason(reason_code) == expected_stage


def test_every_declared_reason_is_registered() -> None:
    declared_reasons = {
        value
        for name, value in vars(contracts).items()
        if name.startswith("PROACTIVE_REASON_") and isinstance(value, str)
    }

    assert set(contracts._PROACTIVE_REASON_STAGE) == declared_reasons


def test_unknown_reason_maps_to_unknown_stage() -> None:
    assert contracts._proactive_stage_for_reason(None) == contracts.PROACTIVE_STAGE_UNKNOWN
    assert (
        contracts._proactive_stage_for_reason("NOT_A_REASON")
        == contracts.PROACTIVE_STAGE_UNKNOWN
    )


def test_body_builders_preserve_explicit_contract_fields() -> None:
    body = contracts._proactive_pass_body(
        contracts.PROACTIVE_REASON_PASS_SOURCE_EMPTY,
        success=False,
        stage="custom-stage",
        message="none",
    )

    assert body == {
        "success": False,
        "reason_code": contracts.PROACTIVE_REASON_PASS_SOURCE_EMPTY,
        "action": "pass",
        "stage": "custom-stage",
        "message": "none",
    }


@pytest.mark.parametrize(
    ("body", "expected_reason", "expected_stage"),
    (
        (
            {"action": "chat"},
            contracts.PROACTIVE_REASON_CHAT_DELIVERED,
            contracts.PROACTIVE_STAGE_DELIVERY,
        ),
        (
            {"action": "pass"},
            contracts.PROACTIVE_REASON_PASS_UNSPECIFIED,
            contracts.PROACTIVE_STAGE_UNKNOWN,
        ),
        (
            {"success": False},
            contracts.PROACTIVE_REASON_ERROR_INTERNAL,
            contracts.PROACTIVE_STAGE_RUNTIME_ERROR,
        ),
    ),
)
def test_ensure_reason_code_preserves_legacy_defaults(
    body: dict,
    expected_reason: str,
    expected_stage: str,
) -> None:
    result = contracts._ensure_proactive_reason_code(body)

    assert result is body
    assert result["reason_code"] == expected_reason
    assert result["stage"] == expected_stage
