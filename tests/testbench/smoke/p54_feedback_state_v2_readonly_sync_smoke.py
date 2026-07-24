"""P44-G1 stage 3: v1/v2 read-only Testbench safe-view sync."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys
import tempfile

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tests.testbench.pipeline import recommendation_adapter as adapter
from tests.testbench.pipeline.recommendation_safe_export import (
    RecommendationSafeExportError,
    prepare_recommendation_safe_view,
    read_recommendation_safe_export,
    write_new_recommendation_safe_export,
)
from tests.testbench.pipeline.recommendation_scenario import read_scenario


def _timing() -> dict:
    return {
        "configured_interval_seconds": 300,
        "elapsed_since_last_delivery_seconds": None,
        "recent_delivery_count_30m": 0,
        "recent_delivery_count_2h": 0,
        "consecutive_unanswered_deliveries": 0,
    }


def _review_context() -> dict:
    return {
        "schema_version": 1,
        "activity_state": "idle",
        "candidate_labels": [{
            "id": "music:1",
            "source_type": "music",
            "safe_title": "安全标题",
            "safe_summary": "安全摘要",
            "score": 0.7,
        }],
        "delivered_excerpt": "安全投递摘要",
        "redaction_notes": [],
    }


def _v1() -> dict:
    return {
        "version": "feedback_state_preview_v1",
        "temporary": {"ttl_seconds": 7_200, "sources": {}},
        "persistent": {"min_explicit_evidence": 3, "sources": {}},
    }


def _v2() -> dict:
    return {
        "version": "feedback_state_preview_v2",
        "preview_only": False,
        "ranking_consumed": True,
        "tuning_consumed": True,
        "conversation_acceptance": {
            "temporary": {
                "ttl_seconds": 7_200,
                "interest_preview": 0.6,
                "positive_evidence_count": 3,
                "negative_evidence_count": 0,
                "expires_in_seconds": 6_000,
                "messages": ["private"],
            },
            "persistent": {
                "min_explicit_evidence": 3,
                "positive_evidence_count": 3,
                "negative_evidence_count": 0,
                "updated_at": 100.0,
                "acceptance_preview": 0.2,
            },
        },
        "source_affinity": {
            "temporary": {
                "ttl_seconds": 7_200,
                "sources": {"music": {
                    "interest_preview": 0.4,
                    "positive_evidence_count": 2,
                    "negative_evidence_count": 0,
                    "expires_in_seconds": 6_000,
                    "reply_latency_seconds": 12.5,
                    "title": "private-title",
                }},
            },
            "persistent": {
                "min_explicit_evidence": 3,
                "sources": {"music": {
                    "positive_evidence_count": 2,
                    "negative_evidence_count": 0,
                    "updated_at": 100.0,
                    "affinity_preview": 0.1,
                    "url": "https://private.example/token=secret",
                }},
            },
        },
    }


def _observation(turn_id: str, preview: dict) -> dict:
    return {
        "ts": 100.0,
        "lanlan_name": "safe-view-user",
        "turn_id": turn_id,
        "algorithm_version": "0.8.3:proactive-recommendation-observation-v3",
        "decision_context": {"timing": _timing()},
        "review_context": _review_context(),
        "feedback_state_preview": preview,
        "recommendation_mode": "shadow",
        "delivered": True,
        "unknown_raw_field": "remains-only-in-source",
    }


def _artifact() -> dict:
    return {
        "schema_version": 1,
        "dataset_type": "shadow_preview_encounter",
        "observations": [
            _observation("turn-v1", _v1()),
            _observation("turn-v2", _v2()),
        ],
        "feedback": [{
            "ts": 110.0,
            "lanlan_name": "safe-view-user",
            "turn_id": "turn-v2",
            "source_type": "music",
            "candidate_id": "music:1",
            "event_type": "user_reply",
            "event_group": "generic_engagement",
            "report_score_v1": 0.15,
            "confidence": "medium",
            "score_version": "report_score_v1",
            "metadata": {
                "reply_latency_seconds": 12.5,
                "active_playback_ms": 12_252,
                "played_wall_ms": 23_940,
                "unknown_metadata": "drop-me",
            },
            "unknown_raw_field": "remains-only-in-source",
        }],
        "annotations": [{
            "turn_id": "turn-v2",
            "context_for_review": {"note": "preserve"},
            "candidate_review_context": {"note": "preserve"},
            "realization_review_context": {"delivered_excerpt": "preserve"},
        }],
        "items": [{
            "turn_id": "turn-v2",
            "context_for_blind_review": {"activity_state": "idle"},
        }],
    }


def main() -> int:
    raw = _artifact()
    original = deepcopy(raw)
    safe = prepare_recommendation_safe_view(raw)
    assert raw == original

    previews = [row["feedback_state_preview"] for row in safe["observations"]]
    assert [preview["version"] for preview in previews] == [
        "feedback_state_preview_v1",
        "feedback_state_preview_v2",
    ]
    v2 = previews[1]
    assert v2["preview_only"] is True
    assert v2["ranking_consumed"] is False
    assert v2["tuning_consumed"] is False
    assert v2["conversation_acceptance"]["persistent"]["acceptance_preview"] == 0.2
    assert v2["source_affinity"]["persistent"]["sources"]["music"]["affinity_preview"] == 0.1
    dumped = json.dumps(safe, ensure_ascii=False)
    assert "private-title" not in dumped and "private.example" not in dumped
    assert "unknown_raw_field" not in dumped and "unknown_metadata" not in dumped
    assert safe["feedback"][0]["metadata"]["reply_latency_seconds"] == 12.5
    assert safe["feedback"][0]["metadata"]["active_playback_ms"] == 12_252
    assert safe["feedback"][0]["metadata"]["played_wall_ms"] == 23_940
    assert safe["observations"][1]["review_context"]["delivered_excerpt"] == "安全投递摘要"
    assert safe["annotations"] == raw["annotations"]
    assert safe["items"] == raw["items"]

    with tempfile.TemporaryDirectory(prefix="neko-safe-export-") as directory:
        root = Path(directory)
        source = root / "source.json"
        target = root / "source-safe.json"
        source.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
        source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
        write_new_recommendation_safe_export(target, safe)
        assert hashlib.sha256(source.read_bytes()).hexdigest() == source_hash
        loaded = read_recommendation_safe_export(target)
        assert loaded == safe
        assert loaded["feedback"][0]["metadata"]["active_playback_ms"] == 12_252
        assert prepare_recommendation_safe_view(loaded) == loaded
        try:
            write_new_recommendation_safe_export(target, safe)
        except RecommendationSafeExportError:
            pass
        else:
            raise AssertionError("existing safe export must not be overwritten")
        try:
            write_new_recommendation_safe_export(source, safe)
        except RecommendationSafeExportError:
            pass
        else:
            raise AssertionError("source artifact must not be overwritten")

    scenario = read_scenario("competition_15")
    baseline = adapter.run_source_stage(scenario, {"id": "production_default"})
    with_preview = deepcopy(scenario)
    with_preview.setdefault("context", {})["feedback_state_preview"] = _v2()
    replay = adapter.run_source_stage(with_preview, {"id": "production_default"})
    for key in ("ranked_candidates", "score_breakdown", "top1_candidate_id", "filtered_reasons"):
        assert baseline[key] == replay[key]
    ranking_source = (PROJECT_ROOT / "main_logic" / "proactive_recommendation.py").read_text(encoding="utf-8")
    assert "feedback_state_preview" not in ranking_source

    print("P54 FEEDBACK STATE V2 READONLY SYNC SMOKE OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
