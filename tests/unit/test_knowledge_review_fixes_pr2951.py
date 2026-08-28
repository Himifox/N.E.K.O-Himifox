"""Review follow-ups on the public-knowledge PR: routing, status, artifacts, root."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from knowledge.api import open_knowledge
from knowledge.catalog_overrides import get_catalog_override_path
from knowledge.store import KnowledgeStoreError


def _pack(pack_id: str, title: str, *, material_type: str = "knowledge") -> dict:
    return {
        "schema_version": 1,
        "pack_id": pack_id,
        "material_type": material_type,
        "source": {"name": pack_id, "homepage": "", "license": "CC0"},
        "entries": [
            {
                "title": title,
                "terms": {"alias": [], "recognition": []},
                "tags": [],
                "summary": f"summary of {title}",
                "content": f"content of {title}",
            }
        ],
    }


def _supports_symlink(tmp_path: Path) -> bool:
    target = tmp_path / "_link_probe_target"
    target.mkdir()
    try:
        (tmp_path / "_link_probe").symlink_to(target, target_is_directory=True)
    except (OSError, NotImplementedError):
        return False
    return True


# --- routing: a failed load must not be published as a clean snapshot ----------


def test_routing_stays_dirty_when_override_load_fails(tmp_path):
    from knowledge.routing import KnowledgeRoutingState, RoutingConfig
    from knowledge.retrieval import KNOWLEDGE_MATCH_POLICY

    from knowledge.packs import validate_pack

    service = open_knowledge(tmp_path)
    service.install_pack(validate_pack(_pack("routed", "永动机")))

    database_path = service.database_path()
    state = KnowledgeRoutingState(
        RoutingConfig(database_path=database_path, policy=KNOWLEDGE_MATCH_POLICY)
    )

    # Healthy load first: routing finds the term.
    state.refresh()
    assert state.match("聊聊永动机") is not None

    # Corrupt the override file and force a reload.
    override = get_catalog_override_path(database_path)
    override.write_text("{ not json", encoding="utf-8")
    state.mark_database_dirty(database_path)
    state.refresh()

    # The failed load must NOT have been cached as clean, otherwise repairing
    # the file would never take effect (repair does not bump the generation).
    assert state._dirty is True

    override.unlink()
    state.refresh()
    assert state._dirty is False
    assert state.match("聊聊永动机") is not None


def test_safe_load_records_distinguishes_failure_from_empty(tmp_path):
    from knowledge import routing as routing_module
    from knowledge.retrieval import KNOWLEDGE_MATCH_POLICY
    from knowledge.routing import RoutingConfig, _safe_load_records

    service = open_knowledge(tmp_path)
    config = RoutingConfig(
        database_path=service.database_path(), policy=KNOWLEDGE_MATCH_POLICY
    )

    # Empty but healthy database -> () , not None.
    assert _safe_load_records(config) == ()

    def _boom(_config):
        raise ValueError("catalog override is unreadable or invalid")

    original = routing_module._load_records
    routing_module._load_records = _boom
    try:
        assert _safe_load_records(config) is None
    finally:
        routing_module._load_records = original


# --- status: malformed tags must degrade, not report ready with zero counts ----


def test_status_degrades_when_source_counts_cannot_be_computed(tmp_path, monkeypatch):
    from knowledge.packs import validate_pack

    service = open_knowledge(tmp_path)
    service.install_pack(validate_pack(_pack("counted", "词条")))

    healthy = service.get_status()
    assert healthy["integrity_ok"] is True
    assert healthy["entries"] == 1

    from knowledge.store import KnowledgeStore

    def _raise_when_strict(self, *, strict: bool = False):
        if strict:
            raise KnowledgeStoreError("malformed tags json")
        return ()

    monkeypatch.setattr(KnowledgeStore, "count_by_source_tags", _raise_when_strict)

    degraded = service.get_status()
    assert degraded["integrity_ok"] is False
    assert degraded["error_code"] == "knowledge_database_unavailable"


# --- staged artifacts: bounded, non-redirecting reads -------------------------


def test_staged_pack_artifact_rejects_a_symlink(tmp_path):
    from knowledge.pack_jobs import PACK_ARTIFACT_NAME, _load_job_pack

    if not _supports_symlink(tmp_path):
        pytest.skip("symlink creation is not permitted in this environment")

    external = tmp_path / "external.json"
    external.write_text(json.dumps(_pack("linked", "外部")), encoding="utf-8")

    job_dir = tmp_path / "job"
    job_dir.mkdir()
    os.symlink(external, job_dir / PACK_ARTIFACT_NAME)

    with pytest.raises(ValueError, match="not a regular file"):
        _load_job_pack(job_dir)


def test_staged_pack_artifact_rejects_an_oversized_file(tmp_path, monkeypatch):
    from knowledge import pack_jobs
    from knowledge.pack_jobs import PACK_ARTIFACT_NAME, _load_job_pack

    job_dir = tmp_path / "job"
    job_dir.mkdir()
    (job_dir / PACK_ARTIFACT_NAME).write_text(
        json.dumps(_pack("big", "大包")), encoding="utf-8"
    )

    monkeypatch.setattr(
        pack_jobs, "_staged_artifact_limits", lambda: {PACK_ARTIFACT_NAME: 8}
    )
    with pytest.raises(ValueError, match="exceeds its protocol limit"):
        _load_job_pack(job_dir)


def test_staged_pack_artifact_still_loads_a_normal_file(tmp_path):
    from knowledge.pack_jobs import PACK_ARTIFACT_NAME, _load_job_pack

    job_dir = tmp_path / "job"
    job_dir.mkdir()
    (job_dir / PACK_ARTIFACT_NAME).write_text(
        json.dumps(_pack("plain", "普通包")), encoding="utf-8"
    )

    assert _load_job_pack(job_dir).pack_id == "plain"


# --- live root: destructive mutation must refuse a redirected root ------------


def test_pack_removal_refuses_a_linked_knowledge_root(tmp_path):
    from knowledge.packs import validate_pack

    real_root = tmp_path / "real"
    real_root.mkdir()
    service = open_knowledge(real_root)
    service.install_pack(validate_pack(_pack("removable", "可删词条")))

    if not _supports_symlink(tmp_path):
        pytest.skip("symlink creation is not permitted in this environment")

    linked_root = tmp_path / "linked"
    os.symlink(real_root, linked_root, target_is_directory=True)

    redirected = open_knowledge(linked_root)
    with pytest.raises(KnowledgeStoreError):
        redirected.cancel_and_remove_pack("removable")

    # The pack in the real store is untouched.
    assert [p["pack_id"] for p in service.list_packs()] == ["removable"]

    # And removal through the real root still works.
    result = service.cancel_and_remove_pack("removable")
    assert result["removed_pack"] is True
    assert service.list_packs() == ()


def test_legacy_staged_pack_json_is_bounded_too(tmp_path, monkeypatch):
    """The legacy `pack.json` fallback is just as attacker-mutable as the new one.

    The first version of the fix bounded only the canonical artifact, so removing
    it and dropping an oversized/symlinked `pack.json` in its place walked
    straight back into the unbounded read.
    """
    from knowledge import pack_jobs
    from knowledge.pack_jobs import LEGACY_PACK_ARTIFACT_NAME, _load_job_pack

    job_dir = tmp_path / "job"
    job_dir.mkdir()
    (job_dir / LEGACY_PACK_ARTIFACT_NAME).write_text(
        json.dumps(_pack("legacy", "旧包")), encoding="utf-8"
    )

    # Normal legacy file still loads.
    assert _load_job_pack(job_dir).pack_id == "legacy"

    monkeypatch.setattr(
        pack_jobs, "_staged_artifact_limits", lambda: {LEGACY_PACK_ARTIFACT_NAME: 8}
    )
    with pytest.raises(ValueError, match="exceeds its protocol limit"):
        _load_job_pack(job_dir)


def test_legacy_staged_pack_json_rejects_a_symlink(tmp_path):
    from knowledge.pack_jobs import LEGACY_PACK_ARTIFACT_NAME, _load_job_pack

    if not _supports_symlink(tmp_path):
        pytest.skip("symlink creation is not permitted in this environment")

    external = tmp_path / "external.json"
    external.write_text(json.dumps(_pack("linked-legacy", "外部旧包")), encoding="utf-8")

    job_dir = tmp_path / "job"
    job_dir.mkdir()
    os.symlink(external, job_dir / LEGACY_PACK_ARTIFACT_NAME)

    with pytest.raises(ValueError, match="not a regular file"):
        _load_job_pack(job_dir)


def test_staged_artifact_read_is_bound_to_the_validated_descriptor(
    tmp_path, monkeypatch
):
    """Validation and read must share one descriptor, not two path lookups.

    Checking the path and then calling read_bytes() re-opens by name, so a writer
    that swaps the artifact in between defeats both the link refusal and the size
    cap. Here the swap is fired from inside fstat: a path-based implementation
    never calls os.fstat at all, and one that re-opens would pick up the decoy.
    """
    from knowledge.pack_jobs import PACK_ARTIFACT_NAME, _load_job_pack

    job_dir = tmp_path / "job"
    job_dir.mkdir()
    target = job_dir / PACK_ARTIFACT_NAME
    target.write_bytes(json.dumps(_pack("original", "原包")).encode("utf-8"))

    decoy = tmp_path / "decoy.json"
    decoy.write_bytes(json.dumps(_pack("swapped", "掉包")).encode("utf-8"))

    real_fstat = os.fstat
    swapped = {"done": False, "possible": True}

    def fstat_then_swap(fd):
        info = real_fstat(fd)
        if not swapped["done"]:
            swapped["done"] = True
            try:
                os.replace(decoy, target)
            except OSError:
                # Windows refuses to replace a file that is currently open.
                swapped["possible"] = False
        return info

    monkeypatch.setattr(os, "fstat", fstat_then_swap)
    pack = _load_job_pack(job_dir)

    # A path-based implementation would never have reached os.fstat.
    assert swapped["done"], "artifact was not validated through a descriptor"
    if not swapped["possible"]:
        pytest.skip("this platform cannot swap an open file; descriptor use verified")
    assert pack.pack_id == "original"


def test_staged_artifact_reader_never_reopens_by_path():
    """Structural dual: the fix is 'do not look the path up a second time'."""
    import ast
    import inspect
    import textwrap

    from knowledge.pack_jobs import _read_staged_artifact

    source = textwrap.dedent(inspect.getsource(_read_staged_artifact))
    tree = ast.parse(source)
    called = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }

    assert "read_bytes" not in called and "read_text" not in called, (
        "re-opening the artifact by path after validating it reintroduces the "
        "swap window the descriptor was meant to close"
    )
    assert {"open", "fstat", "read", "close"} <= called, (
        f"expected the descriptor-based read to survive; saw {sorted(called)}"
    )
