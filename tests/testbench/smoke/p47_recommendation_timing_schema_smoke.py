"""P44-F2 schema-v3 timing audit and import contract smoke."""
from __future__ import annotations

import math
from pathlib import Path
import sys
import tempfile

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tests.testbench.pipeline.recommendation_timing_audit import (
    TIMING_FIELDS,
    audit_timing_dataset,
    inspect_timing_observation,
    observation_schema_generation,
    prepare_observation_for_timing_import,
    timing_analysis_readiness,
)
from main_logic.proactive_recommendation_observer import (
    sanitize_recommendation_observation,
)


ALGORITHM_V3 = "0.8.3:proactive-recommendation-observation-v3"


def _observation(
    index: int,
    *,
    elapsed: float | None,
    count_30m: int,
    count_2h: int,
    unanswered: int,
) -> dict:
    return {
        "turn_id": f"timing-{index}",
        "ts": 20_000.0 + index,
        "algorithm_version": ALGORITHM_V3,
        "git_revision": "timing-smoke",
        "activity_state": ("idle", "focused_work", "chatting")[index % 3],
        "shadow_selected_source_type": ("music", "news", "vision")[index % 3],
        "shadow_selected_score": 0.5,
        "top_candidates": [{
            "rank": 1,
            "id": f"music:{index}",
            "source_type": "music",
            "family": "music",
            "topic": "track",
            "score": 0.5,
        }],
        "delivered": index % 2 == 0,
        "decision_context": {
            "timing": {
                "configured_interval_seconds": 300,
                "elapsed_since_last_delivery_seconds": elapsed,
                "recent_delivery_count_30m": count_30m,
                "recent_delivery_count_2h": count_2h,
                "consecutive_unanswered_deliveries": unanswered,
            },
        },
    }


def main() -> int:
    legacy = {
        "turn_id": "legacy",
        "algorithm_version": "0.8.3:proactive-recommendation-observation-v2",
    }
    assert observation_schema_generation(legacy) == 2
    legacy_result = inspect_timing_observation(legacy)
    assert legacy_result["status"] == "timing_unavailable_legacy"
    assert not legacy_result["timing_eligible"]
    future = dict(
        legacy,
        algorithm_version="0.8.3:proactive-recommendation-observation-v4",
    )
    future_result = inspect_timing_observation(future)
    assert future_result["status"] == "timing_unsupported_future_schema"
    assert not future_result["timing_eligible"]

    valid = _observation(
        0,
        elapsed=540,
        count_30m=2,
        count_2h=5,
        unanswered=1,
    )
    inspected = inspect_timing_observation(valid)
    assert inspected["status"] == "timing_valid_v3", inspected["errors"]
    assert set(inspected["normalized_timing"]) == set(TIMING_FIELDS)

    missing = _observation(
        1,
        elapsed=540,
        count_30m=1,
        count_2h=1,
        unanswered=0,
    )
    del missing["decision_context"]["timing"]["recent_delivery_count_2h"]
    missing_result = inspect_timing_observation(missing)
    assert missing_result["status"] == "timing_invalid_v3"
    assert any(
        error["code"] == "missing_timing_field"
        for error in missing_result["errors"]
    )

    malformed = _observation(
        2,
        elapsed=math.nan,
        count_30m=3,
        count_2h=2,
        unanswered=True,
    )
    malformed["decision_context"]["timing"]["future_field"] = 1
    malformed_result = inspect_timing_observation(malformed)
    malformed_codes = {error["code"] for error in malformed_result["errors"]}
    assert {
        "timing_number_out_of_bounds",
        "timing_count_type_invalid",
        "timing_count_window_inconsistent",
        "unknown_timing_field",
    } <= malformed_codes

    observations = []
    for index in range(100):
        mode = index % 4
        if mode == 0:
            values = (None, 0, 0)
        elif mode == 1:
            values = (240.0, 1, 1)
        elif mode == 2:
            values = (540.0, 2, 3)
        else:
            values = (1_200.0, 1, 5)
        observations.append(_observation(
            index,
            elapsed=values[0],
            count_30m=values[1],
            count_2h=values[2],
            unanswered=mode,
        ))
    feedback = [
        {
            "turn_id": f"timing-{index}",
            "ts": 21_000.0 + index,
            "event_type": "user_replied_after_recommendation",
            "source_type": ("music", "news", "vision")[index % 3],
        }
        for index in range(30)
    ]
    dataset = {"observations": observations, "feedback": feedback}
    audit = audit_timing_dataset(dataset)
    assert audit["timing_valid_count"] == 100
    assert audit["timing_invalid_count"] == 0
    assert audit["timing_coverage_rate"] == 1.0
    assert audit["bucket_distribution"]["elapsed_since_last_delivery"] == {
        "first_or_no_history": 25,
        "lt_5m": 25,
        "5_to_10m": 25,
        "10_to_30m": 25,
        "gte_30m": 0,
    }
    readiness = timing_analysis_readiness(dataset)
    assert readiness["ready_for_timing_strategy_scan"], readiness["blockers"]
    assert readiness["pilot_contract_ready"]
    assert readiness["production_config_modified"] is False
    assert readiness["tuning_modified"] is False

    mixed = audit_timing_dataset({
        "observations": [legacy, valid, missing, future, {"turn_id": "unknown"}],
        "feedback": [],
    })
    assert mixed["timing_valid_count"] == 1
    assert mixed["timing_invalid_count"] == 1
    assert mixed["timing_unavailable_legacy_count"] == 1
    assert mixed["timing_unknown_version_count"] == 1
    assert mixed["timing_unsupported_future_count"] == 1

    # Simulate a v2 production sanitizer that does not yet know
    # decision_context. The Testbench compatibility bridge must preserve only
    # the validated timing fields and reject malformed schema-v3 observations.
    def v2_sanitizer(row: dict) -> dict:
        return {
            key: value
            for key, value in row.items()
            if key != "decision_context"
        }

    prepared = prepare_observation_for_timing_import(valid, v2_sanitizer)
    assert prepared["accepted"]
    assert prepared["observation"]["decision_context"]["timing"] == (
        inspected["normalized_timing"]
    )
    rejected = prepare_observation_for_timing_import(missing, v2_sanitizer)
    assert not rejected["accepted"]
    assert rejected["reason"] == "invalid_timing_context"
    assert rejected["observation"] is None
    production_prepared = prepare_observation_for_timing_import(
        valid,
        sanitize_recommendation_observation,
    )
    assert production_prepared["accepted"]
    assert production_prepared["observation"]["decision_context"]["timing"] == (
        inspected["normalized_timing"]
    )

    router_source = (
        PROJECT_ROOT / "tests/testbench/routers/recommendation_router.py"
    ).read_text(encoding="utf-8")
    assert "prepare_observation_for_timing_import" in router_source
    assert '"/datasets/{dataset_id}/timing-audit"' in router_source
    assert '"timing_readiness": timing_analysis_readiness(dataset)' in router_source
    try:
        from tests.testbench import config as tb_config
        from tests.testbench.routers.recommendation_router import (
            ImportBody,
            dataset_timing_audit,
            datasets_import,
            datasets_read,
        )
    except ModuleNotFoundError as exc:
        if exc.name != "fastapi":
            raise
    else:
        original_dataset_dir = tb_config.RECOMMENDATION_DATASETS_DIR
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                tb_config.RECOMMENDATION_DATASETS_DIR = Path(temp_dir)
                imported = datasets_import(ImportBody(
                    name="timing-import",
                    observations=[valid, missing],
                    feedback=[],
                ))
                assert imported["import_summary"]["accepted"] == 1
                assert imported["import_summary"]["rejected"] == 1
                rejection = imported["import_summary"]["rejections"][0]
                assert rejection["kind"] == "observation"
                assert rejection["reason"] == "invalid_timing_context"
                stored = datasets_read(imported["id"])
                assert stored["observations"][0]["decision_context"]["timing"] == (
                    inspected["normalized_timing"]
                )
                endpoint = dataset_timing_audit(imported["id"])
                assert endpoint["audit"]["timing_valid_count"] == 1
                assert endpoint["production_config_modified"] is False
        finally:
            tb_config.RECOMMENDATION_DATASETS_DIR = original_dataset_dir

    print("P47 RECOMMENDATION TIMING SCHEMA SMOKE OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
