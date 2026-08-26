from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import numpy as np
import pytest

from knowledge.catalog_overrides import get_catalog_override_path
from knowledge.chunking import CHUNKER_VERSION
from knowledge.vector_index import SEMANTIC_THRESHOLD
from scripts.evaluate_knowledge_hybrid_retrieval import (
    EvaluationUnavailable,
    _load_vector_corpus,
    select_threshold,
)


ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "tests" / "fixtures" / "knowledge_hybrid_real_model_cases.json"


def _write_vector_database(path: Path, *, ambiguous_source: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    vector = np.asarray([1.0, 0.0], dtype="<f2").tobytes()
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            INSERT INTO metadata VALUES ('schema_version', '6');
            INSERT INTO metadata VALUES ('embedding_input_version', '1');
            INSERT INTO metadata VALUES ('chunker_version', '1');
            CREATE TABLE entries (
                title TEXT NOT NULL,
                terms TEXT NOT NULL,
                tags TEXT NOT NULL,
                summary TEXT NOT NULL,
                content TEXT NOT NULL
            );
            CREATE TABLE knowledge_chunks (
                chunk_id TEXT PRIMARY KEY,
                entry_rowid INTEGER NOT NULL,
                chunk_index INTEGER NOT NULL,
                embedding_model_id TEXT,
                embedding_dimensions INTEGER,
                embedding BLOB,
                embedding_status TEXT NOT NULL
            );
            """
        )
        first_tags = (
            ["source:fixture.disabled", "source:fixture.other"]
            if ambiguous_source
            else ["source:fixture.disabled"]
        )
        for title, tags in (
            ("Disabled entry", first_tags),
            ("Enabled entry", ["source:fixture.enabled"]),
        ):
            cursor = connection.execute(
                "INSERT INTO entries VALUES (?, '{}', ?, '', 'body')",
                (title, json.dumps(tags)),
            )
            connection.execute(
                "INSERT INTO knowledge_chunks VALUES (?, ?, 0, 'fixture-model', 2, ?, 'ready')",
                (f"chunk-{cursor.lastrowid}", cursor.lastrowid, vector),
            )


def test_real_model_fixture_is_grounded_and_bounded():
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))

    assert payload["schema_version"] == 1
    assert payload["embedding_input_version"] == 1
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


def test_vector_corpus_excludes_disabled_entries(tmp_path):
    database = tmp_path / "knowledge.db"
    _write_vector_database(database)
    get_catalog_override_path(database).write_text(
        json.dumps(
            {
                "disabled": [
                    {"source": "source:fixture.disabled", "title": "Disabled entry"}
                ]
            }
        ),
        encoding="utf-8",
    )

    corpus = _load_vector_corpus(
        tmp_path,
        model_id="fixture-model",
        dimensions=2,
    )

    assert [row["title"] for row in corpus.rows] == ["Enabled entry"]
    assert corpus.status["chunker_version"] == CHUNKER_VERSION
    assert corpus.status["ready_vectors"] == 1
    assert corpus.status["disabled_vectors"] == 1


def test_vector_corpus_rejects_invalid_catalog_override(tmp_path):
    database = tmp_path / "knowledge.db"
    _write_vector_database(database)
    get_catalog_override_path(database).write_text("{", encoding="utf-8")

    with pytest.raises(EvaluationUnavailable, match="catalog_override_unavailable"):
        _load_vector_corpus(tmp_path, model_id="fixture-model", dimensions=2)


def test_vector_corpus_rejects_ambiguous_entry_identity(tmp_path):
    database = tmp_path / "knowledge.db"
    _write_vector_database(database, ambiguous_source=True)

    with pytest.raises(EvaluationUnavailable, match="entry_identity_unavailable"):
        _load_vector_corpus(tmp_path, model_id="fixture-model", dimensions=2)


def test_vector_corpus_rejects_mismatched_chunker_version(tmp_path):
    database = tmp_path / "knowledge.db"
    _write_vector_database(database)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE metadata SET value=? WHERE key='chunker_version'",
            (str(CHUNKER_VERSION + 1),),
        )

    with pytest.raises(EvaluationUnavailable, match="chunker_version_mismatch"):
        _load_vector_corpus(tmp_path, model_id="fixture-model", dimensions=2)
