import hashlib
import json
import zipfile
from pathlib import Path

import pytest

from scripts import unpack_builtin_pngtuber


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_production_pngtuber_packs_unpack_for_static_serving(tmp_path):
    manifest = json.loads(
        (PROJECT_ROOT / "frontend" / "pngtuber-packs" / "manifest.json").read_text(
            encoding="utf-8"
        )
    )
    output_root = tmp_path / "static" / "pngtuber"

    for model in manifest["models"]:
        target = unpack_builtin_pngtuber.unpack_model(
            model,
            PROJECT_ROOT / "frontend" / "pngtuber-packs",
            output_root,
        )
        assert (target / "model.json").is_file()
        assert (target / "metadata.pngtube-remix.json").is_file()
        assert (target / unpack_builtin_pngtuber.READY_MARKER).read_text(
            encoding="utf-8"
        ) == model["archive_sha256"]

    assert sorted(path.name for path in output_root.iterdir()) == [
        "yui-lolita",
        "yui-origin",
        "yui-sister",
    ]


def test_unpack_builtin_pngtuber_rejects_path_traversal(tmp_path):
    packs_root = tmp_path / "packs"
    packs_root.mkdir()
    archive_path = packs_root / "unsafe.zip"
    with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("../escape.png", b"escape")
    digest = hashlib.sha256(archive_path.read_bytes()).hexdigest()
    model = {
        "folder": "unsafe",
        "archive": archive_path.name,
        "archive_sha256": digest,
        "file_count": 1,
        "unpacked_size": len(b"escape"),
    }

    with pytest.raises(ValueError, match="unsafe archive path"):
        unpack_builtin_pngtuber.unpack_model(
            model,
            packs_root,
            tmp_path / "static" / "pngtuber",
        )

    assert not (tmp_path / "static" / "escape.png").exists()


def test_frontend_build_scripts_unpack_builtin_pngtuber_models():
    shell_build = (PROJECT_ROOT / "build_frontend.sh").read_text(encoding="utf-8")
    batch_build = (PROJECT_ROOT / "build_frontend.bat").read_text(encoding="utf-8")

    assert "scripts/unpack_builtin_pngtuber.py" in shell_build
    assert "scripts\\unpack_builtin_pngtuber.py" in batch_build
