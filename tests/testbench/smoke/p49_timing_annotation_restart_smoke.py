"""P44-F2-R0 blind manifest and timing-label readiness smoke."""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tests.testbench.pipeline.recommendation_timing_annotation import (
    build_timing_annotation_manifest,
    timing_annotation_readiness,
    validate_timing_annotation_manifest,
)
from tests.testbench.pipeline.recommendation_timing_annotation_draft import (
    _candidate_relevance,
    _interruptibility,
    build_timing_annotation_assistant_draft,
)


STAMP = "2026-07-21T12:00:00+08:00"
SHA = "a" * 64


def _freeze() -> dict:
    observations, feedback = [], []
    # Four balanced delivered/pass × human-label cells prove the final gate can
    # pass, while no outcome fields enter the reviewer manifest.
    for delivered in (True, False):
        for label in (True, False):
            for index in range(12):
                turn_id = f"{int(delivered)}-{int(label)}-{index}"
                observations.append({
                    "turn_id": turn_id,
                    "delivered": delivered,
                    "actual_reason_code": "CHAT_DELIVERED" if delivered else "PASS_DUPLICATE",
                    "review_context": {
                        "activity_state": "idle",
                        "redaction_notes": [],
                        "candidate_labels": [{
                            "id": f"news:{turn_id}", "source_type": "news",
                            "safe_title": "safe title", "safe_summary": "safe summary",
                            "score": 0.99,
                        }],
                        "delivered_excerpt": "must never leak",
                    },
                })
                if delivered and index < 6:
                    feedback.append({"turn_id": turn_id, "event_type": "user_reply"})
    # Technical failures are structurally reviewable but excluded from all F2 denominators.
    observations.extend([
        {
            "turn_id": "technical-delivery", "delivered": False,
            "actual_reason_code": "DELIVERY_PREEMPTED",
            "review_context": {"activity_state": "idle", "redaction_notes": [],
                               "candidate_labels": [{"id": "x", "source_type": "news", "safe_title": "x"}]},
        },
        {
            "turn_id": "technical-generation", "delivered": False,
            "actual_reason_code": "PASS_GENERATION_EMPTY",
            "review_context": {"activity_state": "idle", "redaction_notes": [],
                               "candidate_labels": [{"id": "y", "source_type": "news", "safe_title": "y"}]},
        },
    ])
    return {"observations": observations, "feedback": feedback}


def _complete(manifest: dict) -> None:
    for item in manifest["items"]:
        if not item["review_eligible"]:
            continue
        _delivered, label, _index = item["turn_id"].split("-") if item["turn_id"].count("-") == 2 else ("0", "0", "0")
        should = label == "1"
        item["primary_review"].update({
            "status": "completed", "reviewer_id": "primary", "reviewed_at": STAMP,
            "should_recommend": should, "confidence": "medium",
            "reason_code": "candidate_appropriate" if should else "activity_unsuitable",
        })
        if item["second_review"]["required"]:
            item["second_review"].update({
                "status": "completed", "reviewer_id": "second", "reviewed_at": STAMP,
                "should_recommend": should, "confidence": "medium",
                "reason_code": "candidate_appropriate" if should else "activity_unsuitable",
            })


def main() -> int:
    freeze = _freeze()
    manifest = build_timing_annotation_manifest(
        freeze, source_freeze_filename="freeze.json", source_freeze_sha256=SHA,
        created_at=STAMP,
    )
    assert not validate_timing_annotation_manifest(manifest)
    serialised = json.dumps(manifest, ensure_ascii=False)
    assert '"score"' not in serialised
    assert "must never leak" not in serialised
    assert "DELIVERY_PREEMPTED" not in serialised
    initial = timing_annotation_readiness(freeze, manifest)
    assert initial["status"] == "hold"
    assert initial["counts"]["structurally_eligible_count"] == 48

    draft = build_timing_annotation_assistant_draft(manifest)
    assert all(item["primary_review"]["status"] == "pending" for item in draft["items"])
    assert timing_annotation_readiness(freeze, draft)["status"] == "hold"
    assert "must never leak" not in json.dumps(draft, ensure_ascii=False)

    # Vision is semantically strong, but its final 0–3 competition score is
    # adjusted by interruptibility and then compared with all other sources.
    vision = [{"id": "vision:screen", "source_type": "vision", "safe_title": "screen_context"}]
    open_state = _interruptibility("idle", "")
    visual_focus_state = _interruptibility("idle", "请看这个窗口")
    restricted_state = _interruptibility("chatting", "")
    assert open_state["vision_semantic_relevance"] == 3
    assert open_state["vision_competition_score"] == 2
    assert visual_focus_state["vision_competition_score"] == 3
    assert restricted_state["vision_competition_score"] == 0
    assert _candidate_relevance(
        vision, dialogue="", repeat_evidence={},
        vision_competition_score=open_state["vision_competition_score"],
    ) == {"vision:screen": 2}
    assert _candidate_relevance(
        vision, dialogue="", repeat_evidence={},
        vision_competition_score=restricted_state["vision_competition_score"],
    ) == {"vision:screen": 0}
    assert _candidate_relevance(
        vision, dialogue="", repeat_evidence={},
        vision_competition_score=visual_focus_state["vision_competition_score"],
    ) == {"vision:screen": 3}

    # Reused redacted review context is an annotation-dedup signal only.  The
    # follow-up row abstains; no fatigue/scheduling rule is inferred from it.
    duplicate_vision = deepcopy(manifest)
    for index in (0, 1):
        duplicate_vision["items"][index]["context_for_blind_review"].update({
            "activity_state": "idle",
            "candidates": [{"id": f"vision:{index}", "source_type": "vision", "safe_title": "screen_context"}],
            "pre_decision_context": {"available": True, "messages": [{"role": "user", "content": "same context"}]},
        })
    deduplicated = build_timing_annotation_assistant_draft(duplicate_vision)
    assert deduplicated["items"][0]["assistant_pre_annotation"]["status"] == "proposed"
    assert deduplicated["items"][1]["assistant_pre_annotation"]["status"] == "abstained"
    assert deduplicated["items"][1]["assistant_pre_annotation"]["abstain_reason"] == "repeated_vision_review_context"

    _complete(manifest)
    assert not validate_timing_annotation_manifest(manifest)
    ready = timing_annotation_readiness(freeze, manifest)
    assert ready["status"] == "ready_for_f2_rerun", ready
    assert ready["counts"]["qualified_count"] == 48
    assert ready["counts"]["qualified_delivered_count"] == 24
    assert ready["counts"]["qualified_pass_count"] == 24
    assert ready["counts"]["cells"] == {
        "delivered_should_true": 12, "delivered_should_false": 12,
        "pass_should_true": 12, "pass_should_false": 12,
    }

    leaked = deepcopy(manifest)
    leaked["items"][0]["context_for_blind_review"]["score"] = 0.9
    assert any(error["path"].endswith(".score") for error in validate_timing_annotation_manifest(leaked))
    print("P49 TIMING ANNOTATION RESTART SMOKE OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
