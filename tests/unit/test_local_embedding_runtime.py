from __future__ import annotations


class _Service:
    def __init__(self, state: str):
        self.state = state

    def is_available(self):
        return self.state == "ready"

    def is_disabled(self):
        return self.state == "disabled"

    def disable_reason(self):
        return "fixture_disabled"

    def model_id(self):
        return "fixture-3d-fp32"

    def dim(self):
        return 3


def test_shared_local_embedding_status_is_domain_neutral(monkeypatch):
    from utils import local_embedding_runtime

    monkeypatch.setattr(
        local_embedding_runtime,
        "get_local_embedding_service",
        lambda: _Service("ready"),
    )
    status = local_embedding_runtime.get_local_embedding_status()
    assert status.ready
    assert status.model_id == "fixture-3d-fp32"
    assert status.dimensions == 3


def test_shared_local_embedding_status_reports_disabled_reason(monkeypatch):
    from utils import local_embedding_runtime

    monkeypatch.setattr(
        local_embedding_runtime,
        "get_local_embedding_service",
        lambda: _Service("disabled"),
    )
    status = local_embedding_runtime.get_local_embedding_status()
    assert status.state == "disabled"
    assert status.disable_reason == "fixture_disabled"


def test_knowledge_runtime_imports_no_memory_server_business_module():
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    for relative_path in (
        "knowledge/vector_index.py",
        "knowledge/indexer.py",
        "knowledge/service.py",
    ):
        source = (root / relative_path).read_text(encoding="utf-8")
        assert "app.memory_server" not in source
        assert "/internal/embeddings" not in source
