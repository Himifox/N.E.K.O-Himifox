"""P44-G1-R1 bounded point-in-time personalization impact smoke."""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tests.testbench.pipeline.recommendation_bounded_personalization import (
    BoundedPersonalizationError,
    analyze_bounded_personalization,
    render_bounded_personalization_markdown,
)


def _preview(*, affinity: float | None, positive: int, negative: int) -> dict:
    return {
        "version": "feedback_state_preview_v2",
        "preview_only": True,
        "ranking_consumed": False,
        "tuning_consumed": False,
        "conversation_acceptance": {
            "temporary": {
                "ttl_seconds": 7_200,
                "interest_preview": 1.0,
                "positive_evidence_count": 20,
                "negative_evidence_count": 0,
                "expires_in_seconds": 7_000,
            },
            "persistent": {
                "min_explicit_evidence": 3,
                "positive_evidence_count": 20,
                "negative_evidence_count": 0,
                "updated_at": 90.0,
                "acceptance_preview": 1.0,
            },
        },
        "source_affinity": {
            "temporary": {"ttl_seconds": 7_200, "sources": {}},
            "persistent": {
                "min_explicit_evidence": 3,
                "sources": {
                    "music": {
                        "positive_evidence_count": positive,
                        "negative_evidence_count": negative,
                        "updated_at": 90.0,
                        "affinity_preview": affinity,
                    }
                },
            },
        },
    }


def _observation(turn_id: str, ts: float, preview: dict) -> dict:
    return {
        "ts": ts,
        "lanlan_name": "g1-r1-user",
        "turn_id": turn_id,
        "recommendation_mode": "shadow",
        "feedback_state_preview": preview,
        "top_candidates": [
            {"rank": 1, "id": f"news:{turn_id}", "source_type": "news", "score": 0.50},
            {"rank": 2, "id": f"music:{turn_id}", "source_type": "music", "score": 0.49},
        ],
    }


def main() -> int:
    dataset = {
        "as_of": 200.0,
        "observations": [
            _observation("cold", 100.0, _preview(affinity=0.0, positive=2, negative=0)),
            _observation("warm", 110.0, _preview(affinity=1.0, positive=3, negative=0)),
            _observation("future", 300.0, _preview(affinity=-1.0, positive=3, negative=3)),
        ],
        "feedback": [],
    }
    original = deepcopy(dataset)
    report = analyze_bounded_personalization(dataset)
    replay = analyze_bounded_personalization(dataset)
    assert dataset == original
    assert report == replay
    assert report["conclusion"]["status"] == "impact_only"
    assert report["conclusion"]["effectiveness_evaluated"] is False
    assert report["conclusion"]["candidate_for_shadow"] is False
    assert report["candidate"]["conversation_acceptance_ranking_consumed"] is False
    assert report["input"]["eligible_ranked_observation_count"] == 2
    assert report["data_issues"]["distribution"]["observation_after_as_of"] == 1
    assert report["impact"]["warm_state_observation_count"] == 1
    assert report["impact"]["top1_flip_count"] == 1
    assert report["impact"]["source_score_impact"]["music"] == {
        "candidate_count": 2,
        "adjusted_candidate_count": 1,
        "average_baseline_score": 0.49,
        "average_candidate_score": 0.505,
        "average_score_delta": 0.015,
        "max_abs_score_delta": 0.03,
    }
    assert "no_negative_source_evidence" in report["conclusion"]["effectiveness_blockers"]
    assert report["hard_violations"] == []

    cold, warm = report["rows"]
    assert cold["top1_changed"] is False
    assert all(item["score_delta"] == 0 for item in cold["candidate_scores"])
    assert warm["baseline_top1_source"] == "news"
    assert warm["candidate_top1_source"] == "music"
    music = next(item for item in warm["candidate_scores"] if item["source_type"] == "music")
    assert music["score_delta"] == 0.03
    assert music["candidate_score"] == 0.52
    assert {item["id"] for item in warm["candidate_scores"]} == {
        "news:warm",
        "music:warm",
    }

    markdown = render_bounded_personalization_markdown(report)
    assert "impact_only" in markdown
    assert "conversation_acceptance" in markdown
    assert "各资源候选分数" in markdown
    assert "news:warm" not in markdown  # report exposes turn/source impact, not candidate payload.

    try:
        analyze_bounded_personalization(dataset, max_abs_delta=0.031)
    except BoundedPersonalizationError:
        pass
    else:
        raise AssertionError("delta above the registered bound must be rejected")

    source = (
        PROJECT_ROOT
        / "tests"
        / "testbench"
        / "pipeline"
        / "recommendation_bounded_personalization.py"
    ).read_text(encoding="utf-8")
    assert "proactive_recommendation_tuning" not in source
    assert "maybe_auto_apply" not in source
    assert "conversation_acceptance_ranking_consumed\": False" in source
    assert "production_config_modified\": False" in source
    router_source = (
        PROJECT_ROOT / "tests" / "testbench" / "routers" / "recommendation_router.py"
    ).read_text(encoding="utf-8")
    assert '@router.post("/personalization/bounded-impact")' in router_source
    json.dumps(report, ensure_ascii=False, allow_nan=False)
    print("P55 BOUNDED PERSONALIZATION SMOKE OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
