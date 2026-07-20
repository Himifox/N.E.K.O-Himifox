"""P45 recommendation pre-decision context recovery smoke."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tests.testbench.pipeline.recommendation_context_recovery import (
    load_time_indexed_archive,
    recover_candidate_review_context,
)
from tests.testbench.pipeline.recommendation_annotation_report import (
    build_annotation_report,
)
from tests.testbench.pipeline.recommendation_review_batch import (
    apply_blind_second_review_corrections,
    apply_review_batch,
    build_lightweight_adjudication_bundle,
    build_context_recovered_blind_bundle,
    expand_review_seed,
    finalize_lightweight_adjudication,
    merge_blind_second_reviews,
    normalize_blind_second_reviews,
    reposition_blind_second_reviews,
    validate_lightweight_adjudication_bundle,
)


def _epoch(value: str) -> float:
    return datetime.fromisoformat(value).replace(
        tzinfo=ZoneInfo("Asia/Shanghai")
    ).timestamp()


def main() -> int:
    with tempfile.TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "time_indexed.db"
        connection = sqlite3.connect(db_path)
        try:
            connection.execute(
                "CREATE TABLE time_indexed_original ("
                "id INTEGER PRIMARY KEY, session_id VARCHAR, "
                "message TEXT, timestamp DATETIME)"
            )
            for row in (
                (1, "s1", "human", "候选前用户上下文", "2026-07-16 10:00:00"),
                (2, "s1", "ai", "候选前助手回复", "2026-07-16 10:01:00"),
                (3, "s2", "human", "决定后的未来消息", "2026-07-16 10:03:00"),
            ):
                connection.execute(
                    "INSERT INTO time_indexed_original "
                    "(id, session_id, message, timestamp) VALUES (?, ?, ?, ?)",
                    (
                        row[0],
                        row[1],
                        json.dumps(
                            {"type": row[2], "data": {"content": row[3]}},
                            ensure_ascii=False,
                        ),
                        row[4],
                    ),
                )
            connection.commit()
        finally:
            connection.close()

        turns, source_meta = load_time_indexed_archive(db_path)
        observation_ts = _epoch("2026-07-16 10:02:00")
        freeze = {
            "observations": [{
                "turn_id": "turn-1",
                "ts": observation_ts,
                "top_candidates": [{
                    "id": "music:1", "source_type": "music", "score": 0.6,
                }],
            }],
        }
        workbook = {
            "instructions": {},
            "annotations": [{
                "turn_id": "turn-1",
                "should_recommend": True,
                "relevance": {"music:1": 3},
                "acceptable_top1_sources": ["music"],
                "primary_review_status": "pending",
                "context_for_review": {
                    "activity": "idle",
                    "delivered": True,
                    "reason": "CHAT_DELIVERED",
                    "delivered_excerpt": "下游生成文本",
                    "candidates": [{
                        "id": "music:1", "source_type": "music", "score": 0.6,
                    }],
                },
            }],
        }
        recovered, summary = recover_candidate_review_context(
            freeze, workbook, turns, source_meta, max_messages=10
        )
        row = recovered["annotations"][0]
        context = row["context_for_review"]
        pre = context["pre_decision_context"]
        assert [message["db_row_id"] for message in pre["messages"]] == [1, 2]
        assert all(
            message["ts_epoch"] <= observation_ts
            for message in pre["messages"]
        )
        assert "delivered_excerpt" not in context
        assert row["realization_review_context"]["delivered_excerpt"] == "下游生成文本"
        assert summary["recovered_count"] == 1
        assert summary["causal_violation_count"] == 0
        assert summary["temporal_confidence_distribution"] == {"high": 1}
        assert source_meta["source_row_count"] == 3
        report = build_annotation_report(recovered)
        assert report["summary"]["gate_eligible_count"] == 1
        assert report["summary"]["gate_confusion_matrix"]["tp"] == 1
        batch = {
            "schema_version": 1,
            "batch_id": "batch-test",
            "assistant_reviewer_id": "codex-test",
            "assistant_reviewed_at": "2026-07-20T10:00:00+08:00",
            "items": [{
                "turn_id": "turn-1",
                "confidence": "high",
                "primary_review_status": "corrected",
                "fields": {
                    "should_recommend": False,
                    "acceptable_top1_sources": [],
                    "relevance": {"music:1": 0},
                    "must_filter_candidate_ids": [],
                    "expected_filter_reasons": {},
                    "interruption_level": "high",
                    "privacy_risk": "none",
                    "score_diagnosis": "reasonable",
                    "issue_layer": "none",
                    "comment": "candidate-first proposal",
                },
            }],
        }
        seed = {
            "schema_version": 1,
            "batch_id": "batch-test",
            "assistant_reviewer_id": "codex-test",
            "assistant_reviewed_at": "2026-07-20T10:00:00+08:00",
            "items": [{
                "turn_id": "turn-1",
                "confidence": "high",
                "should_recommend": False,
                "acceptable_top1_sources": [],
                "relevance_by_source": {"music": 0},
                "interruption_level": "interruptive",
                "score_diagnosis": "over_scored",
                "issue_layer": "score",
                "comment": "source-keyed seed",
            }],
        }
        expanded = expand_review_seed(recovered, seed)
        assert expanded["items"][0]["fields"]["relevance"] == {"music:1": 0}
        assert expanded["items"][0]["fields"]["privacy_risk"] == "none"
        proposed, proposed_summary = apply_review_batch(recovered, batch)
        proposed_row = proposed["annotations"][0]
        assert proposed_summary["applied_count"] == 1
        assert proposed_row["primary_review_status"] == "pending"
        assert proposed_row["assistant_review_proposal"]["status"] == "proposed"
        confirmed, confirmed_summary = apply_review_batch(
            recovered,
            batch,
            confirm=True,
            primary_reviewer_id="human-test",
            primary_reviewed_at="2026-07-20T10:05:00+08:00",
        )
        confirmed_row = confirmed["annotations"][0]
        assert confirmed_summary["confirmed_count"] == 1
        assert confirmed_row["primary_review_status"] == "corrected"
        assert confirmed_row["primary_reviewer_id"] == "human-test"
        assert confirmed_row["assistant_review_proposal"]["status"] == "human_confirmed"

        invalid_batch = deepcopy(batch)
        invalid_batch["items"][0]["fields"]["relevance"] = {"unknown:1": 3}
        try:
            apply_review_batch(recovered, invalid_batch)
        except ValueError as exc:
            assert "relevance must cover every candidate exactly" in str(exc)
        else:
            raise AssertionError("mismatched relevance candidate IDs must fail")

        try:
            apply_review_batch(
                recovered,
                batch,
                confirm=True,
                primary_reviewer_id="human-test",
                primary_reviewed_at="2026-07-20T10:05:00",
            )
        except ValueError as exc:
            assert "timezone" in str(exc)
        else:
            raise AssertionError("naive reviewed_at must fail")

        invalid_seed = deepcopy(seed)
        invalid_seed["items"][0]["relevance_by_source"] = {"news": 0}
        try:
            expand_review_seed(recovered, invalid_seed)
        except ValueError as exc:
            assert "cover candidate sources exactly" in str(exc)
        else:
            raise AssertionError("source-keyed seed coverage mismatch must fail")

        blind = build_context_recovered_blind_bundle(recovered, ["turn-1"])
        blind_row = blind["reviews"][0]
        assert blind["schema_version"] == 2
        assert blind_row["turn_id"] == "turn-1"
        assert "pre_decision_context" in blind_row["context_for_review"]
        assert "delivered_excerpt" not in blind_row["context_for_review"]
        assert "realization_review_context" not in blind_row
        assert "primary_review_status" not in blind_row
        assert "assistant_review_proposal" not in blind_row
        assert blind_row["second_review"]["status"] == "pending"
        assert blind_row["second_review"]["should_recommend"] is None

        causal_leak = deepcopy(recovered)
        causal_leak["annotations"][0]["context_for_review"][
            "pre_decision_context"
        ]["messages"][0]["ts_epoch"] = observation_ts + 1
        try:
            build_context_recovered_blind_bundle(causal_leak, ["turn-1"])
        except ValueError as exc:
            assert "causal boundary" in str(exc)
        else:
            raise AssertionError("blind review causal leak must fail")

        shorthand = deepcopy(blind)
        shorthand["reviews"][0]["second_review"].update({
            "reviewer_id": "reviewer-02",
            "should_recommend": "false",
            "relevance": {"music": "2"},
        })
        normalized_blind, normalization_audit = normalize_blind_second_reviews(
            shorthand,
            default_reviewer_id="reviewer-02",
            completed_at="2026-07-20T14:00:00+08:00",
        )
        normalized_second = normalized_blind["reviews"][0]["second_review"]
        assert normalized_second["status"] == "completed"
        assert normalized_second["should_recommend"] is False
        assert normalized_second["relevance"] == {"music:1": 2}
        assert normalization_audit["unresolved_count"] == 0

        abstained_blind = deepcopy(normalized_blind)
        abstained_blind["reviews"][0]["second_review"].update({
            "status": "abstained",
            "should_recommend": None,
            "relevance": {},
            "abstain_reason": "insufficient_context",
        })
        renormalized_abstained, abstained_audit = normalize_blind_second_reviews(
            abstained_blind,
            default_reviewer_id="reviewer-02",
            completed_at="2026-07-20T14:00:00+08:00",
        )
        assert abstained_audit["unresolved_count"] == 0
        assert (
            renormalized_abstained["reviews"][0]["second_review"]["status"]
            == "abstained"
        )

        position_bundle = deepcopy(shorthand)
        position_bundle["reviews"].append(deepcopy(position_bundle["reviews"][0]))
        position_bundle["reviews"][1]["turn_id"] = "turn-position-2"
        position_bundle["reviews"][1]["second_review"] = {
            "required": True,
            "status": "pending",
            "reviewer_id": "",
            "reviewed_at": "",
            "should_recommend": None,
            "relevance": {},
            "comment": "",
            "abstain_reason": "",
        }
        repositioned, position_audit = reposition_blind_second_reviews(
            position_bundle,
            moves=[(1, 2)],
        )
        assert position_audit["operations"][0]["operation"] == "move"
        assert repositioned["reviews"][0]["second_review"]["should_recommend"] is None
        assert repositioned["reviews"][1]["second_review"]["should_recommend"] == "false"

        corrected, correction_audit = apply_blind_second_review_corrections(
            repositioned,
            [{
                "turn_id": "turn-1",
                "should_recommend": True,
                "relevance": {"music": 3},
                "comment_append": "human correction",
            }],
        )
        assert correction_audit["applied_count"] == 1
        assert corrected["reviews"][0]["second_review"]["should_recommend"] is True
        assert corrected["reviews"][0]["second_review"]["relevance"]["music"] == 3

        merge_workbook = deepcopy(workbook)
        merge_workbook["annotations"][0]["primary_review_status"] = "accepted"
        merge_workbook["annotations"][0]["primary_reviewer_id"] = "primary-reviewer"
        merge_workbook["annotations"][0]["primary_reviewed_at"] = (
            "2026-07-20T13:00:00+08:00"
        )
        merge_workbook["annotations"][0].setdefault(
            "second_review", {}
        )["required"] = True
        merge_blind = deepcopy(normalized_blind)
        merged, agreement = merge_blind_second_reviews(
            merge_workbook,
            merge_blind,
        )
        assert merged["annotations"][0]["second_review"]["status"] == "completed"
        assert agreement["required_count"] == 1
        assert agreement["handled_count"] == 1

        adjudication_bundle = build_lightweight_adjudication_bundle(merged)
        assert adjudication_bundle["counts"]["A"] == 1
        assert adjudication_bundle["items"][0]["adjudication"]["status"] == "pending"
        assert "delivered" not in adjudication_bundle["items"][0]["context_for_review"]
        assert not validate_lightweight_adjudication_bundle(
            adjudication_bundle,
            require_complete=False,
        )
        invalid_adjudication = deepcopy(adjudication_bundle)
        invalid_adjudication["items"][0]["adjudication"].update({
            "status": "completed",
            "adjudicator_id": "adjudicator",
            "adjudicated_at": "2026-07-20T15:00:00+08:00",
            "candidate_relevance": {"music:1": 0},
            "timing_ok": True,
            "fatigue_suppressed": False,
            "should_recommend": True,
            "reason_code": "ordinary_recommendation",
        })
        assert any(
            "all-zero recommendation" in error["message"]
            for error in validate_lightweight_adjudication_bundle(
                invalid_adjudication,
                require_complete=True,
            )
        )
        finalized_workbook, finalized_bundle, finalization_audit = (
            finalize_lightweight_adjudication(
                merged,
                adjudication_bundle,
                [{
                    "turn_id": "turn-1",
                    "candidate_relevance": {"music:1": 2},
                    "timing_ok": True,
                    "fatigue_suppressed": False,
                    "should_recommend": False,
                    "reason_code": "test_decision",
                    "comment": "confirmed",
                }],
                adjudicator_id="adjudicator",
                adjudicated_at="2026-07-20T16:00:00+08:00",
            )
        )
        assert finalization_audit["validation_error_count"] == 0
        assert (
            finalized_workbook["annotations"][0][
                "adjudicated_should_recommend"
            ]
            is False
        )
        assert finalized_bundle["items"][0]["adjudication"]["status"] == "completed"

        try:
            recover_candidate_review_context(
                freeze, workbook, turns, source_meta, max_messages=0
            )
        except ValueError as exc:
            assert "max_messages" in str(exc)
        else:
            raise AssertionError("max_messages=0 must fail")

    print("P45 RECOMMENDATION CONTEXT RECOVERY SMOKE OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
