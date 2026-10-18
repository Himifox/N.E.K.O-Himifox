from __future__ import annotations

import json
import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace

from plugin.neko_plugin_cli.core import build_plugin
from plugin.neko_plugin_cli.core.inspect import inspect_package
from plugin.plugins.yui_stardew.installer import Installer, _file_version


def test_distributable_contains_verified_mod_license_and_setup(tmp_path):
    source = Path(__file__).resolve().parents[3] / "plugins" / "yui_stardew"
    output = tmp_path / "yui_stardew.neko-plugin"
    build_plugin(source, output)
    assert inspect_package(output).payload_hash_verified is True
    with zipfile.ZipFile(output) as package:
        prefix = "payload/plugins/yui_stardew/"
        for name in (
            "installer.py",
            "setup_ui.py",
            "ui/panel.tsx",
            "bundled/LICENSE.txt",
        ):
            assert prefix + name in package.namelist()
        # Read the actual distributable's bundle, not the developer's source tree.
        target = tmp_path / "unpacked"
        target.mkdir()
        for name in package.namelist():
            if name.startswith(prefix + "bundled/") and not name.endswith("/"):
                (target / Path(name).name).write_bytes(package.read(name))
        version, files = Installer(target)._bundle()
        assert version == json.loads(files["manifest.json"])["Version"]
        assert b"MIT License" in files["LICENSE"]
        assert "config.json" not in files
        assert all("config.json" not in name for name in package.namelist())


def test_unreadable_windows_version_resource_is_reported_as_unknown(monkeypatch):
    class VersionResourceError(Exception):
        pass

    def read_version(*_):
        raise VersionResourceError("No version resource")

    monkeypatch.setitem(
        sys.modules,
        "win32api",
        SimpleNamespace(
            GetFileVersionInfo=read_version,
            error=VersionResourceError,
        ),
    )
    assert _file_version(Path("corrupt.dll")) == ()
