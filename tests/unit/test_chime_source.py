from __future__ import annotations

import json
from importlib.resources import files
from pathlib import Path

from knowledge.moegirl_knowledge import MoegirlKnowledgeRetriever, MoegirlKnowledgeStore
from knowledge.moegirl_knowledge.sources.chime import (
    CHIME_COMMIT,
    CHIME_ENTRY_COUNT,
    CHIME_LICENSE,
    CHIME_SHA256,
    load_bundled_chime_dataset,
)
from knowledge.moegirl_knowledge.turn_context import build_meme_turn_context


def test_bundled_chime_dataset_has_pinned_integrity_and_provenance():
    dataset = load_bundled_chime_dataset()

    assert dataset.commit == CHIME_COMMIT
    assert dataset.sha256 == CHIME_SHA256
    assert len(dataset.entries) == CHIME_ENTRY_COUNT
    assert CHIME_LICENSE
    assert all("source:chime" in entry.tags for entry in dataset.entries)
    assert len({entry.content_hash for entry in dataset.entries}) == CHIME_ENTRY_COUNT


def test_bundled_chime_jsonl_has_one_object_per_line():
    raw = (
        files("knowledge.moegirl_knowledge.data")
        .joinpath("chime_full.jsonl")
        .read_text(encoding="utf-8")
    )
    lines = raw.splitlines()

    assert len(lines) == CHIME_ENTRY_COUNT
    assert all(line for line in lines)
    assert all(isinstance(json.loads(line), dict) for line in lines)


def test_bundled_chime_import_is_idempotent_and_searchable(tmp_path):
    dataset = load_bundled_chime_dataset()
    store = MoegirlKnowledgeStore(tmp_path / "knowledge.db")

    first = store.replace_source("source:chime", dataset.entries)
    second = store.replace_source("source:chime", dataset.entries)

    assert sum(result.created for result in first) == CHIME_ENTRY_COUNT
    assert sum(result.created for result in second) == CHIME_ENTRY_COUNT
    assert store.count() == CHIME_ENTRY_COUNT
    hits = MoegirlKnowledgeRetriever(store).search("treetree", limit=1)
    assert hits and hits[0].entry.source_tag == "source:chime"
    upper_context = build_meme_turn_context(
        "最近做这个方案越改越上头", store.database_path,
    )
    involution_context = build_meme_turn_context(
        "大家把日报写成论文，太内卷了", store.database_path,
    )
    assert upper_context.match_mode == "weak_short"
    assert "Term: 上头" in upper_context.text
    assert involution_context.match_mode == "weak_short"
    assert "Term: 内卷" in involution_context.text
    assert build_meme_turn_context(
        "她就这么水灵灵地把 bug 带上线了", store.database_path,
    ).match_mode == "none"


def test_fixed_response_quality_cases_keep_their_expected_routes(tmp_path):
    cases_path = (
        Path(__file__).parents[1]
        / "fixtures"
        / "knowledge_response_quality_cases.json"
    )
    cases = json.loads(cases_path.read_text(encoding="utf-8"))
    store = MoegirlKnowledgeStore(tmp_path / "knowledge.db")
    store.replace_source("source:chime", load_bundled_chime_dataset().entries)

    actual = {
        case["id"]: build_meme_turn_context(
            case["message"], store.database_path,
        ).match_mode
        for case in cases
    }

    assert actual == {case["id"]: case["expected_mode"] for case in cases}


def test_bundled_chime_marks_only_confirmed_stale_usage_entry():
    entries = load_bundled_chime_dataset().entries
    waterling_entries = [entry for entry in entries if entry.title == "水灵灵"]

    assert waterling_entries
    assert all("quality:stale-usage" in entry.tags for entry in waterling_entries)
    assert all(
        "quality:stale-usage" not in entry.tags
        for entry in entries
        if entry.title != "水灵灵"
    )
