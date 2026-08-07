"""Snapshot engine for the bundled data backup plugin."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import threading
import uuid
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable


BACKUP_GROUPS: dict[str, tuple[str, ...]] = {
    "core": ("config", "character_cards", "memory"),
    "assets": ("card_faces", "live2d", "vrm", "mmd", "pngtuber", "workshop"),
}
_SNAPSHOT_ID_RE = re.compile(r"^\d{8}T\d{12}Z-[0-9a-f]{8}$")


class BackupError(RuntimeError):
    """Raised when a snapshot operation cannot be completed safely."""


class BackupEngine:
    def __init__(
        self, data_root: Path, backup_root: Path, *, retention: int = 10
    ) -> None:
        self.data_root = data_root.expanduser().resolve(strict=False)
        self.backup_root = backup_root.expanduser().resolve(strict=False)
        self.retention = max(1, min(int(retention), 100))
        self._lock = threading.RLock()
        self._last_snapshot_time: datetime | None = None

        if self.backup_root == self.data_root:
            raise BackupError("backup directory cannot be the data root")
        self.backup_root.mkdir(parents=True, exist_ok=True)

    def create_snapshot(
        self, group: str, *, protected: Iterable[str] = ()
    ) -> dict[str, Any]:
        with self._lock:
            paths = self._group_paths(group)
            present_paths = [path for path in paths if (self.data_root / path).exists()]
            if not present_paths:
                raise BackupError(f"backup group has no existing data: {group}")

            snapshot_id = self._new_snapshot_id()
            group_root = self.backup_root / group
            stage = group_root / f".tmp-{snapshot_id}"
            destination = group_root / snapshot_id
            files_root = stage / "files"
            previous = self._latest_manifest(group)
            previous_files = previous.get("files", {}) if previous else {}
            previous_root = (
                Path(previous["_snapshot_path"]) / "files" if previous else None
            )
            manifest_files: dict[str, dict[str, Any]] = {}
            total_bytes = 0

            group_root.mkdir(parents=True, exist_ok=True)
            stage.mkdir(parents=True)
            try:
                for relative_root in present_paths:
                    source_root = self._safe_source(relative_root)
                    if source_root.is_symlink():
                        raise BackupError(
                            f"symbolic-link backup roots are not allowed: {relative_root}"
                        )
                    for source in self._iter_files(source_root):
                        relative = source.relative_to(self.data_root)
                        key = relative.as_posix()
                        target = files_root / relative
                        target.parent.mkdir(parents=True, exist_ok=True)

                        previous_meta = previous_files.get(key)
                        previous_file = (
                            previous_root / relative if previous_root else None
                        )
                        digest, size = self._copy_snapshot_file(
                            source,
                            target,
                            previous_file=previous_file,
                            previous_meta=previous_meta,
                        )

                        manifest_files[key] = {"sha256": digest, "size": size}
                        total_bytes += size

                created_at = datetime.now(UTC).isoformat()
                manifest = {
                    "version": 1,
                    "id": snapshot_id,
                    "group": group,
                    "created_at": created_at,
                    "paths": list(paths),
                    "present_paths": present_paths,
                    "file_count": len(manifest_files),
                    "total_bytes": total_bytes,
                    "files": manifest_files,
                }
                (stage / "manifest.json").write_text(
                    json.dumps(manifest, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                stage.replace(destination)
            except Exception:
                shutil.rmtree(stage, ignore_errors=True)
                raise

            self._prune(group, protected={snapshot_id, *protected})
            return self._public_manifest(manifest)

    def list_snapshots(self, group: str) -> list[dict[str, Any]]:
        with self._lock:
            self._group_paths(group)
            snapshots: list[dict[str, Any]] = []
            for path in self._snapshot_dirs(group):
                try:
                    snapshots.append(self._public_manifest(self._load_manifest(path)))
                except (BackupError, OSError, json.JSONDecodeError):
                    continue
            return sorted(snapshots, key=lambda item: item["id"], reverse=True)

    def restore_snapshot(self, group: str, snapshot_id: str) -> dict[str, Any]:
        with self._lock:
            paths = self._group_paths(group)
            snapshot = self._snapshot_path(group, snapshot_id)
            manifest = self._load_manifest(snapshot)
            if (
                manifest.get("group") != group
                or tuple(manifest.get("paths", ())) != paths
            ):
                raise BackupError("snapshot group metadata does not match")
            self._verify_snapshot(snapshot, manifest)

            safety = self.create_snapshot(group, protected={snapshot_id})
            restore_token = uuid.uuid4().hex
            stage = self.data_root / f".data-backup-restore-{restore_token}"
            old_root = self.data_root / f".data-backup-old-{restore_token}"
            present_paths = set(manifest.get("present_paths", ()))
            moved_old: list[str] = []
            installed: list[str] = []

            try:
                for relative_root in paths:
                    if relative_root in present_paths:
                        source = snapshot / "files" / relative_root
                        target = stage / relative_root
                        if source.exists():
                            shutil.copytree(source, target, copy_function=shutil.copy2)
                        else:
                            target.mkdir(parents=True, exist_ok=True)

                for relative_root in paths:
                    current = self._safe_source(relative_root)
                    old = old_root / relative_root
                    replacement = stage / relative_root
                    if current.exists():
                        old.parent.mkdir(parents=True, exist_ok=True)
                        current.replace(old)
                        moved_old.append(relative_root)
                    if relative_root in present_paths:
                        current.parent.mkdir(parents=True, exist_ok=True)
                        replacement.replace(current)
                        installed.append(relative_root)
            except Exception as exc:
                for relative_root in reversed(installed):
                    self._remove_path(self.data_root / relative_root)
                for relative_root in reversed(moved_old):
                    old = old_root / relative_root
                    current = self.data_root / relative_root
                    if old.exists():
                        current.parent.mkdir(parents=True, exist_ok=True)
                        old.replace(current)
                raise BackupError(f"restore failed and was rolled back: {exc}") from exc
            finally:
                shutil.rmtree(stage, ignore_errors=True)

            shutil.rmtree(old_root, ignore_errors=True)
            self._prune(group, protected={snapshot_id, safety["id"]})
            return {
                "restored": snapshot_id,
                "safety_snapshot": safety["id"],
                "restart_required": True,
            }

    def delete_snapshot(self, group: str, snapshot_id: str) -> dict[str, str]:
        with self._lock:
            snapshot = self._snapshot_path(group, snapshot_id)
            if not snapshot.is_dir():
                raise BackupError("snapshot not found")
            shutil.rmtree(snapshot)
            return {"deleted": snapshot_id}

    def status(self) -> dict[str, Any]:
        with self._lock:
            groups = {
                group: {
                    "paths": list(paths),
                    "snapshots": self.list_snapshots(group),
                }
                for group, paths in BACKUP_GROUPS.items()
            }
            return {
                "data_root": str(self.data_root),
                "backup_root": str(self.backup_root),
                "retention": self.retention,
                "groups": groups,
            }

    def _group_paths(self, group: str) -> tuple[str, ...]:
        try:
            return BACKUP_GROUPS[group]
        except KeyError as exc:
            raise BackupError(f"unknown backup group: {group}") from exc

    def _safe_source(self, relative: str) -> Path:
        candidate = (self.data_root / relative).resolve(strict=False)
        if candidate == self.data_root or self.data_root not in candidate.parents:
            raise BackupError(f"path escapes data root: {relative}")
        return candidate

    @staticmethod
    def _iter_files(root: Path) -> Iterable[Path]:
        for directory, dirnames, filenames in os.walk(root, followlinks=False):
            base = Path(directory)
            dirnames[:] = [name for name in dirnames if not (base / name).is_symlink()]
            for filename in filenames:
                path = base / filename
                if BackupEngine._is_sqlite_sidecar(path):
                    continue
                if path.is_file() and not path.is_symlink():
                    yield path

    def _copy_snapshot_file(
        self,
        source: Path,
        target: Path,
        *,
        previous_file: Path | None,
        previous_meta: Any,
    ) -> tuple[str, int]:
        if self._is_sqlite_database(source):
            self._backup_sqlite(source, target)
            digest = self._sha256(target)
            return digest, target.stat().st_size
        else:
            digest = self._sha256(source)
            if self._can_link_previous(previous_file, previous_meta, digest):
                try:
                    os.link(previous_file, target)
                    return digest, target.stat().st_size
                except OSError:
                    pass
            shutil.copy2(source, target)
            digest = self._sha256(target)
        return digest, target.stat().st_size

    def _can_link_previous(
        self, previous_file: Path | None, previous_meta: Any, digest: str
    ) -> bool:
        return bool(
            isinstance(previous_meta, dict)
            and previous_meta.get("sha256") == digest
            and previous_file is not None
            and previous_file.is_file()
            and not previous_file.is_symlink()
            and self._sha256(previous_file) == digest
        )

    @staticmethod
    def _is_sqlite_database(path: Path) -> bool:
        try:
            with path.open("rb") as stream:
                return stream.read(16) == b"SQLite format 3\x00"
        except OSError:
            return False

    @classmethod
    def _is_sqlite_sidecar(cls, path: Path) -> bool:
        for suffix in ("-wal", "-shm"):
            if path.name.endswith(suffix):
                return cls._is_sqlite_database(
                    path.with_name(path.name[: -len(suffix)])
                )
        return False

    @staticmethod
    def _backup_sqlite(source: Path, target: Path) -> None:
        source_uri = f"{source.resolve(strict=True).as_uri()}?mode=ro"
        try:
            with closing(
                sqlite3.connect(source_uri, uri=True, timeout=10)
            ) as source_db:
                with closing(sqlite3.connect(target, timeout=10)) as target_db:
                    source_db.backup(target_db)
        except sqlite3.Error as exc:
            raise BackupError(f"SQLite backup failed: {source}") from exc

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def _new_snapshot_id(self) -> str:
        now = datetime.now(UTC)
        if self._last_snapshot_time is not None and now <= self._last_snapshot_time:
            now = self._last_snapshot_time + timedelta(microseconds=1)
        self._last_snapshot_time = now
        stamp = now.strftime("%Y%m%dT%H%M%S%fZ")
        return f"{stamp}-{uuid.uuid4().hex[:8]}"

    def _snapshot_dirs(self, group: str) -> list[Path]:
        group_root = self.backup_root / group
        if not group_root.is_dir():
            return []
        return [
            path
            for path in group_root.iterdir()
            if path.is_dir()
            and not path.is_symlink()
            and _SNAPSHOT_ID_RE.fullmatch(path.name)
        ]

    def _snapshot_path(self, group: str, snapshot_id: str) -> Path:
        self._group_paths(group)
        if not _SNAPSHOT_ID_RE.fullmatch(snapshot_id):
            raise BackupError("invalid snapshot id")
        return self.backup_root / group / snapshot_id

    def _load_manifest(self, snapshot: Path) -> dict[str, Any]:
        if snapshot.is_symlink():
            raise BackupError(f"unsafe snapshot path: {snapshot.name}")
        try:
            payload = json.loads(
                (snapshot / "manifest.json").read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError) as exc:
            raise BackupError(f"invalid snapshot manifest: {snapshot.name}") from exc
        if not isinstance(payload, dict) or payload.get("id") != snapshot.name:
            raise BackupError(f"invalid snapshot manifest: {snapshot.name}")
        payload["_snapshot_path"] = str(snapshot)
        return payload

    def _latest_manifest(self, group: str) -> dict[str, Any] | None:
        snapshots = sorted(self._snapshot_dirs(group), reverse=True)
        for snapshot in snapshots:
            try:
                return self._load_manifest(snapshot)
            except BackupError:
                continue
        return None

    def _verify_snapshot(self, snapshot: Path, manifest: dict[str, Any]) -> None:
        files = manifest.get("files")
        if not isinstance(files, dict):
            raise BackupError("snapshot file manifest is missing")
        files_root = (snapshot / "files").resolve(strict=False)
        actual_files: set[str] = set()
        if files_root.exists():
            for directory, dirnames, filenames in os.walk(
                files_root, followlinks=False
            ):
                base = Path(directory)
                if any((base / name).is_symlink() for name in dirnames):
                    raise BackupError("snapshot contains a symbolic link")
                for filename in filenames:
                    path = base / filename
                    if path.is_symlink():
                        raise BackupError("snapshot contains a symbolic link")
                    actual_files.add(path.relative_to(files_root).as_posix())
        if actual_files != set(files):
            raise BackupError("snapshot files do not match the manifest")
        for relative, metadata in files.items():
            if not isinstance(relative, str) or not isinstance(metadata, dict):
                raise BackupError("snapshot file manifest is invalid")
            source = (snapshot / "files" / Path(relative)).resolve(strict=False)
            if (
                files_root not in source.parents
                or not source.is_file()
                or source.is_symlink()
            ):
                raise BackupError(f"snapshot file is missing or unsafe: {relative}")
            if self._sha256(source) != metadata.get("sha256"):
                raise BackupError(f"snapshot checksum mismatch: {relative}")

    def _prune(self, group: str, *, protected: set[str]) -> None:
        snapshots = sorted(self._snapshot_dirs(group), reverse=True)
        keep = set(path.name for path in snapshots[: self.retention]) | protected
        for path in snapshots:
            if path.name not in keep:
                shutil.rmtree(path)

    @staticmethod
    def _remove_path(path: Path) -> None:
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        elif path.exists() or path.is_symlink():
            path.unlink()

    @staticmethod
    def _public_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
        return {
            key: value
            for key, value in manifest.items()
            if key not in {"files", "_snapshot_path"}
        }
