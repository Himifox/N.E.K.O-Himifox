"""P44-G0 Testbench parity for the MVP feedback_state_preview contract."""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from main_logic.proactive_recommendation_observer import sanitize_recommendation_observation
from tests.testbench.pipeline import recommendation_adapter as adapter
from tests.testbench.pipeline.atomic_io import atomic_write_json
from tests.testbench.pipeline.recommendation_scenario import read_scenario
from tests.testbench.pipeline.recommendation_timing_audit import prepare_observation_for_timing_import


def _observation() -> dict:
    return {
        "ts": 1_000.0,
        "lanlan_name": "g0-user",
        "turn_id": "feedback-state-preview-turn",
        "algorithm_version": "0.8.3:proactive-recommendation-observation-v3",
        "decision_context": {
            "timing": {
                "configured_interval_seconds": 300,
                "elapsed_since_last_delivery_seconds": None,
                "recent_delivery_count_30m": 0,
                "recent_delivery_count_2h": 0,
                "consecutive_unanswered_deliveries": 0,
            },
        },
        "feedback_state_preview": {
            "version": "feedback_state_preview_v1",
            "preview_only": False,
            "ranking_consumed": True,
            "tuning_consumed": True,
            "temporary": {
                "ttl_seconds": 7_200,
                "sources": {
                    "music": {
                        "interest_preview": 0.4,
                        "positive_evidence_count": 2,
                        "negative_evidence_count": 0,
                        "expires_in_seconds": 6_000,
                        "reply_latency_seconds": 12.5,
                        "title": "private-title",
                    },
                },
            },
            "persistent": {
                "min_explicit_evidence": 3,
                "sources": {
                    "music": {
                        "positive_evidence_count": 3,
                        "negative_evidence_count": 1,
                        "updated_at": 999.0,
                        "affinity_preview": 0.1,
                        "url": "https://private.example/token=secret",
                    },
                },
            },
            "messages": ["must-not-leak"],
        },
    }


def main() -> int:
    raw = _observation()
    safe = sanitize_recommendation_observation(raw)
    preview = safe["feedback_state_preview"]

    # MVP semantics survive: preview only, 2-hour temporary TTL and 3-event gate.
    assert preview["version"] == "feedback_state_preview_v1"
    assert preview["preview_only"] is True
    assert preview["ranking_consumed"] is False
    assert preview["tuning_consumed"] is False
    assert preview["temporary"]["ttl_seconds"] == 7_200
    assert preview["persistent"]["min_explicit_evidence"] == 3
    assert preview["persistent"]["sources"]["music"]["affinity_preview"] == 0.1
    dumped = json.dumps(safe, ensure_ascii=False)
    assert "private-title" not in dumped and "must-not-leak" not in dumped
    assert "reply_latency" not in dumped and "private.example" not in dumped

    # Testbench import preparation -> atomic dataset persistence -> read preserves it.
    prepared = prepare_observation_for_timing_import(raw, sanitize_recommendation_observation)
    assert prepared["accepted"] is True
    assert prepared["observation"]["feedback_state_preview"] == safe["feedback_state_preview"]
    with tempfile.TemporaryDirectory(prefix="neko-feedback-state-preview-") as directory:
        dataset_path = Path(directory) / "feedback-state-preview.json"
        atomic_write_json(dataset_path, {"observations": [prepared["observation"]]})
        loaded = json.loads(dataset_path.read_text(encoding="utf-8"))
    assert loaded["observations"] == [prepared["observation"]]
    assert "private-title" not in json.dumps(loaded, ensure_ascii=False)

    # Preview state is exportable diagnostic data, never a ranking input.
    scenario = read_scenario("competition_15")
    baseline = adapter.run_source_stage(scenario, {"id": "production_default"})
    replayed = adapter.run_source_stage(scenario, {"id": "production_default"})
    assert baseline["ranked_candidates"] == replayed["ranked_candidates"]
    assert baseline["score_breakdown"] == replayed["score_breakdown"]
    ranking_source = (PROJECT_ROOT / "main_logic" / "proactive_recommendation.py").read_text(encoding="utf-8")
    assert "feedback_state_preview" not in ranking_source
    print("P52 FEEDBACK STATE PREVIEW SYNC SMOKE OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
