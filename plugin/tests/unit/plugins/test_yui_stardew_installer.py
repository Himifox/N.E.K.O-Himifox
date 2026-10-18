from __future__ import annotations

import hashlib
import json
import shutil
import zipfile
from pathlib import Path

import pytest

from plugin.plugins.yui_stardew import installer


MOD_ID = "Himifox.YuiToIssho"
VERSION = "0.31.0"


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def _snapshot(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


def _make_bundle(root: Path, *, extra: dict[str, str] | None = None) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    archive = root / f"YuiToIssho-{VERSION}.zip"
    manifest = {
        "UniqueID": MOD_ID,
        "Version": VERSION,
        "EntryDll": "YuiToIssho.dll",
        "MinimumApiVersion": "4.5.2",
    }
    with zipfile.ZipFile(archive, "w") as package:
        package.writestr("YuiToIssho/manifest.json", json.dumps(manifest))
        package.writestr("YuiToIssho/YuiToIssho.dll", b"test packaged assembly")
        package.writestr("YuiToIssho/i18n/default.json", '{"hello":"Hello"}')
        for name, content in (extra or {}).items():
            package.writestr(name, content)
    _write_json(
        root / "manifest.json",
        {
            "version": VERSION,
            "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
            "archive": archive.name,
            "protocol_version": 2,
        },
    )
    return root


def _make_existing_mod(game: Path, *, version: str = "0.30.0") -> Path:
    mod = game / "Mods" / "YuiToIssho"
    _write_json(
        mod / "manifest.json",
        {"UniqueID": MOD_ID, "Version": version, "EntryDll": "YuiToIssho.dll"},
    )
    (mod / "YuiToIssho.dll").write_bytes(b"previous assembly")
    (mod / "user-notes.txt").write_text("keep in backup", encoding="utf-8")
    return mod


@pytest.fixture
def game(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    steamapps = tmp_path / "SteamLibrary" / "steamapps"
    game = steamapps / "common" / "Stardew Valley"
    game.mkdir(parents=True)
    (steamapps / "appmanifest_413150.acf").write_text(
        '"AppState"\n{\n "appid" "413150"\n "installdir" "Stardew Valley"\n}',
        encoding="utf-8",
    )
    for name in (
        "Stardew Valley.exe",
        "Stardew Valley.dll",
        "StardewModdingAPI.exe",
        "StardewModdingAPI.dll",
        "smapi-internal/SMAPI.Toolkit.dll",
    ):
        path = game / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"test executable")
    monkeypatch.setattr(installer, "_windows_supported", lambda: True)
    monkeypatch.setattr(installer, "game_running", lambda path: False)
    monkeypatch.setattr(
        installer,
        "_file_version",
        lambda path: (
            (4, 5, 2, 0)
            if Path(path).name == "StardewModdingAPI.dll"
            else (1, 6, 15, 0)
        ),
    )
    return game


@pytest.fixture
def setup(tmp_path: Path) -> installer.Installer:
    return installer.Installer(bundle_dir=_make_bundle(tmp_path / "bundle"))


def test_install_enables_bridge_without_replacing_user_configuration(game, setup):
    mod = _make_existing_mod(game)
    config = {
        "EnableNekoBridge": False,
        "NekoBridgeToken": "test-only-private-token",
        "EnableExperimentalFeatures": False,
        "PlayerName": "结衣",
        "UnrecognizedSetting": {"nested": [1, True, "preserve"]},
    }
    _write_json(mod / "config.json", config)
    previous_files = _snapshot(mod)

    result = setup.install(str(game))

    assert result["version"] == VERSION
    assert json.loads((mod / "config.json").read_text(encoding="utf-8")) == {
        **config,
        "EnableNekoBridge": True,
    }
    assert (mod / "YuiToIssho.dll").read_bytes() == b"test packaged assembly"
    backup = Path(result["backup_path"])
    assert backup.is_relative_to(game / ".yuitoissho-backups")
    assert _snapshot(backup) == previous_files


def test_fresh_install_and_repeated_install_are_idempotent(game, setup):
    first = setup.install(str(game))
    mod = game / "Mods" / "YuiToIssho"
    before = _snapshot(mod)
    backups = _snapshot(game / ".yuitoissho-backups")

    second = setup.install(str(game))

    assert first["version"] == second["version"] == VERSION
    assert not second.get("backup_path")
    assert _snapshot(mod) == before
    assert _snapshot(game / ".yuitoissho-backups") == backups
    assert json.loads((mod / "config.json").read_text())["EnableNekoBridge"] is True


@pytest.mark.parametrize(
    "config_text",
    [
        '{"EnableNekoBridge":',
        "[]",
        "null",
        '{"EnableNekoBridge":false,"Other":NaN}',
        '{"EnableNekoBridge":false,"Other":Infinity}',
        '{"EnableNekoBridge":false,"EnableNekoBridge":true}',
    ],
)
@pytest.mark.parametrize("operation", ["install", "enable_bridge"])
def test_invalid_config_is_rejected_without_changing_mod(
    game, setup, config_text, operation
):
    mod = _make_existing_mod(game)
    (mod / "config.json").write_text(config_text, encoding="utf-8")
    before = _snapshot(mod)

    with pytest.raises(installer.SetupError):
        getattr(setup, operation)(str(game))

    assert _snapshot(mod) == before


@pytest.mark.parametrize("operation", ["install", "enable_bridge"])
def test_running_game_blocks_modification(game, setup, monkeypatch, operation):
    mod = _make_existing_mod(game)
    _write_json(mod / "config.json", {"EnableNekoBridge": False})
    before = _snapshot(mod)
    monkeypatch.setattr(installer, "game_running", lambda path: True)

    with pytest.raises(installer.SetupError):
        getattr(setup, operation)(str(game))

    assert _snapshot(mod) == before


def test_enable_bridge_preserves_unknown_settings_and_token(game, setup):
    setup.install(str(game))
    mod = game / "Mods" / "YuiToIssho"
    config = {
        "EnableNekoBridge": False,
        "NekoBridgeToken": "existing-test-token",
        "Other": [1, 2],
    }
    _write_json(mod / "config.json", config)
    assembly_before = (mod / "YuiToIssho.dll").read_bytes()

    setup.enable_bridge(str(game))

    assert json.loads((mod / "config.json").read_text()) == {
        **config,
        "EnableNekoBridge": True,
    }
    assert (mod / "YuiToIssho.dll").read_bytes() == assembly_before


def test_downgrade_is_rejected_without_modification(game, setup):
    mod = _make_existing_mod(game, version="0.32.0")
    before = _snapshot(mod)

    with pytest.raises(installer.SetupError):
        setup.install(str(game))

    assert _snapshot(mod) == before


def test_another_directory_with_same_mod_id_is_rejected(game, setup):
    other = game / "Mods" / "CustomName" / "manifest.json"
    _write_json(other, {"UniqueID": MOD_ID, "Version": "0.30.0"})
    before = _snapshot(game / "Mods")

    with pytest.raises(installer.SetupError):
        setup.install(str(game))

    assert _snapshot(game / "Mods") == before


@pytest.mark.parametrize("manifest_name", ["Manifest.json", "MANIFEST.JSON"])
def test_duplicate_mod_detection_is_case_insensitive(game, setup, manifest_name):
    other = game / "Mods" / "OtherYui" / manifest_name
    _write_json(other, {"UniqueID": MOD_ID, "Version": "0.30.0"})
    before = _snapshot(game / "Mods")

    with pytest.raises(installer.SetupError) as error:
        setup.install(str(game))

    assert error.value.code == "MOD-DUPLICATE"
    assert _snapshot(game / "Mods") == before


def test_disabled_mod_directory_does_not_block_install_or_get_modified(game, setup):
    disabled = game / "Mods" / ".disabled"
    _write_json(
        disabled / "Yui" / "manifest.json", {"UniqueID": MOD_ID, "Version": "0.30.0"}
    )
    (disabled / "Yui" / "user-notes.txt").write_text(
        "keep disabled backup", encoding="utf-8"
    )
    before = _snapshot(disabled)

    result = setup.install(str(game))

    assert result["version"] == VERSION
    assert _snapshot(disabled) == before
    assert (game / "Mods" / "YuiToIssho" / "YuiToIssho.dll").is_file()


def test_manifest_examples_inside_another_mod_are_not_active_duplicates(game, setup):
    other = game / "Mods" / "OtherMod"
    _write_json(
        other / "manifest.json", {"UniqueID": "Other.AuthorMod", "Version": "1.0.0"}
    )
    _write_json(
        other / "examples" / "Yui" / "manifest.json",
        {"UniqueID": MOD_ID, "Version": "0.30.0"},
    )
    before = _snapshot(other)

    result = setup.install(str(game))

    assert result["version"] == VERSION
    assert _snapshot(other) == before


def test_target_mod_hidden_symlink_is_rejected_even_when_disabled_folders_are_ignored(
    game, setup, tmp_path
):
    mod = _make_existing_mod(game)
    external = tmp_path / "external-user-data"
    external.mkdir()
    (external / "keep.txt").write_text(
        "must not be copied or modified", encoding="utf-8"
    )
    try:
        (mod / ".hidden-link").symlink_to(external, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"This environment cannot create directory symlinks: {error}")
    before = _snapshot(external)
    original_assembly = (mod / "YuiToIssho.dll").read_bytes()

    with pytest.raises(installer.SetupError) as error:
        setup.install(str(game))

    assert error.value.code == "PATH-REDIRECTED"
    assert _snapshot(external) == before
    assert (mod / "YuiToIssho.dll").read_bytes() == original_assembly


def test_bundle_checksum_mismatch_does_not_modify_existing_mod(game, tmp_path):
    bundle = _make_bundle(tmp_path / "bundle")
    with (bundle / f"YuiToIssho-{VERSION}.zip").open("ab") as stream:
        stream.write(b"tampered")
    mod = _make_existing_mod(game)
    before = _snapshot(mod)

    with pytest.raises(installer.SetupError):
        installer.Installer(bundle_dir=bundle).install(str(game))

    assert _snapshot(mod) == before


@pytest.mark.parametrize(
    "entry", ["../escaped.txt", "YuiToIssho/../../escaped.txt", "C:/escaped.txt"]
)
def test_unsafe_archive_paths_are_rejected_before_modification(game, tmp_path, entry):
    bundle = _make_bundle(tmp_path / "bundle", extra={entry: "must not be extracted"})
    mod = _make_existing_mod(game)
    before = _snapshot(mod)

    with pytest.raises(installer.SetupError):
        installer.Installer(bundle_dir=bundle).install(str(game))

    assert _snapshot(mod) == before
    assert not (game / "escaped.txt").exists()


def test_selected_path_must_match_steam_app_manifest(game, setup):
    assert Path(setup.select_game(str(game))) == game.resolve()
    (game.parents[1] / "appmanifest_413150.acf").write_text(
        '"AppState" { "appid" "413150" "installdir" "Another Game" }',
        encoding="utf-8",
    )

    with pytest.raises(installer.SetupError):
        setup.select_game(str(game))


@pytest.mark.parametrize(
    ("dll", "version"),
    [
        ("Stardew Valley.dll", (1, 6, 14, 0)),
        ("Stardew Valley.dll", (1, 7, 0, 0)),
        ("StardewModdingAPI.dll", (4, 5, 1, 0)),
        ("StardewModdingAPI.dll", (5, 0, 0, 0)),
    ],
)
def test_incompatible_game_or_smapi_version_blocks_install(
    game, setup, monkeypatch, dll, version
):
    mod = _make_existing_mod(game)
    before = _snapshot(mod)

    def file_version(path):
        if Path(path).name == dll:
            return version
        return (
            (4, 5, 2, 0)
            if Path(path).name == "StardewModdingAPI.dll"
            else (1, 6, 15, 0)
        )

    monkeypatch.setattr(installer, "_file_version", file_version)

    with pytest.raises(installer.SetupError):
        setup.install(str(game))

    assert _snapshot(mod) == before


@pytest.mark.parametrize(
    "missing",
    [
        "StardewModdingAPI.exe",
        "StardewModdingAPI.dll",
        "smapi-internal/SMAPI.Toolkit.dll",
    ],
)
def test_missing_smapi_files_block_install_without_mod_changes(game, setup, missing):
    (game / missing).unlink()
    mod = _make_existing_mod(game)
    before = _snapshot(mod)

    with pytest.raises(installer.SetupError):
        setup.install(str(game))

    assert _snapshot(mod) == before


@pytest.mark.parametrize("operation", ["install", "enable_bridge", "launch"])
def test_unsupported_platform_blocks_actions(game, setup, monkeypatch, operation):
    monkeypatch.setattr(installer, "_windows_supported", lambda: False)
    before = _snapshot(game)

    with pytest.raises(installer.SetupError):
        getattr(setup, operation)(str(game))

    assert _snapshot(game) == before


def test_launch_uses_smapi_without_shell_or_real_process(game, setup, monkeypatch):
    setup.install(str(game))
    calls = []

    def fake_popen(*args, **kwargs):
        calls.append((args, kwargs))
        return type("Process", (), {"pid": 12345})()

    monkeypatch.setattr(installer.subprocess, "Popen", fake_popen)

    setup.launch(str(game))

    assert len(calls) == 1
    args, kwargs = calls[0]
    assert Path(args[0][0]) == game / "StardewModdingAPI.exe"
    assert Path(kwargs["cwd"]) == game
    assert not kwargs.get("shell", False)


def test_activation_failure_restores_previous_mod_and_cleans_stage(
    game, setup, monkeypatch
):
    mod = _make_existing_mod(game)
    _write_json(
        mod / "config.json", {"EnableNekoBridge": False, "Unrelated": "preserve"}
    )
    before = _snapshot(mod)
    original_rename = Path.rename

    def fail_activation(path, destination):
        if path.name.startswith(".yuitoissho-stage-") and Path(destination) == mod:
            raise OSError("simulated activation failure")
        return original_rename(path, destination)

    monkeypatch.setattr(Path, "rename", fail_activation)

    with pytest.raises(installer.SetupError):
        setup.install(str(game))

    assert _snapshot(mod) == before
    assert not list(game.glob(".yuitoissho-stage-*"))


def test_staging_write_failure_preserves_previous_mod(game, setup, monkeypatch):
    mod = _make_existing_mod(game)
    before = _snapshot(mod)
    original_write = Path.write_bytes

    def fail_stage_write(path, data):
        if path.name == "YuiToIssho.dll" and path.parent.name.startswith(
            ".yuitoissho-stage-"
        ):
            raise OSError("simulated staging write failure")
        return original_write(path, data)

    monkeypatch.setattr(Path, "write_bytes", fail_stage_write)

    with pytest.raises(installer.SetupError):
        setup.install(str(game))

    assert _snapshot(mod) == before
    assert not list(game.glob(".yuitoissho-stage-*"))


@pytest.mark.parametrize("operation", ["install", "enable_bridge"])
@pytest.mark.parametrize("change", ["config", "added_file", "removed_file"])
def test_changes_during_staging_are_not_overwritten(game, setup, monkeypatch, operation, change):
    setup.install(str(game))
    mod = game / "Mods" / "YuiToIssho"
    _write_json(mod / "config.json", {"EnableNekoBridge": False, "Other": "before"})
    (mod / "user-notes.txt").write_text("user data", encoding="utf-8")
    original_copy = shutil.copytree
    latest = None

    def edit_after_copy(source, destination, *args, **kwargs):
        nonlocal latest
        result = original_copy(source, destination, *args, **kwargs)
        if Path(source) == mod:
            if change == "config":
                _write_json(mod / "config.json", {"EnableNekoBridge": False, "Other": "edited"})
            elif change == "added_file":
                (mod / "new-user-file.txt").write_text("keep the latest edit", encoding="utf-8")
            else:
                (mod / "user-notes.txt").unlink()
            latest = _snapshot(mod)
        return result

    monkeypatch.setattr(shutil, "copytree", edit_after_copy)

    with pytest.raises(installer.SetupError) as error:
        getattr(setup, operation)(str(game))

    assert error.value.code == "MOD-CHANGED"
    assert latest is not None and _snapshot(mod) == latest
    assert not list(game.glob(".yuitoissho-stage-*"))


def test_new_installation_appearing_before_activation_is_preserved(game, setup, monkeypatch):
    original_idle = setup._idle
    latest = None
    calls = 0

    def install_before_activation(path):
        nonlocal latest, calls
        original_idle(path)
        calls += 1
        if calls == 2:
            mod = _make_existing_mod(game)
            _write_json(mod / "config.json", {"EnableNekoBridge": False, "Other": "new"})
            latest = _snapshot(mod)

    monkeypatch.setattr(setup, "_idle", install_before_activation)

    with pytest.raises(installer.SetupError) as error:
        setup.install(str(game))

    assert error.value.code == "MOD-CHANGED"
    assert latest is not None and _snapshot(game / "Mods/YuiToIssho") == latest


def test_failed_automatic_restore_reports_the_exact_intact_backup(game, setup, monkeypatch):
    mod = _make_existing_mod(game)
    _write_json(mod / "config.json", {"EnableNekoBridge": False, "Other": "keep"})
    before = _snapshot(mod)
    original_rename = Path.rename

    def fail_activation_and_restore(path, destination):
        if Path(destination) == mod and (
            path.name.startswith(".yuitoissho-stage-") or path.parent.name == ".yuitoissho-backups"
        ):
            raise OSError("simulated file lock")
        return original_rename(path, destination)

    monkeypatch.setattr(Path, "rename", fail_activation_and_restore)

    with pytest.raises(installer.SetupError) as error:
        setup.install(str(game))

    backups = list((game / ".yuitoissho-backups").iterdir())
    assert len(backups) == 1 and _snapshot(backups[0]) == before
    assert error.value.code == "MOD-RESTORE-FAILED"
    assert str(backups[0]) in error.value.message


def test_unreadable_mod_subdirectory_blocks_install(game, setup, monkeypatch):
    mod = _make_existing_mod(game)
    before = _snapshot(mod)
    original_walk = installer.os.walk

    def inaccessible_walk(path, **kwargs):
        if Path(path) == game / "Mods":
            if kwargs.get("onerror"):
                kwargs["onerror"](PermissionError("unreadable mod directory"))
            return iter(())
        return original_walk(path, **kwargs)

    monkeypatch.setattr(installer.os, "walk", inaccessible_walk)

    with pytest.raises(installer.SetupError) as error:
        setup.install(str(game))

    assert error.value.code == "MOD-SCAN-FAILED"
    assert _snapshot(mod) == before


def test_full_backup_restores_old_install_without_leaving_new_files(game, setup):
    mod = _make_existing_mod(game)
    config = {"EnableNekoBridge": False, "NekoBridgeToken": "test-stable-key", "Extra": [1, 2]}
    _write_json(mod / "config.json", config)
    before = _snapshot(mod)

    result = setup.install(str(game))
    backup = Path(result["backup_path"])
    assert (mod / "i18n/default.json").is_file()
    retained = game / "retained-new-install"
    assert mod.resolve().is_relative_to(game.resolve())
    assert retained.resolve().is_relative_to(game.resolve())
    mod.rename(retained)
    shutil.copytree(backup, mod)

    assert _snapshot(mod) == before
    assert _snapshot(backup) == before
    assert not (mod / "i18n/default.json").exists()
    assert setup.diagnose(str(game))["code"] == "MOD-INSTALL-REQUIRED"
    setup.install(str(game))
    assert json.loads((mod / "config.json").read_text()) == {**config, "EnableNekoBridge": True}


def test_diagnosis_is_read_only_and_reports_missing_smapi(game, setup, monkeypatch):
    monkeypatch.setattr(installer, "discover_steam_games", lambda: [game])
    (game / "StardewModdingAPI.exe").unlink()
    before = _snapshot(game)

    state = setup.diagnose()

    assert state["code"] == "SMAPI-MISSING"
    assert Path(state["selected_path"]) == game
    assert not state["can_install"]
    assert not state["can_enable_bridge"]
    assert not state["can_launch"]
    assert _snapshot(game) == before


def test_diagnosis_requires_explicit_selection_for_multiple_games(
    game, setup, monkeypatch
):
    monkeypatch.setattr(
        installer, "discover_steam_games", lambda: [game, game.parent / "Other"]
    )
    before = _snapshot(game)

    state = setup.diagnose()

    assert state["code"] == "GAME-SELECTION-REQUIRED"
    assert not state["can_install"]
    assert not state["can_launch"]
    assert _snapshot(game) == before


def test_discovery_follows_steam_libraries_on_other_disks_and_deduplicates(
    game, tmp_path, monkeypatch
):
    primary = game.parents[2]
    secondary = tmp_path / "AdditionalLibrary"
    shutil.copytree(primary / "steamapps", secondary / "steamapps")
    paths = [primary, secondary, secondary]
    entries = "\n".join(
        f'"{index}" {{ "path" {json.dumps(str(path))} }}'
        for index, path in enumerate(paths)
    )
    (primary / "steamapps" / "libraryfolders.vdf").write_text(
        f'"libraryfolders" {{ {entries} }}', encoding="utf-8"
    )
    monkeypatch.setattr(installer, "_steam_roots", lambda: [primary])

    games = installer.discover_steam_games()

    assert set(games) == {game, secondary / "steamapps" / "common" / game.name}
    assert len(games) == 2
