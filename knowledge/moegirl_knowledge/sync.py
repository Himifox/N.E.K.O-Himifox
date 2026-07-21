"""Bounded, restart-safe synchronization into the local public knowledge store."""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Protocol

from utils.file_utils import atomic_write_json

from .filters import is_relevant_source_page
from .models import MoegirlKnowledgeEntry
from .store import MoegirlKnowledgeStore


class PageSource(Protocol):
    async def find_relevant_page(self, query: str, *, limit: int = 5): ...


class CatalogPageSource(PageSource, Protocol):
    async def recent_public_pages(self, *, limit: int): ...

    async def catalog_public_pages(self, seed_queries: Iterable[str], *, limit: int): ...


class MoegirlKnowledgeSynchronizer:
    """Synchronize a small curated catalog without making semantic alias claims."""

    def __init__(
        self,
        database_path: str | Path,
        state_path: str | Path,
        source: PageSource,
        *,
        request_delay_seconds: float = 0.0,
    ) -> None:
        self.store = MoegirlKnowledgeStore(database_path)
        self.state_path = Path(state_path)
        self.source = source
        self.request_delay_seconds = max(0.0, request_delay_seconds)
        self._lock = asyncio.Lock()

    async def sync_once(self, queries: Iterable[str], *, limit: int) -> dict[str, int | str]:
        async with self._lock:
            started = datetime.now(timezone.utc)
            added = updated = unchanged = failed = 0
            for query in list(queries)[:max(0, limit)]:
                try:
                    page = await self.source.find_relevant_page(str(query), limit=5)
                    if page is None or not is_relevant_source_page(
                        str(query), title=page.title, content=page.content
                    ):
                        failed += 1
                        continue
                    entry_id = f"moegirl:{page.page_id}" if page.page_id is not None else f"moegirl:{hashlib.sha256(page.source_url.encode()).hexdigest()[:20]}"
                    entry = MoegirlKnowledgeEntry(
                        id=entry_id, title=page.title, content=page.content,
                        summary=page.content[:600], source_url=page.source_url,
                        source_page_id=page.page_id, tags=("moegirl", "public-knowledge"),
                        synced_at=started.isoformat().replace("+00:00", "Z"),
                    )
                    result = await asyncio.to_thread(self.store.upsert, entry)
                    added += int(result.created)
                    updated += int(result.updated)
                    unchanged += int(result.unchanged)
                except Exception:
                    failed += 1
                if self.request_delay_seconds:
                    await asyncio.sleep(self.request_delay_seconds)

            previous_state = self.load_state()
            successful_items = added + updated + unchanged
            status = {
                "last_attempt_at": started.isoformat().replace("+00:00", "Z"),
                "last_success_at": (
                    started.isoformat().replace("+00:00", "Z")
                    if successful_items
                    else str(previous_state.get("last_success_at") or "")
                ),
                "status": "ready" if failed == 0 else "degraded",
                "entries": self.store.count(), "added": added, "updated": updated,
                "unchanged": unchanged, "failed": failed,
            }
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            await asyncio.to_thread(atomic_write_json, self.state_path, status, ensure_ascii=False, indent=2)
            return status

    async def sync_recent_once(self, *, limit: int) -> dict[str, int | str]:
        """Append one low-priority public catalog batch without reading chat text."""
        if not hasattr(self.source, "recent_public_pages"):
            return self._write_status(
                started=datetime.now(timezone.utc), added=0, updated=0, unchanged=0, failed=0
            )
        async with self._lock:
            started = datetime.now(timezone.utc)
            try:
                pages = await self.source.recent_public_pages(limit=max(0, limit))
                entries = tuple(self._entry_from_page(page, started) for page in pages)
                results = await asyncio.to_thread(self.store.upsert_many, entries)
                return self._write_status(
                    started=started,
                    added=sum(result.created for result in results),
                    updated=sum(result.updated for result in results),
                    unchanged=sum(result.unchanged for result in results),
                    failed=0,
                )
            except Exception:
                return self._write_status(
                    started=started, added=0, updated=0, unchanged=0, failed=1
                )

    async def sync_catalog_once(
        self,
        seed_queries: Iterable[str],
        *,
        limit: int,
    ) -> dict[str, int | str]:
        """Expand source-owned categories from bootstrap pages in one bounded batch."""
        if not hasattr(self.source, "catalog_public_pages"):
            return await self.sync_once(seed_queries, limit=limit)
        async with self._lock:
            started = datetime.now(timezone.utc)
            try:
                pages = await self.source.catalog_public_pages(seed_queries, limit=max(0, limit))
                entries = tuple(self._entry_from_page(page, started) for page in pages)
                results = await asyncio.to_thread(self.store.upsert_many, entries)
                return self._write_status(
                    started=started,
                    added=sum(result.created for result in results),
                    updated=sum(result.updated for result in results),
                    unchanged=sum(result.unchanged for result in results),
                    failed=0,
                )
            except Exception:
                return self._write_status(
                    started=started, added=0, updated=0, unchanged=0, failed=1
                )

    @staticmethod
    def _entry_from_page(page, synced_at: datetime) -> MoegirlKnowledgeEntry:
        entry_id = (
            f"moegirl:{page.page_id}"
            if page.page_id is not None
            else f"moegirl:{hashlib.sha256(page.source_url.encode()).hexdigest()[:20]}"
        )
        return MoegirlKnowledgeEntry(
            id=entry_id,
            title=page.title,
            content=page.content,
            summary=page.content[:600],
            source_url=page.source_url,
            source_page_id=page.page_id,
            tags=("moegirl", "public-knowledge"),
            synced_at=synced_at.isoformat().replace("+00:00", "Z"),
        )

    def _write_status(
        self,
        *,
        started: datetime,
        added: int,
        updated: int,
        unchanged: int,
        failed: int,
    ) -> dict[str, int | str]:
        previous_state = self.load_state()
        successful_items = added + updated + unchanged
        status = {
            "last_attempt_at": started.isoformat().replace("+00:00", "Z"),
            "last_success_at": (
                started.isoformat().replace("+00:00", "Z")
                if successful_items
                else str(previous_state.get("last_success_at") or "")
            ),
            "status": "ready" if failed == 0 else "degraded",
            "entries": self.store.count(),
            "added": added,
            "updated": updated,
            "unchanged": unchanged,
            "failed": failed,
        }
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(self.state_path, status, ensure_ascii=False, indent=2)
        return status

    def load_state(self) -> dict:
        try:
            return json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"status": "not_synced", "entries": self.store.count()}
