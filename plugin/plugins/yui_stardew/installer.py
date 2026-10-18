"""Windows Steam setup. All writes and launches require an explicit UI action."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

import portalocker
import psutil

MOD_ID = "Himifox.YuiToIssho"
MOD_DIRECTORY = "YuiToIssho"
MAX_JSON_BYTES = 1024 * 1024


class SetupError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _windows_supported() -> bool:
    return sys.platform == "win32"


def _no_redirects(path: Path) -> None:
    # resolve() alone hides junctions; inspect every original component first.
    for part in (path, *path.parents):
        if part.is_symlink() or (
            part.exists() and getattr(part.lstat(), "st_file_attributes", 0) & 0x400
        ):
            raise SetupError(
                "PATH-REDIRECTED", "目录包含符号链接或目录联接，请选择实际的游戏目录。"
            )


def _json_object(path: Path) -> dict[str, Any]:
    def invalid_constant(value):
        raise ValueError(f"non-JSON numeric constant: {value}")

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    if path.stat().st_size > MAX_JSON_BYTES:
        raise ValueError("JSON file exceeds size limit")
    value = json.loads(
        path.read_text(encoding="utf-8-sig"),
        object_pairs_hook=pairs,
        parse_constant=invalid_constant,
    )
    if not isinstance(value, dict):
        raise ValueError("expected JSON object")
    return value


def _scan_failed(error: OSError) -> None:
    raise SetupError(
        "MOD-SCAN-FAILED", "无法完整读取模组目录，已停止操作；请检查目录权限后重试。"
    ) from error


def _mod_snapshot(root: Path) -> dict[str, str] | None:
    """Detect edits while preparing a replacement, including extra user files."""
    _no_redirects(root)
    if not root.exists():
        return None
    if not root.is_dir():
        raise SetupError("MOD-CHANGED", "模组目录已变化，请刷新后重试。")
    snapshot: dict[str, str] = {}
    for current, directories, names in os.walk(root, followlinks=False, onerror=_scan_failed):
        for name in directories + names:
            path = Path(current) / name
            _no_redirects(path)
            relative = path.relative_to(root).as_posix()
            if name in directories:
                snapshot[f"directory:{relative}"] = ""
            else:
                with path.open("rb") as stream:
                    snapshot[f"file:{relative}"] = hashlib.file_digest(stream, "sha256").hexdigest()
    return snapshot


def _read_vdf(path: Path) -> dict[str, Any]:
    if path.stat().st_size > MAX_JSON_BYTES:
        raise ValueError("VDF exceeds size limit")
    source = path.read_text(encoding="utf-8-sig")
    tokens = re.finditer(r'"((?:\\.|[^"\\])*)"|([{}])|(?:\/\/[^\n]*)', source)
    flat = [
        match[2] or re.sub(r'\\([\\"])', r"\1", match[1])
        for match in tokens
        if match[1] is not None or match[2]
    ]
    index = 0

    def table(depth=0):
        nonlocal index
        if depth > 12:
            raise ValueError("VDF nesting limit")
        result = {}
        while index < len(flat):
            key = flat[index]
            index += 1
            if key == "}":
                return result
            if key == "{" or index >= len(flat):
                raise ValueError("invalid VDF")
            value = flat[index]
            index += 1
            result[key] = table(depth + 1) if value == "{" else value
        if depth:
            raise ValueError("unclosed VDF")
        return result

    return table()


def _steam_roots() -> list[Path]:
    roots: list[Path] = []
    if _windows_supported():
        import winreg

        for hive, key, name in (
            (winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", "SteamPath"),
            (
                winreg.HKEY_LOCAL_MACHINE,
                r"SOFTWARE\WOW6432Node\Valve\Steam",
                "InstallPath",
            ),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Valve\Steam", "InstallPath"),
        ):
            try:
                with winreg.OpenKey(hive, key) as handle:
                    value, _ = winreg.QueryValueEx(handle, name)
                if isinstance(value, str):
                    roots.append(Path(value))
            except OSError:
                continue
    for name in ("ProgramFiles(x86)", "ProgramFiles"):
        if os.environ.get(name):
            roots.append(Path(os.environ[name]) / "Steam")
    return roots


def discover_steam_games() -> list[Path]:
    libraries: list[Path] = []
    for root in _steam_roots():
        libraries.append(root)
        try:
            entries = _read_vdf(root / "steamapps" / "libraryfolders.vdf").get(
                "libraryfolders", {}
            )
            if isinstance(entries, dict):
                for key, value in entries.items():
                    if not key.isdigit():
                        continue
                    location = value.get("path") if isinstance(value, dict) else value
                    if isinstance(location, str) and location:
                        libraries.append(Path(location))
        except (OSError, ValueError):
            continue
    games: dict[str, Path] = {}
    for root in libraries:
        try:
            manifest = _read_vdf(root / "steamapps" / "appmanifest_413150.acf")[
                "AppState"
            ]
            directory = manifest["installdir"]
            if not isinstance(directory, str) or Path(directory).name != directory:
                continue
            game = _validate_game(root / "steamapps" / "common" / directory)
            games[str(game).casefold()] = game
        except (OSError, ValueError, KeyError, TypeError, SetupError):
            continue
    return list(games.values())


def _validate_game(path: Path) -> Path:
    if not path.is_absolute() or str(path).startswith(("\\\\", "//")):
        raise SetupError("GAME-INVALID", "请选择本机 Steam 星露谷游戏目录。")
    _no_redirects(path)
    if (
        path.parent.name.casefold() != "common"
        or path.parent.parent.name.casefold() != "steamapps"
    ):
        raise SetupError("GAME-INVALID", "所选目录不属于 Steam 游戏库。")
    try:
        state = _read_vdf(path.parent.parent / "appmanifest_413150.acf")["AppState"]
        valid = (
            state.get("appid") == "413150"
            and state.get("installdir", "").casefold() == path.name.casefold()
        )
    except (OSError, ValueError, KeyError, AttributeError):
        valid = False
    if not valid or not all(
        (path / name).is_file() for name in ("Stardew Valley.exe", "Stardew Valley.dll")
    ):
        raise SetupError(
            "GAME-INVALID", "未找到有效的 Steam 星露谷安装，请检查游戏目录。"
        )
    return path.resolve()


def _file_version(path: Path) -> tuple[int, ...]:
    try:
        import win32api
    except ImportError:
        return ()
    try:
        info = win32api.GetFileVersionInfo(str(path), "\\")
        return (
            info["FileVersionMS"] >> 16,
            info["FileVersionMS"] & 0xFFFF,
            info["FileVersionLS"] >> 16,
            info["FileVersionLS"] & 0xFFFF,
        )
    except (OSError, win32api.error):
        return ()


def game_running(game: Path) -> bool:
    # A process with a matching game name but unreadable executable is unsafe to update.
    for process in psutil.process_iter(["name"]):
        try:
            if (process.info.get("name") or "").casefold() not in {
                "stardew valley.exe",
                "stardewmoddingapi.exe",
            }:
                continue
            try:
                if Path(process.exe()).parent.resolve() == game:
                    return True
            except psutil.AccessDenied:
                return True
        except psutil.NoSuchProcess:
            continue
    return False


def _version(value: object) -> tuple[int, int, int]:
    if not isinstance(value, str) or re.fullmatch(r"\d+\.\d+\.\d+", value) is None:
        raise SetupError(
            "MOD-VERSION-INVALID", "模组版本无法识别，已保留原文件；请检查现有安装。"
        )
    return tuple(map(int, value.split(".")))


class Installer:
    def __init__(self, bundle_dir: Path | None = None) -> None:
        self.bundle_dir = bundle_dir or Path(__file__).parent / "bundled"

    def _bundle(self) -> tuple[str, dict[str, bytes]]:
        try:
            meta = _json_object(self.bundle_dir / "manifest.json")
            version = meta["version"]
            _version(version)
            archive = meta["archive"]
            if (
                not isinstance(archive, str)
                or Path(archive).name != archive
                or meta.get("protocol_version") != 2
            ):
                raise ValueError("invalid bundle manifest")
            path = self.bundle_dir / archive
            if path.stat().st_size > 4 * 1024 * 1024:
                raise ValueError("bundle exceeds compressed size limit")
            content = path.read_bytes()
            if hashlib.sha256(content).hexdigest() != meta["sha256"]:
                raise ValueError("bundle checksum mismatch")
            files: dict[str, bytes] = {}
            total = 0
            with zipfile.ZipFile(path) as package:
                if len(package.infolist()) > 256:
                    raise ValueError("too many bundle files")
                for info in package.infolist():
                    parts = info.filename.split("/")
                    if (
                        not parts
                        or parts[0] != MOD_DIRECTORY
                        or any(p in {".", ".."} for p in parts)
                        or "\\" in info.filename
                        or ":" in info.filename
                        or stat.S_ISLNK(info.external_attr >> 16)
                    ):
                        raise ValueError("invalid archive path")
                    if info.is_dir():
                        continue
                    relative = PurePosixPath(*parts[1:]).as_posix()
                    allowed = relative in {
                        "manifest.json",
                        "YuiToIssho.dll",
                        "LICENSE",
                    } or (
                        len(parts) == 3
                        and parts[1] == "i18n"
                        and parts[2].endswith(".json")
                    )
                    if not allowed or relative.casefold() in {
                        p.casefold() for p in files
                    }:
                        raise ValueError("unexpected or duplicate archive file")
                    total += info.file_size
                    if total > 32 * 1024 * 1024:
                        raise ValueError("bundle exceeds unpacked size limit")
                    files[relative] = package.read(info)
            manifest = json.loads(files["manifest.json"])
            if (
                manifest.get("UniqueID") != MOD_ID
                or manifest.get("Version") != version
                or manifest.get("EntryDll") != "YuiToIssho.dll"
                or "YuiToIssho.dll" not in files
                or "i18n/default.json" not in files
            ):
                raise ValueError("bundle identity mismatch")
            license_path = self.bundle_dir / "LICENSE.txt"
            if license_path.is_file():
                if license_path.stat().st_size > 16 * 1024:
                    raise ValueError("invalid bundled license")
                files["LICENSE"] = license_path.read_bytes()
            return version, files
        except (
            OSError,
            ValueError,
            KeyError,
            TypeError,
            zipfile.BadZipFile,
            SetupError,
        ) as error:
            raise SetupError(
                "BUNDLE-INVALID", "内附模组损坏或校验失败，请重新安装 N.E.K.O 插件。"
            ) from error

    def select_game(self, game_path: str) -> str:
        if not _windows_supported():
            raise SetupError(
                "PLATFORM-UNSUPPORTED", "一键安装目前支持 Windows Steam 版。"
            )
        return str(_validate_game(Path(game_path)))

    def _environment(self, game_path: str) -> Path:
        game = Path(self.select_game(game_path))
        game_version = _file_version(game / "Stardew Valley.dll")
        if not ((1, 6, 15) <= game_version < (1, 7)):
            raise SetupError(
                "GAME-VERSION-UNSUPPORTED",
                "配套模组支持星露谷 1.6.15 至 1.6.x，请先检查游戏版本。",
            )
        smapi_files = (
            "StardewModdingAPI.exe",
            "StardewModdingAPI.dll",
            "smapi-internal/SMAPI.Toolkit.dll",
        )
        if not all((game / name).is_file() for name in smapi_files):
            raise SetupError(
                "SMAPI-MISSING",
                "尚未安装 SMAPI。请通过官方安装器安装，完成后刷新此面板。",
            )
        if not ((4, 5, 2) <= _file_version(game / "StardewModdingAPI.dll") < (5,)):
            raise SetupError(
                "SMAPI-VERSION-UNSUPPORTED",
                "需要 SMAPI 4.5.2 或兼容的更新 4.x 版本，请安装后刷新。",
            )
        return game

    def _existing(self, game: Path) -> tuple[Path, str, dict[str, Any]]:
        mods = game / "Mods"
        target = mods / MOD_DIRECTORY
        _no_redirects(mods)
        _no_redirects(target)
        if target.exists() and not target.is_dir():
            raise SetupError("MOD-CONFLICT", "目标模组路径已被其他文件占用，请先检查。")
        # Validate every file we will copy, including user files in hidden subfolders.
        for current, directories, names in (
            os.walk(target, followlinks=False, onerror=_scan_failed) if target.exists() else ()
        ):
            for name in directories + names:
                _no_redirects(Path(current) / name)
        # SMAPI ignores dot-prefixed folders and does not search inside a mod.
        # Casefold manifest names: Windows preserves casing in os.walk results.
        if mods.exists():
            count = 0
            for current, directories, names in os.walk(mods, followlinks=False, onerror=_scan_failed):
                count += 1
                if count > 4096:
                    raise SetupError(
                        "MOD-SCAN-LIMIT", "模组目录过多，无法安全确认是否存在重复安装。"
                    )
                current_path = Path(current)
                if current_path == target:
                    directories[:] = []
                    continue
                manifest_name = next(
                    (name for name in names if name.casefold() == "manifest.json"), None
                )
                if current_path != mods and manifest_name:
                    _no_redirects(current_path / manifest_name)
                    directories[:] = []
                else:
                    directories[:] = [
                        name for name in directories if not name.startswith(".")
                    ]
                for name in directories:
                    _no_redirects(current_path / name)
                if current_path != mods and manifest_name:
                    try:
                        manifest = _json_object(current_path / manifest_name)
                    except (OSError, ValueError):
                        # An unreadable manifest might hide a duplicate; do not write yet.
                        raise SetupError(
                            "MOD-SCAN-FAILED",
                            "存在无法读取的模组清单，请修复后再安装。",
                        ) from None
                    if (
                        str(manifest.get("UniqueID", "")).casefold()
                        == MOD_ID.casefold()
                    ):
                        raise SetupError(
                            "MOD-DUPLICATE",
                            "其他目录中已安装 YuiToIssho，请先整理为 Mods/YuiToIssho，避免重复加载。",
                        )
        version = ""
        if target.exists():
            try:
                manifest = _json_object(target / "manifest.json")
                if manifest.get("UniqueID") != MOD_ID:
                    raise ValueError("another mod occupies the target")
                version = manifest["Version"]
                _version(version)
            except (OSError, KeyError, ValueError) as error:
                raise SetupError(
                    "MOD-CONFLICT", "现有 YuiToIssho 清单无法识别，已保留原安装。"
                ) from error
        config: dict[str, Any] = {}
        if (target / "config.json").exists():
            try:
                config = _json_object(target / "config.json")
            except (OSError, ValueError) as error:
                raise SetupError(
                    "MOD-CONFIG-INVALID",
                    "现有 config.json 无法安全读取，已保留原文件，请修复后重试。",
                ) from error
        return target, version, config

    def diagnose(self, selected_path: str = "") -> dict[str, Any]:
        state: dict[str, Any] = {
            "supported": _windows_supported(),
            "code": "GAME-NOT-FOUND",
            "message": "未找到 Steam 星露谷，请选择游戏目录。",
            "games": [],
            "selected_path": selected_path,
            "installed_version": "",
            "bundled_version": "",
            "smapi_installed": False,
            "bridge_enabled": False,
            "game_running": False,
            "can_install": False,
            "can_enable_bridge": False,
            "can_launch": False,
            "backup_available": False,
        }
        try:
            if not state["supported"]:
                raise SetupError(
                    "PLATFORM-UNSUPPORTED", "一键安装目前支持 Windows Steam 版。"
                )
            version, files = self._bundle()
            state["bundled_version"] = version
            games = discover_steam_games()
            state["games"] = [{"path": str(p), "label": f"星露谷 · {p}"} for p in games]
            if not selected_path:
                if len(games) != 1:
                    if games:
                        raise SetupError(
                            "GAME-SELECTION-REQUIRED",
                            "发现多个游戏安装，请选择本次使用的目录。",
                        )
                    return state
                selected_path = str(games[0])
            game = Path(self.select_game(selected_path))
            state["selected_path"] = str(game)
            state["game_running"] = game_running(game)
            state["backup_available"] = (game / ".yuitoissho-backups").is_dir()
            self._environment(str(game))
            state["smapi_installed"] = True
            target, installed, config = self._existing(game)
            state["installed_version"] = installed
            state["bridge_enabled"] = config.get("EnableNekoBridge") is True
            if installed and _version(installed) > _version(version):
                raise SetupError(
                    "MOD-NEWER",
                    "已安装较新的 YuiToIssho，自动安装不会降级；请使用对应版本的 N.E.K.O 插件。",
                )
            matching = bool(installed) and all(
                (target / name).is_file() and (target / name).read_bytes() == data
                for name, data in files.items()
            )
            idle = not state["game_running"]
            state["can_install"] = idle
            state["can_enable_bridge"] = (
                idle and matching and not state["bridge_enabled"]
            )
            state["can_launch"] = idle and matching and state["bridge_enabled"]
            if not matching:
                state.update(
                    code="MOD-INSTALL-REQUIRED",
                    message="点击安装配套模组；已有安装会先完整备份，配置将保留。",
                )
            elif not state["bridge_enabled"]:
                state.update(
                    code="BRIDGE-DISABLED",
                    message="Yui 已安装，点击开启桥接后即可连接 N.E.K.O。",
                )
            elif state["game_running"]:
                state.update(
                    code="GAME-RUNNING",
                    message="游戏正在运行；请进入房主存档，连接状态会自动刷新。",
                )
            else:
                state.update(
                    code="READY", message="安装已就绪，点击启动游戏并进入房主存档。"
                )
            if state["game_running"] and (not matching or not state["bridge_enabled"]):
                state["message"] += " 请先退出游戏再操作。"
        except SetupError as error:
            state.update(code=error.code, message=error.message)
        except OSError:
            state.update(
                code="SETUP-ACCESS-DENIED",
                message="无法读取游戏目录，请检查目录权限后重试。",
            )
        return state

    def _idle(self, game: Path) -> None:
        if game_running(game):
            raise SetupError("GAME-RUNNING", "请先退出星露谷，再安装或修改模组配置。")

    def _apply(
        self, game: Path, files: dict[str, bytes], version: str
    ) -> dict[str, Any]:
        original = _mod_snapshot(game / "Mods" / MOD_DIRECTORY)
        target, installed, config = self._existing(game)
        if installed and _version(installed) > _version(version):
            raise SetupError("MOD-NEWER", "已安装较新模组，已阻止自动降级。")
        if (
            installed
            and config.get("EnableNekoBridge") is True
            and all(
                (target / name).is_file() and (target / name).read_bytes() == value
                for name, value in files.items()
            )
        ):
            return {
                "ok": True,
                "message": "配套模组与桥接已就绪，无需重复安装。",
                "version": version,
                "backup_path": "",
            }
        backup_root = game / ".yuitoissho-backups"
        _no_redirects(backup_root)
        target.parent.mkdir(exist_ok=True)
        # Stage beside Mods, so SMAPI can never discover an incomplete second copy.
        stage = Path(tempfile.mkdtemp(prefix=".yuitoissho-stage-", dir=game))
        backup: Path | None = None
        activated = False
        try:
            if target.exists():
                shutil.copytree(target, stage, dirs_exist_ok=True)
            for name, data in files.items():
                destination = stage / name
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(data)
            if config.get("EnableNekoBridge") is not True:
                config["EnableNekoBridge"] = True
                (stage / "config.json").write_text(
                    json.dumps(config, ensure_ascii=False, indent=2, allow_nan=False)
                    + "\n",
                    encoding="utf-8",
                )
            self._idle(game)
            if _mod_snapshot(target) != original:
                raise SetupError(
                    "MOD-CHANGED",
                    "安装准备期间模组或配置被修改，已保留最新文件；请停止其他修改后重试。",
                )
            if target.exists():
                backup_root.mkdir(exist_ok=True)
                backup = backup_root / (
                    datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
                    + "-"
                    + stage.name.rsplit("-", 1)[-1]
                )
                target.rename(backup)
            try:
                stage.rename(target)
                activated = True
            except OSError:
                if backup is not None:
                    try:
                        backup.rename(target)
                    except OSError as error:
                        raise SetupError(
                            "MOD-RESTORE-FAILED",
                            f"安装未完成且自动恢复受阻。原安装的完整备份位于：{backup}。"
                            "请退出游戏并解除文件占用，按插件说明恢复整个模组目录后再重试。",
                        ) from error
                raise
            return {
                "ok": True,
                "message": "配套模组已安装并开启桥接；原有配置已保留，请启动游戏。",
                "version": version,
                "backup_path": str(backup) if backup else "",
            }
        finally:
            if not activated and stage.exists():
                # stage was allocated under the validated game root by this operation.
                if stage.parent == game and stage.name.startswith(".yuitoissho-stage-"):
                    shutil.rmtree(stage)

    def _operate(self, game_path: str, action: str) -> dict[str, Any]:
        version, files = self._bundle()
        game = self._environment(game_path)
        lock_path = game / ".yuitoissho-setup.lock"
        _no_redirects(lock_path)
        try:
            with portalocker.Lock(str(lock_path), timeout=0):
                self._idle(game)
                if action == "install":
                    return self._apply(game, files, version)
                target, installed, config = self._existing(game)
                if installed != version or not all(
                    (target / name).is_file() and (target / name).read_bytes() == data
                    for name, data in files.items()
                ):
                    raise SetupError(
                        "MOD-INSTALL-REQUIRED",
                        "请先安装配套模组，再开启桥接或启动游戏。",
                    )
                if action == "enable":
                    return self._apply(game, {}, version)
                if config.get("EnableNekoBridge") is not True:
                    raise SetupError("BRIDGE-DISABLED", "请先开启桥接，再启动游戏。")
                for name in (
                    "StardewModdingAPI.exe",
                    "StardewModdingAPI.dll",
                    "smapi-internal/SMAPI.Toolkit.dll",
                ):
                    _no_redirects(game / name)
                # No shell or caller-supplied executable/arguments. Do not alter Steam launch options.
                subprocess.Popen(
                    [str(game / "StardewModdingAPI.exe")],
                    cwd=str(game),
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                return {
                    "ok": True,
                    "message": "已发出启动请求；进入房主存档后会自动连接。",
                }
        except portalocker.exceptions.LockException as error:
            raise SetupError(
                "SETUP-BUSY", "另一项安装或启动操作正在进行，请稍后刷新。"
            ) from error
        except OSError as error:
            raise SetupError(
                "SETUP-IO-FAILED",
                "操作未完成，请检查目录权限或文件占用；已有备份保存在游戏的 .yuitoissho-backups 目录。",
            ) from error

    def install(self, game_path: str) -> dict[str, Any]:
        return self._operate(game_path, "install")

    def enable_bridge(self, game_path: str) -> dict[str, Any]:
        return self._operate(game_path, "enable")

    def launch(self, game_path: str) -> dict[str, Any]:
        return self._operate(game_path, "launch")
