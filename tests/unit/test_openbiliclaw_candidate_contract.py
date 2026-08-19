from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from main_logic.proactive_chat import candidate_selection, generation
from main_logic.proactive_chat.openbiliclaw_candidate import (
    format_phase1_candidate,
    format_phase2_candidate,
    is_proactive_candidate_allowed,
    openbiliclaw_link,
    project_openbiliclaw_candidate,
    recent_user_context,
)


def _core_candidate(
    *,
    title: str = "Agent context optimization",
    sensitivity: str = "none",
    proactive_policy: str = "allow",
    why_now_source: str = "aggregated_interest",
    delivery_ref: object | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        tracking=SimpleNamespace(
            candidate_id="obc:11223344",
            item_key="bilibili:BV1TEST",
            url="https://www.bilibili.com/video/BV1TEST",
            expires_at=None,
            delivery_ref=delivery_ref or object(),
        ),
        semantics=SimpleNamespace(
            title=title,
            topic="AI coding",
            summary="Use structured state to reduce long history prompts",
            reason_codes=("recent_interest", "source_affinity"),
            source_platform="bilibili",
            author_name="Creator",
            content_type="video",
            confidence=0.86,
            freshness="recent",
        ),
        policy=SimpleNamespace(
            sensitivity=sensitivity,
            proactive_policy=proactive_policy,
            why_now_source=why_now_source,
        ),
    )


def test_phase1_and_phase2_are_disjoint_bounded_projections() -> None:
    envelope = project_openbiliclaw_candidate(_core_candidate(), language="en")

    phase1_text = format_phase1_candidate(2, envelope)
    assert phase1_text.startswith("2. ")
    phase1 = json.loads(phase1_text.removeprefix("2. "))
    assert set(phase1) == {
        "title",
        "topic",
        "summary",
        "why_now",
        "reason_codes",
        "source_platform",
        "author_name",
        "content_type",
        "confidence",
        "freshness",
    }

    phase2_text = format_phase2_candidate(envelope)
    phase2 = json.loads(phase2_text.splitlines()[0])
    assert set(phase2) == {"title", "topic", "summary", "why_now"}
    assert "do not" not in phase2_text.lower()

    forbidden = {
        "candidate_id",
        "item_key",
        "delivery_ref",
        "expression",
        "content_url",
        "BV1TEST",
    }
    assert all(value not in phase1_text for value in forbidden)
    assert all(value not in phase2_text for value in forbidden)


def test_phase1_formatter_dispatch_does_not_render_public_link_or_tracking() -> None:
    envelope = project_openbiliclaw_candidate(_core_candidate(), language="zh")
    rendered = candidate_selection._format_phase1_link_candidate(
        1,
        openbiliclaw_link(envelope),
    )

    assert "https://" not in rendered
    assert "obc:" not in rendered
    assert "bilibili:BV" not in rendered
    assert "why_now" in rendered


def test_sequence_number_is_exact_and_duplicate_title_fallback_passes() -> None:
    first = openbiliclaw_link(
        project_openbiliclaw_candidate(
            _core_candidate(title="Same title"),
            language="en",
        )
    )
    second = openbiliclaw_link(
        project_openbiliclaw_candidate(
            _core_candidate(title="Same title"),
            language="en",
        )
    )

    assert generation._lookup_phase1_link(
        {"number": "2", "title": "Same title"},
        [first, second],
    ) is second
    assert generation._lookup_phase1_link(
        {"number": "9", "title": "Same title"},
        [first, second],
    ) is None
    assert generation._lookup_phase1_link(
        {"title": "Same title"},
        [first, second],
    ) is None


def test_sensitive_candidate_gate_fails_closed_before_phase1() -> None:
    inferred = _core_candidate(
        sensitivity="health",
        proactive_policy="allow",
        why_now_source="aggregated_interest",
    )
    denied = _core_candidate(
        sensitivity="finance",
        proactive_policy="deny",
        why_now_source="current_conversation",
    )
    context_allowed = _core_candidate(
        sensitivity="health",
        proactive_policy="explicit_context_only",
        why_now_source="current_conversation",
    )
    subscription_allowed = _core_candidate(
        sensitivity="politics",
        proactive_policy="explicit_context_or_subscription",
        why_now_source="explicit_subscription",
    )

    assert not is_proactive_candidate_allowed(inferred)
    assert not is_proactive_candidate_allowed(denied)
    assert is_proactive_candidate_allowed(context_allowed)
    assert is_proactive_candidate_allowed(subscription_allowed)


def test_recent_user_context_reads_only_three_in_memory_user_messages() -> None:
    session = SimpleNamespace(
        _conversation_history=[
            SimpleNamespace(type="human", content="one"),
            SimpleNamespace(type="ai", content="reply"),
            SimpleNamespace(type="human", content="two"),
            SimpleNamespace(type="human", content="three"),
            SimpleNamespace(type="human", content="four"),
        ]
    )

    assert recent_user_context(session) == ("two", "three", "four")


def test_openbiliclaw_selected_candidate_bypasses_generic_bilibili_formatter() -> None:
    service_source = (
        Path(__file__).resolve().parents[2]
        / "main_logic"
        / "proactive_chat"
        / "service.py"
    ).read_text(encoding="utf-8")

    assert 'envelope = selected_web_link.get("_openbiliclaw_candidate")' in service_source
    assert "web_topic = format_phase2_candidate(" in service_source
    assert "core.chat(" not in service_source
