from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

from knowledge.prebuilt_index import PREBUILT_DIMENSIONS, PREBUILT_MODEL_ID
from knowledge.subscriptions import canonical_pack_bytes


SCRIPT_PATH = (
    Path(__file__).resolve().parents[2] / "scripts" / "build_knowledge_pack_index.py"
)
SPEC = importlib.util.spec_from_file_location("build_knowledge_pack_index", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def _pack_payload():
    return {
        "schema_version": 1,
        "pack_id": "publisher-fixture",
        "collection_id": "corpora",
        "source": {"name": "Fixture", "homepage": "", "license": "CC0"},
        "entries": [
            {
                "title": "Published fact",
                "terms": {"alias": [], "recognition": []},
                "tags": [],
                "summary": "A fact",
                "content": "The published fact has a grounded answer.",
            }
        ],
    }


class _EmbeddingService:
    async def request_load(self):
        return True

    def model_id(self):
        return PREBUILT_MODEL_ID

    def dim(self):
        return PREBUILT_DIMENSIONS

    async def embed_batch(self, texts):
        return [[1.0, *([0.0] * (PREBUILT_DIMENSIONS - 1))] for _ in texts]


def test_build_and_verify_prebuilt_sidecars(tmp_path, monkeypatch, capsys):
    pack_path = tmp_path / "fixture.neko-knowledge.json"
    pack_path.write_bytes(canonical_pack_bytes(_pack_payload()))
    released = []
    monkeypatch.setattr(MODULE, "get_local_embedding_service", _EmbeddingService)

    async def _release():
        released.append(True)

    monkeypatch.setattr(MODULE, "release_local_embedding_service", _release)

    assert MODULE.main([str(pack_path), "--output-dir", str(tmp_path)]) == 0
    built = json.loads(capsys.readouterr().out)
    manifest = Path(built["manifest"])
    vectors = Path(built["vectors"])
    assert manifest.is_file()
    assert vectors.is_file()
    assert released == [True]

    assert (
        MODULE.main(
            [
                str(pack_path),
                "--verify",
                "--manifest",
                str(manifest),
                "--vectors",
                str(vectors),
            ]
        )
        == 0
    )
    verified = json.loads(capsys.readouterr().out)
    assert verified["ok"] is True
    assert verified["chunk_count"] == 1


def test_builder_rejects_the_wrong_runtime_model(tmp_path, monkeypatch, capsys):
    pack_path = tmp_path / "fixture.neko-knowledge.json"
    pack_path.write_bytes(canonical_pack_bytes(_pack_payload()))
    service = _EmbeddingService()
    service.model_id = lambda: "another-model"
    monkeypatch.setattr(MODULE, "get_local_embedding_service", lambda: service)

    async def _release():
        return None

    monkeypatch.setattr(MODULE, "release_local_embedding_service", _release)

    assert MODULE.main([str(pack_path)]) == 2
    result = json.loads(capsys.readouterr().out)
    assert result["ok"] is False
    assert result["error_type"] == "RuntimeError"
