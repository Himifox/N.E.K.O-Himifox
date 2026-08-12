"""Plugin-local encrypted storage for the optional NetEase MUSIC_U cookie."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

_COOKIE_FILE = "netease_credentials.bin"
_KEY_FILE = "netease_credentials.key"
_MAX_COOKIE_LENGTH = 4096


class CredentialError(RuntimeError):
    """The plugin-local credential could not be safely read or written."""


def normalize_music_u(value: object) -> str:
    """Accept a MUSIC_U value or Cookie header and return only MUSIC_U."""

    if not isinstance(value, str):
        return ""
    raw = value.strip()
    if not raw or any(ord(char) < 32 or ord(char) == 127 for char in raw):
        return ""

    music_u = raw
    if "=" in raw or ";" in raw:
        parsed: dict[str, str] = {}
        for item in raw.split(";"):
            if "=" not in item:
                continue
            key, candidate = item.strip().split("=", 1)
            if key.strip().upper() == "MUSIC_U":
                parsed["MUSIC_U"] = candidate.strip()
        music_u = parsed.get("MUSIC_U", "")

    if not music_u or len(music_u) > _MAX_COOKIE_LENGTH:
        return ""
    if any(char.isspace() or char in ";\r\n" for char in music_u):
        return ""
    return music_u


class CredentialStore:
    """Encrypt one MUSIC_U value inside this plugin's private data directory."""

    def __init__(self, data_dir: Path) -> None:
        self._data_dir = Path(data_dir)
        self._cookie_path = self._data_dir / _COOKIE_FILE
        self._key_path = self._data_dir / _KEY_FILE

    async def configured(self) -> bool:
        return bool(await self.load())

    async def load(self) -> str:
        try:
            return await asyncio.to_thread(self._load_sync)
        except (OSError, InvalidToken, UnicodeError, ValueError, json.JSONDecodeError):
            return ""

    async def save(self, value: str) -> None:
        music_u = normalize_music_u(value)
        if not music_u:
            raise CredentialError("MUSIC_U is invalid")
        try:
            await asyncio.to_thread(self._save_sync, music_u)
        except OSError as exc:
            raise CredentialError("MUSIC_U could not be saved") from exc

    async def clear(self) -> None:
        try:
            await asyncio.to_thread(self._clear_sync)
        except OSError as exc:
            raise CredentialError("MUSIC_U could not be cleared") from exc

    def _load_sync(self) -> str:
        if not self._cookie_path.is_file() or not self._key_path.is_file():
            return ""
        key = self._key_path.read_bytes()
        encrypted = self._cookie_path.read_bytes()
        payload = json.loads(Fernet(key).decrypt(encrypted).decode("utf-8"))
        if not isinstance(payload, dict):
            return ""
        return normalize_music_u(payload.get("MUSIC_U"))

    def _save_sync(self, music_u: str) -> None:
        self._data_dir.mkdir(parents=True, exist_ok=True)
        key = self._key_path.read_bytes() if self._key_path.is_file() else Fernet.generate_key()
        encrypted = Fernet(key).encrypt(
            json.dumps({"MUSIC_U": music_u}, ensure_ascii=False).encode("utf-8")
        )
        self._atomic_write(self._key_path, key)
        self._atomic_write(self._cookie_path, encrypted)

    def _clear_sync(self) -> None:
        for path in (self._cookie_path, self._key_path):
            try:
                path.unlink()
            except FileNotFoundError:
                pass

    @staticmethod
    def _atomic_write(path: Path, content: bytes) -> None:
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        try:
            temporary.write_bytes(content)
            if os.name != "nt":
                temporary.chmod(0o600)
            os.replace(temporary, path)
            if os.name != "nt":
                path.chmod(0o600)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


__all__ = ["CredentialError", "CredentialStore", "normalize_music_u"]
