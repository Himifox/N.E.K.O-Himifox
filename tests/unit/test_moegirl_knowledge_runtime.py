from __future__ import annotations

import json
import logging
from types import SimpleNamespace

import pytest

from knowledge.moegirl_knowledge import MoegirlKnowledgeEntry, MoegirlKnowledgeStore
from knowledge.moegirl_knowledge.bundled_chime_runtime import (
    _import_bundled_chime,
    get_public_knowledge_status,
    request_bundled_chime_reimport,
)
from knowledge.moegirl_knowledge.sources.chime import CHIME_ENTRY_COUNT


@pytest.mark.asyncio
async def test_bundled_chime_runtime_import_records_independent_state(tmp_path):
    database_path = tmp_path / "knowledge.db"
    state_path = tmp_path / "chime_state.json"

    await _import_bundled_chime(database_path, state_path, logging.getLogger("test.chime"))

    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["status"] == "ready"
    assert state["entries"] == CHIME_ENTRY_COUNT
    assert MoegirlKnowledgeStore(database_path).count() == CHIME_ENTRY_COUNT


def test_status_reports_live_local_counts_and_isolated_remote_sources(tmp_path):
    config = SimpleNamespace(knowledge_dir=tmp_path)
    root = tmp_path / "moegirl-knowledge"
    store = MoegirlKnowledgeStore(root / "knowledge.db")
    store.upsert(MoegirlKnowledgeEntry(
        title="local card",
        terms={"alias": (), "recognition": ()},
        tags=("source:chime",),
        summary="summary",
        content="content",
    ))
    (root / "chime_state.json").write_text(
        json.dumps({"status": "ready", "last_success_at": "2026-07-20T00:00:00Z"}),
        encoding="utf-8",
    )

    status = get_public_knowledge_status(config)

    assert status["mode"] == "local_only"
    assert status["remote_acquisition"] == "isolated"
    assert status["database"] == {
        "entries": 1, "active_entries": 1, "disabled_entries": 0, "integrity_ok": True,
    }
    assert status["sources"]["chime"]["entries"] == 1
    assert status["sources"]["geng8"]["acquisition"] == "isolated"
    assert status["sources"]["moegirl"]["acquisition"] == "isolated"


@pytest.mark.asyncio
async def test_manual_reimport_schedules_only_one_background_task(monkeypatch, tmp_path):
    import knowledge.moegirl_knowledge.bundled_chime_runtime as runtime

    completed = False

    async def _fake_import(database_path, state_path, logger):
        nonlocal completed
        assert database_path == tmp_path / "moegirl-knowledge" / "knowledge.db"
        assert state_path == tmp_path / "moegirl-knowledge" / "chime_state.json"
        completed = True

    monkeypatch.setattr(runtime, "_import_bundled_chime", _fake_import)
    monkeypatch.setattr(runtime, "_chime_task", None)
    config = SimpleNamespace(knowledge_dir=tmp_path, ensure_knowledge_directory=lambda: True)

    assert request_bundled_chime_reimport(config, logging.getLogger("test.chime")) == "scheduled"
    assert request_bundled_chime_reimport(config, logging.getLogger("test.chime")) == "already_running"
    await runtime._chime_task
    assert completed is True
    runtime._chime_task = None


@pytest.mark.asyncio
async def test_main_runtime_only_schedules_bundled_local_import(monkeypatch, tmp_path):
    import app.main_server.moegirl_knowledge_runtime as runtime

    scheduled = []
    stopped = []

    monkeypatch.setattr(runtime, "schedule_bundled_chime_import", lambda *_args: scheduled.append(True))

    async def _stop():
        stopped.append(True)

    monkeypatch.setattr(runtime, "stop_bundled_chime_import", _stop)
    config = SimpleNamespace(knowledge_dir=tmp_path, ensure_knowledge_directory=lambda: True)

    await runtime.start_moegirl_knowledge_runtime(config, logging.getLogger("test.runtime"))
    await runtime.stop_moegirl_knowledge_runtime()

    assert scheduled == [True]
    assert stopped == [True]
    assert not hasattr(runtime, "submit_public_meme_candidate")
    assert not hasattr(runtime, "request_moegirl_knowledge_sync")
