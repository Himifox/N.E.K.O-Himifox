"""P44-F1 offline PASS/no-op threshold analysis smoke."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tests.testbench.pipeline.recommendation_threshold_analysis import (
    analyze_pass_noop_thresholds,
)


def _annotation(
    turn_id: str,
    score: float,
    delivered: bool,
    should: bool,
    source: str = "news",
) -> dict:
    return {
        "turn_id": turn_id,
        "primary_review_status": "accepted",
        "should_recommend": should,
        "context_for_review": {
            "top1_score": score,
            "top1": source,
            "activity": "idle",
            "candidates": [{
                "id": f"{source}:{turn_id}",
                "source_type": source,
                "score": score,
            }],
        },
        "realization_review_context": {"delivered": delivered},
    }


def main() -> int:
    workbook = {
        "annotations": [
            _annotation("a", 0.40, True, False, "news"),
            _annotation("b", 0.50, True, True, "music"),
            _annotation("c", 0.60, True, False, "vision"),
            _annotation("d", 0.70, False, True, "meme"),
            {
                **_annotation("excluded", 0.80, True, True),
                "adjudication_status": "excluded_abstention",
            },
        ]
    }
    original = deepcopy(workbook)
    report = analyze_pass_noop_thresholds(
        workbook,
        thresholds=[0.0, 0.55, 0.65],
    )
    assert workbook == original
    assert report["eligible_count"] == 4
    assert report["excluded_count"] == 1
    assert report["production_baseline"]["confusion_matrix"] == {
        "tp": 1,
        "fp": 2,
        "tn": 0,
        "fn": 1,
    }
    middle = report["threshold_curve"][1]
    assert middle["threshold"] == 0.55
    assert middle["confusion_matrix"] == {
        "tp": 0,
        "fp": 1,
        "tn": 1,
        "fn": 2,
    }
    assert report["score_only_curve"][2]["confusion_matrix"] == {
        "tp": 1,
        "fp": 0,
        "tn": 2,
        "fn": 1,
    }
    assert report["production_config_modified"] is False
    assert report["tuning_modified"] is False
    assert report["selected_threshold_impact"]["source_impact"]
    assert report["limitations"]
    assert report["conclusion"]["production_candidate_status"] in {
        "candidate_available",
        "no_universal_threshold_candidate",
    }
    one_class = analyze_pass_noop_thresholds(
        {"annotations": [
            _annotation("positive-only", 0.50, True, True),
        ]},
        thresholds=[0.0, 0.5],
    )
    assert one_class["score_distribution"]["negative_count"] == 0
    assert one_class["score_distribution"]["negative_mean"] is None
    assert one_class["score_distribution"]["roc_auc"] is None
    try:
        analyze_pass_noop_thresholds(workbook, thresholds=[-0.1])
    except ValueError as exc:
        assert "0-1" in str(exc)
    else:
        raise AssertionError("negative threshold must fail")
    print("P46 RECOMMENDATION THRESHOLD ANALYSIS SMOKE OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
