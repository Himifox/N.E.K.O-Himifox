"""P44-G1-R2 gradual personalization response-curve smoke."""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tests.testbench.pipeline.recommendation_personalization_response_curve import (
    MAX_ABS_SCORE_DELTA,
    analyze_personalization_response_curves,
    render_personalization_response_curves_markdown,
)


def _preview(*, positive: int, negative: int, affinity: float) -> dict:
    return {
        "version": "feedback_state_preview_v2",
        "preview_only": True,
        "ranking_consumed": False,
        "tuning_consumed": False,
        "conversation_acceptance": {
            "temporary": {
                "ttl_seconds": 7_200,
                "positive_evidence_count": 20,
                "negative_evidence_count": 0,
                "interest_preview": 1.0,
                "expires_in_seconds": 7_000,
            },
            "persistent": {
                "min_explicit_evidence": 3,
                "positive_evidence_count": 20,
                "negative_evidence_count": 0,
                "updated_at": 90.0,
                "acceptance_preview": 0.2,
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


def _observation(
    turn_id: str,
    ts: float,
    *,
    positive: int,
    negative: int = 0,
    affinity: float = 0.2,
    candidates: list[dict] | None = None,
) -> dict:
    return {
        "ts": ts,
        "lanlan_name": "g1-r2-user",
        "turn_id": turn_id,
        "recommendation_mode": "shadow",
        "feedback_state_preview": _preview(
            positive=positive,
            negative=negative,
            affinity=affinity,
        ),
        "top_candidates": candidates or [
            {"rank": 1, "id": f"news:{turn_id}", "source_type": "news", "score": 0.50},
            {"rank": 2, "id": f"music:{turn_id}", "source_type": "music", "score": 0.49},
        ],
    }


def _music(row: dict) -> dict:
    return next(
        candidate
        for candidate in row["candidate_scores"]
        if candidate["source_type"] == "music"
    )


def main() -> int:
    dataset = {
        "as_of": 100.0,
        "observations": [
            _observation("e2", 2.0, positive=2, affinity=0.0),
            _observation("e3", 3.0, positive=3),
            _observation(
                "e6",
                6.0,
                positive=6,
                candidates=[
                    {"rank": 1, "id": "news:e6", "source_type": "news", "score": 0.60},
                    {"rank": 2, "id": "meme:e6", "source_type": "meme", "score": 0.55},
                    {"rank": 3, "id": "music:e6", "source_type": "music", "score": 0.54},
                ],
            ),
            _observation("e8", 8.0, positive=8),
            _observation("e12", 12.0, positive=12),
            _observation("e20", 20.0, positive=20),
            _observation("mixed", 30.0, positive=4, negative=2, affinity=0.067),
            _observation("negative", 40.0, positive=0, negative=3, affinity=-0.2),
            _observation(
                "single",
                50.0,
                positive=8,
                candidates=[
                    {"rank": 1, "id": "music:single", "source_type": "music", "score": 0.48},
                ],
            ),
            _observation("future", 200.0, positive=99),
        ],
        "feedback": [
            {"turn_id": "e3", "ts": 4.0, "event_type": "music_played_through"},
        ],
    }
    original = deepcopy(dataset)
    report = analyze_personalization_response_curves(dataset)
    replay = analyze_personalization_response_curves(dataset)
    assert dataset == original
    assert report == replay
    assert report["input"]["eligible_ranked_observation_count"] == 9
    assert report["data_issues"]["distribution"]["observation_after_as_of"] == 1
    assert report["diagnosis"]["r1_comparison"]["matches"] is True
    assert report["hard_violations"] == []
    assert report["conclusion"]["status"] == "response_curve_descriptive_only"
    assert report["conclusion"]["effectiveness_evaluated"] is False
    assert report["conclusion"]["candidate_for_shadow"] is False
    assert report["conclusion"]["production_config_modified"] is False

    rows = {row["turn_id"]: row for row in report["rows"]}
    assert all(
        value["score_delta"] == 0
        for value in _music(rows["e2"])["variants"].values()
    )
    assert _music(rows["e3"])["variants"]["gradual_8"]["score_delta"] == 0.01125
    assert _music(rows["e3"])["variants"]["gradual_12"]["score_delta"] == 0.0075
    assert _music(rows["e6"])["variants"]["gradual_8"]["score_delta"] == 0.0225
    assert _music(rows["e6"])["variants"]["gradual_12"]["score_delta"] == 0.015
    assert _music(rows["e8"])["variants"]["gradual_8"]["score_delta"] == 0.03
    assert _music(rows["e12"])["variants"]["gradual_12"]["score_delta"] == 0.03
    assert _music(rows["e20"])["variants"]["gradual_20"]["score_delta"] == 0.03
    assert _music(rows["mixed"])["variants"]["gradual_12"]["score_delta"] == 0.005025
    assert _music(rows["negative"])["variants"]["gradual_12"]["score_delta"] == -0.0075

    e6 = rows["e6"]
    assert e6["variant_results"]["gradual_12"]["top1_changed"] is False
    assert e6["variant_results"]["gradual_12"]["top3_changed"] is True
    assert _music(e6)["variants"]["gradual_12"]["rank"] == 2
    single = rows["single"]
    assert single["candidate_count"] == 1
    assert single["variant_results"]["gradual_12"]["top1_changed"] is False
    assert _music(single)["gap_to_previous"] is None
    assert _music(single)["gap_to_top"] == 0.0

    for row in report["rows"]:
        for candidate in row["candidate_scores"]:
            for value in candidate["variants"].values():
                assert abs(value["score_delta"]) <= MAX_ABS_SCORE_DELTA
        for candidate in row["candidate_scores"]:
            if candidate["source_type"] != "music":
                assert all(
                    value["score_delta"] == 0
                    for value in candidate["variants"].values()
                )

    changed_feedback = deepcopy(dataset)
    changed_feedback["feedback"].extend([
        {"turn_id": "e3", "ts": 4.0, "event_type": "music_played_through"},
        {"turn_id": "e3", "ts": 999.0, "event_type": "future_feedback"},
    ])
    changed_report = analyze_personalization_response_curves(changed_feedback)
    assert changed_report["rows"] == report["rows"]
    assert changed_report["variant_impact"] == report["variant_impact"]
    assert changed_report["evidence_trajectories"] == report["evidence_trajectories"]

    markdown = render_personalization_response_curves_markdown(report)
    assert "逐 observation 资源分数" in markdown
    assert "music:e6" in markdown
    assert "gradual_12" in markdown
    assert "非 Top-1 Music 分差" in markdown
    json.dumps(report, ensure_ascii=False, allow_nan=False)

    source = (
        PROJECT_ROOT
        / "tests"
        / "testbench"
        / "pipeline"
        / "recommendation_personalization_response_curve.py"
    ).read_text(encoding="utf-8")
    assert "from main_logic.proactive_recommendation_feedback_state import" in source
    assert "PERSISTENT_AFFINITY_MAX = 0.2" not in source
    assert "production_config_modified\": False" in source
    assert "conversation_acceptance_ranking_consumed\": False" in source
    router_source = (
        PROJECT_ROOT / "tests" / "testbench" / "routers" / "recommendation_router.py"
    ).read_text(encoding="utf-8")
    assert '@router.post("/personalization/response-curves")' in router_source
    print("P56 PERSONALIZATION RESPONSE CURVE SMOKE OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
