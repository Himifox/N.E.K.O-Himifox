from __future__ import annotations

import json
import logging
from types import SimpleNamespace

import pytest

from knowledge.moegirl_knowledge.bundled_chime_runtime import (
    _import_bundled_chime,
    get_public_knowledge_status,
    request_bundled_chime_reimport,
)
from knowledge.moegirl_knowledge import MoegirlKnowledgeEntry, MoegirlKnowledgeStore
from knowledge.moegirl_knowledge.sources.chime import CHIME_ENTRY_COUNT


@pytest.mark.asyncio
async def test_bundled_chime_runtime_import_records_independent_state(tmp_path):
    database_path = tmp_path / "knowledge.db"
    state_path = tmp_path / "chime_state.json"

    await _import_bundled_chime(database_path, state_path, logging.getLogger("test.chime"))

    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["status"] == "ready"
    assert state["entries"] == CHIME_ENTRY_COUNT
    assert state["added"] == CHIME_ENTRY_COUNT
    assert MoegirlKnowledgeStore(database_path).count() == CHIME_ENTRY_COUNT


def test_status_keeps_chime_and_moegirl_degradation_independent(tmp_path):
    class _Config:
        knowledge_dir = tmp_path

    root = tmp_path / "moegirl-knowledge"
    store = MoegirlKnowledgeStore(root / "knowledge.db")
    store.upsert(MoegirlKnowledgeEntry(
        id="chime:one", title="chime", content="content", summary="summary",
        source_url="https://example.test/chime", source_license="MIT",
    ))
    store.upsert(MoegirlKnowledgeEntry(
        id="moegirl:one", title="moegirl", content="content", summary="summary",
        source_url="https://example.test/moegirl",
    ))
    (root / "chime_state.json").write_text(
        json.dumps({"status": "ready", "last_success_at": "2026-07-20T00:00:00Z"}),
        encoding="utf-8",
    )
    (root / "sync_state.json").write_text(
        json.dumps({"status": "degraded", "failed": 3}), encoding="utf-8",
    )

    status = get_public_knowledge_status(_Config())

    assert status["database"] == {"entries": 2, "integrity_ok": True}
    assert status["sources"]["chime"]["status"] == "ready"
    assert status["sources"]["chime"]["entries"] == 1
    assert status["sources"]["moegirl"] == {
        "status": "degraded", "entries": 1, "last_success_at": "", "failed": 3,
    }


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
    assert runtime._chime_task is not None
    await runtime._chime_task
    assert completed is True
    runtime._chime_task = None


@pytest.mark.asyncio
async def test_remote_sync_starts_after_a_reply_trigger_not_server_start(monkeypatch, tmp_path):
    import app.main_server.moegirl_knowledge_runtime as runtime

    triggered = False

    async def _fake_run():
        nonlocal triggered
        triggered = True

    async def _fake_stop_chime():
        return None

    monkeypatch.setattr(runtime, "schedule_bundled_chime_import", lambda *_args: "scheduled")
    monkeypatch.setattr(runtime, "stop_bundled_chime_import", _fake_stop_chime)
    monkeypatch.setattr(runtime, "_run_sync_loop", _fake_run)
    monkeypatch.setattr(runtime, "_sync_task", None)
    monkeypatch.setattr(runtime, "_synchronizer", None)
    monkeypatch.setattr(runtime, "_stop_event", None)
    monkeypatch.setattr(runtime, "_remove_post_reply_hook", None)
    config = SimpleNamespace(knowledge_dir=tmp_path, ensure_knowledge_directory=lambda: True)

    await runtime.start_moegirl_knowledge_sync(config, logging.getLogger("test.runtime"))

    assert runtime._sync_task is None
    runtime.request_moegirl_knowledge_sync()
    assert runtime._sync_task is not None
    await runtime._sync_task
    assert triggered is True

    await runtime.stop_moegirl_knowledge_sync()
