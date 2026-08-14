from __future__ import annotations

import json
from pathlib import Path

from scripts.evaluate_knowledge_hybrid_retrieval import select_threshold
from knowledge.vector_index import SEMANTIC_THRESHOLD


ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "tests" / "fixtures" / "knowledge_hybrid_real_model_cases.json"


def test_real_model_fixture_is_grounded_and_bounded():
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))

    assert payload["schema_version"] == 1
    assert payload["embedding_input_version"] == 2
    assert payload["quality_targets"] == {
        "recall_at_3": 0.8,
        "negative_rejection": 0.9,
    }
    assert len(payload["positives"]) == 10
    assert len(payload["negatives"]) == 20
    assert {case["expected_title"] for case in payload["positives"]} == {
        "吊桥效应",
        "全靠同行衬托",
        "半场开香槟",
        "人血馒头",
        "耗子尾汁",
        "现象级",
        "建国后不许成精",
        "电车难题",
        "扫地老太太",
        "永远的神",
    }
    assert {case["expected_collection"] for case in payload["positives"]} == {"meme"}
    identifiers = [
        case["id"] for group in ("positives", "negatives") for case in payload[group]
    ]
    assert len(identifiers) == len(set(identifiers))


def test_threshold_selection_reproduces_lowest_057_boundary():
    positives = [
        {"expected_rank": 1, "expected_score": score}
        for score in (0.82, 0.78, 0.74, 0.70, 0.66, 0.63, 0.61, 0.57)
    ] + [
        {"expected_rank": 4, "expected_score": 0.71},
        {"expected_rank": None, "expected_score": 0.54},
    ]
    negatives = [{"top1_score": score} for score in ([0.56] * 18 + [0.60, 0.62])]

    result = select_threshold(positives, negatives)

    assert result == {
        "threshold": 0.57,
        "recall_at_3": 0.8,
        "negative_rejection": 0.9,
        "positive_passes": 8,
        "negative_rejections": 18,
    }
    assert result["threshold"] == SEMANTIC_THRESHOLD


def test_threshold_selection_reports_unsatisfied_targets():
    positives = [{"expected_rank": 1, "expected_score": 0.40}] * 10
    negatives = [{"top1_score": 0.80}] * 20

    assert select_threshold(positives, negatives) is None
