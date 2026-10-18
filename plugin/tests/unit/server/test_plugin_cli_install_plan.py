from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from plugin.neko_plugin_cli.public import build_bundle, build_plugin
from plugin.server.application.plugin_cli.install_plan import build_install_plan, confirmation_token
from plugin.server.application.plugin_cli.service import PluginCliService
from plugin.server.application.plugin_cli import service as plugin_cli_service
from plugin.server.application.install_source import InstallSourceError
from plugin.server.domain.errors import ServerDomainError

pytestmark = pytest.mark.plugin_unit


def _write_plugin(
    root: Path,
    plugin_id: str,
    version: str,
    previous_ids: tuple[str, ...] = (),
) -> Path:
    plugin_dir = root / plugin_id
    plugin_dir.mkdir(parents=True)
    previous_line = f"previous_ids = {json.dumps(list(previous_ids))}\n" if previous_ids else ""
    (plugin_dir / "plugin.toml").write_text(
        "".join(
            [
                "[plugin]\n",
                f'id = "{plugin_id}"\n',
                f'name = "{plugin_id}"\n',
                f'version = "{version}"\n',
                'type = "plugin"\n',
                previous_line,
                f"\n[{plugin_id}]\n",
                "enabled = true\n",
            ]
        ),
        encoding="utf-8",
    )
    (plugin_dir / "__init__.py").write_text("VALUE = 1\n", encoding="utf-8")
    return plugin_dir


def _single_package(tmp_path: Path, plugin_id: str, version: str = "2.0.0") -> Path:
    package_path = tmp_path / f"{plugin_id}-{version}.neko-plugin"
    build_plugin(_write_plugin(tmp_path / "source", plugin_id, version), package_path)
    return package_path


def test_plan_marks_new_single_plugin_as_install(tmp_path: Path) -> None:
    plan = build_install_plan(
        package_path=_single_package(tmp_path, "demo"),
        plugins_root=tmp_path / "plugins",
    )

    assert plan.action == "install"
    assert plan.plugin_id == "demo"
    assert plan.confirmation_token == ""


def test_plan_marks_matching_existing_plugin_as_upgrade(tmp_path: Path) -> None:
    package = _single_package(tmp_path, "demo", version="2.0.0")
    _write_plugin(tmp_path / "plugins", plugin_id="demo", version="1.0.0")

    plan = build_install_plan(package_path=package, plugins_root=tmp_path / "plugins")

    assert plan.action == "upgrade"
    assert plan.current_version == "1.0.0"
    assert plan.target_version == "2.0.0"
    assert len(plan.confirmation_token) == 64


def _runtime_config(plugins_root: Path, plugin_id: str = "demo") -> Path:
    config = plugins_root / plugin_id / "config" / "plugin.toml"
    config.parent.mkdir(parents=True)
    config.write_text(f"[plugin_runtime]\nenabled = false\n\n[{plugin_id}]\ncustom = 'keep'\n", encoding="utf-8")
    return config


@pytest.mark.parametrize("with_lock", [False, True])
def test_config_only_runtime_directory_can_receive_code_without_changing_config(tmp_path: Path, with_lock: bool) -> None:
    package = _single_package(tmp_path, "demo")
    plugins_root = tmp_path / "plugins"
    config = _runtime_config(plugins_root)
    if with_lock:
        (config.parent / "plugin.toml.lock").touch()
    original = config.read_bytes()

    plan = build_install_plan(package_path=package, plugins_root=plugins_root)
    assert plan.action == "install"
    result = PluginCliService()._install_via_staging_sync(
        package=package,
        plugins_root=plugins_root,
        profiles_root=tmp_path / "profiles",
        on_conflict="fail",
    )

    assert result.installed_plugins[0].target_dir == plugins_root / "demo"
    assert (plugins_root / "demo" / "plugin.toml").is_file()
    assert (plugins_root / "demo" / "__init__.py").is_file()
    assert config.read_bytes() == original
    if with_lock:
        assert (config.parent / "plugin.toml.lock").read_bytes() == b""


@pytest.mark.parametrize("variant", ["empty", "unknown_file", "unknown_config", "nonempty_lock", "other_table", "identity_table", "broken_config", "broken_manifest", "other_manifest"])
def test_runtime_adoption_rejects_ambiguous_or_foreign_directories(tmp_path: Path, variant: str) -> None:
    package = _single_package(tmp_path, "demo")
    plugins_root = tmp_path / "plugins"
    config = _runtime_config(plugins_root)
    target = config.parent.parent
    if variant == "empty":
        config.unlink()
    elif variant == "unknown_file":
        (target / "notes.txt").write_text("user data", encoding="utf-8")
    elif variant == "unknown_config":
        (config.parent / "extra.toml").write_text("custom = true", encoding="utf-8")
    elif variant == "nonempty_lock":
        (config.parent / "plugin.toml.lock").write_text("unknown data", encoding="utf-8")
    elif variant == "other_table":
        config.write_text("[other]\ncustom = true", encoding="utf-8")
    elif variant == "identity_table":
        config.write_text("[plugin]\nid = 'other'\n[demo]\ncustom = true", encoding="utf-8")
    elif variant == "broken_config":
        config.write_text("[broken", encoding="utf-8")
    else:
        (target / "plugin.toml").write_text("[broken" if variant == "broken_manifest" else "[plugin]\nid = 'other'", encoding="utf-8")

    if variant == "broken_manifest":
        with pytest.raises(ValueError):
            build_install_plan(package_path=package, plugins_root=plugins_root)
    else:
        plan = build_install_plan(package_path=package, plugins_root=plugins_root)
        assert plan.action == "blocked"
        assert plan.reason == "directory_identity_conflict"


def test_runtime_adoption_rollback_preserves_original_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    package = _single_package(tmp_path, "demo")
    plugins_root = tmp_path / "plugins"
    config = _runtime_config(plugins_root)
    original = config.read_bytes()
    rename = Path.rename

    def fail_manifest_promotion(path: Path, target: Path) -> Path:
        if path.name == "plugin.toml" and Path(target).parent == plugins_root / "demo":
            raise OSError("simulated promotion failure")
        return rename(path, target)

    monkeypatch.setattr(Path, "rename", fail_manifest_promotion)
    with pytest.raises(OSError, match="simulated"):
        PluginCliService()._install_via_staging_sync(
            package=package,
            plugins_root=plugins_root,
            profiles_root=tmp_path / "profiles",
            on_conflict="fail",
        )
    assert config.read_bytes() == original
    assert set(config.parent.parent.iterdir()) == {config.parent}
    assert not list(plugins_root.glob(".neko_staging_*"))


def test_runtime_adoption_rejects_package_runtime_config_collision(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    package = _single_package(tmp_path, "demo")
    plugins_root = tmp_path / "plugins"
    config = _runtime_config(plugins_root)
    original = config.read_bytes()
    install_package = plugin_cli_service.install_package

    def stage_package_with_config(*args, **kwargs):
        result = install_package(*args, **kwargs)
        packaged_config = result.installed_plugins[0].target_dir / "config" / "plugin.toml"
        packaged_config.parent.mkdir()
        packaged_config.write_text("[demo]\ncustom = 'overwrite'", encoding="utf-8")
        return result

    monkeypatch.setattr(plugin_cli_service, "install_package", stage_package_with_config)
    with pytest.raises(FileExistsError, match="overwrite existing runtime"):
        PluginCliService()._install_via_staging_sync(
            package=package,
            plugins_root=plugins_root,
            profiles_root=tmp_path / "profiles",
            on_conflict="fail",
        )
    assert config.read_bytes() == original
    assert set(config.parent.parent.iterdir()) == {config.parent}


def test_runtime_adoption_still_blocks_another_installation_of_same_plugin(tmp_path: Path) -> None:
    package = _single_package(tmp_path, "demo")
    plugins_root = tmp_path / "plugins"
    _runtime_config(plugins_root)
    installed = _write_plugin(plugins_root, "demo_copy", "1.0.0")
    (installed / "plugin.toml").write_text("[plugin]\nid = 'demo'", encoding="utf-8")
    plan = build_install_plan(package_path=package, plugins_root=plugins_root)
    assert plan.action == "blocked"
    assert plan.reason == "multiple_installations"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["missing_manager", "lock_write"])
async def test_upload_adoption_metadata_failure_preserves_original_runtime_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    import plugin.settings as plugin_settings

    package = _single_package(tmp_path, "demo")
    plugins_root = tmp_path / "plugins"
    config = _runtime_config(plugins_root)
    (config.parent / "plugin.toml.lock").touch()
    original = config.read_bytes()
    builtin_root = tmp_path / "builtin"
    packages_root = tmp_path / "packages"
    monkeypatch.setattr(plugin_settings, "BUILTIN_PLUGIN_CONFIG_ROOT", builtin_root)
    monkeypatch.setattr(plugin_settings, "USER_PLUGIN_CONFIG_ROOT", plugins_root)
    monkeypatch.setattr(plugin_settings, "USER_PLUGIN_PACKAGES_ROOT", packages_root)
    monkeypatch.setattr(plugin_settings, "USER_PACKAGE_PROFILES_ROOT", tmp_path / "profiles")

    def fail_record(**_kwargs):
        raise InstallSourceError("lock_write_failed", "lock write denied")

    manager = None if failure == "missing_manager" else SimpleNamespace(
        builtin_root=builtin_root,
        user_root=plugins_root,
        record_market_install=fail_record,
    )
    monkeypatch.setattr(plugin_cli_service, "get_install_source_manager", lambda: manager)
    expected_error = ServerDomainError if failure == "missing_manager" else InstallSourceError
    market_detail = {} if failure == "missing_manager" else {
        "plugin_market_id": "demo",
        "version": "2.0.0",
        "package_url": "https://example.invalid/demo.neko-plugin",
    }
    with pytest.raises(expected_error):
        await PluginCliService().upload_and_install(
            filename=package.name,
            content=package.read_bytes(),
            install_source_override={"channel": "market", "mode": "install", "market_detail": market_detail},
        )

    assert config.read_bytes() == original
    assert (config.parent / "plugin.toml.lock").read_bytes() == b""
    assert set(config.parent.parent.iterdir()) == {config.parent}
    assert not list(plugins_root.glob(".neko_staging_*"))
    assert not list(packages_root.glob("*.neko-plugin"))


def test_plan_blocks_bundle_with_any_existing_plugin(tmp_path: Path) -> None:
    source_root = tmp_path / "source"
    package_path = tmp_path / "demo-bundle.neko-bundle"
    build_bundle(
        [_write_plugin(source_root, "demo", "2.0.0"), _write_plugin(source_root, "other", "2.0.0")],
        package_path,
        bundle_id="demo_bundle",
        package_name="Demo Bundle",
        version="2.0.0",
    )
    plugins_root = tmp_path / "plugins"
    _write_plugin(plugins_root, "demo", "1.0.0")

    plan = build_install_plan(package_path=package_path, plugins_root=plugins_root)

    assert plan.action == "blocked"
    assert plan.reason == "bundle_conflict"


def test_plan_blocks_an_installed_declared_previous_id(tmp_path: Path) -> None:
    source = _write_plugin(
        tmp_path / "source",
        "neko_live",
        "1.0.0",
        previous_ids=("neko_roast",),
    )
    package_path = tmp_path / "neko-live.neko-plugin"
    build_plugin(source, package_path)
    plugins_root = tmp_path / "plugins"
    _write_plugin(plugins_root, "neko_roast", "0.1.0")

    plan = build_install_plan(package_path=package_path, plugins_root=plugins_root)

    assert plan.action == "blocked"
    assert plan.reason == "legacy_plugin_present"
    assert plan.legacy_plugin_ids == ("neko_roast",)


def test_confirmation_token_streams_package_instead_of_reading_it_all_at_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package_path = tmp_path / "demo.neko-plugin"
    package_bytes = b"package-content" * 1024
    package_path.write_bytes(package_bytes)
    target_dir = _write_plugin(tmp_path / "plugins", "demo", "1.0.0")
    manifest_bytes = (target_dir / "plugin.toml").read_bytes()
    original_read_bytes = Path.read_bytes

    def guarded_read_bytes(path: Path) -> bytes:
        if path == package_path:
            raise AssertionError("package hash must use bounded reads")
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", guarded_read_bytes)

    expected = hashlib.sha256()
    expected.update(package_bytes)
    expected.update(b"\0")
    expected.update(str(target_dir.resolve()).encode("utf-8"))
    expected.update(b"\0")
    expected.update(manifest_bytes)

    assert confirmation_token(package_path=package_path, target_dir=target_dir) == expected.hexdigest()
