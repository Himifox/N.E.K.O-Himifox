"""Stable, local-only service for conversational public knowledge."""

from __future__ import annotations

import random
import unicodedata
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Iterable, Mapping

from .collection_overrides import (
    get_collection_override_path,
    load_auto_context_overrides,
    set_collection_auto_context,
)
from .collection_specs import (
    BUILTIN_COLLECTIONS as BUILTIN_COLLECTIONS,
    CORPORA_COLLECTION as CORPORA_COLLECTION,
    CORPORA_MATCH_POLICY as CORPORA_MATCH_POLICY,
    CORPORA_MATERIAL_ROUTES as CORPORA_MATERIAL_ROUTES,
    CORPORA_RESPONSE_POLICY as CORPORA_RESPONSE_POLICY,
    CORPORA_SAMPLE_TAGS as CORPORA_SAMPLE_TAGS,
    MEME_COLLECTION as MEME_COLLECTION,
    MEME_RESPONSE_POLICY as MEME_RESPONSE_POLICY,
    CollectionSpec as CollectionSpec,
    MaterialRoute as MaterialRoute,
    ResponsePolicy as ResponsePolicy,
    _MATERIAL_REQUEST_TERMS as _MATERIAL_REQUEST_TERMS,
    get_reference_details as get_reference_details,
    get_tag_value as get_tag_value,
    get_usage_example as get_usage_example,
)
from .engine.catalog_overrides import (
    get_catalog_override_path,
    load_disabled_entries,
    set_entry_disabled,
)
from .engine.models import KnowledgeEntry, KnowledgeHit
from .engine.source_registry import SOURCES, get_source
from .engine.retrieval import KnowledgeRetriever, MatchPolicy
from .engine.store import KnowledgeStore
from .engine.routing import (
    KnowledgeRoutingState,
    RouteCollection,
    get_routing_state,
    notify_database_changed,
)


@dataclass(frozen=True, slots=True)
class KnowledgeTurnMatch:
    collection_id: str
    hit: KnowledgeHit
    match_mode: str
    collection_priority: int = 0


@dataclass(frozen=True, slots=True)
class KnowledgeTurnContext:
    text: str = ""
    hit_count: int = 0
    match_mode: str = "none"
    collection_id: str = ""
    entry_title: str = ""
    source_tag: str = ""



class KnowledgeService:
    """Query, match and manage local public-knowledge collections."""

    def __init__(
        self,
        knowledge_root: str | Path,
        *,
        collections: Iterable[CollectionSpec] = BUILTIN_COLLECTIONS,
        database_paths: Mapping[str, str | Path] | None = None,
    ) -> None:
        self.knowledge_root = Path(knowledge_root)
        self._collections = {spec.collection_id: spec for spec in collections}
        self._database_paths = {
            key: Path(value) for key, value in (database_paths or {}).items()
        }
        self._auto_context_overrides = load_auto_context_overrides(
            get_collection_override_path(self.knowledge_root)
        )
        self._routing_state: KnowledgeRoutingState | None = None

    @classmethod
    def from_root(cls, knowledge_root: str | Path) -> "KnowledgeService":
        return cls(knowledge_root)

    @classmethod
    def for_collection(
        cls,
        collection_id: str,
        database_path: str | Path,
    ) -> "KnowledgeService":
        database_path = Path(database_path)
        return cls(
            database_path.parent.parent,
            database_paths={collection_id: database_path},
        )

    def search(
        self,
        collection_id: str,
        query: str,
        *,
        limit: int = 3,
    ) -> list[KnowledgeHit]:
        return self._retriever(collection_id).search(query, limit=limit)

    def search_page(
        self,
        collection_id: str,
        query: str,
        *,
        limit: int = 50,
        offset: int = 0,
        source_tag: str = "",
        include_disabled: bool = False,
    ) -> tuple[KnowledgeHit, ...]:
        """Return one bounded ranked page without loading the whole collection."""
        limit = min(max(int(limit), 1), 100)
        offset = min(max(int(offset), 0), 10_000)
        hits = self._retriever(collection_id).search(
            query,
            limit=offset + limit + 1,
            allowed_source_tags=(source_tag,) if source_tag else None,
            include_disabled=include_disabled,
        )
        return tuple(hits[offset:offset + limit + 1])

    def sample_entries(
        self,
        collection_id: str,
        sample_tag: str,
        *,
        limit: int = 1,
    ) -> tuple[KnowledgeEntry, ...]:
        """Return a small random selection from a collection-approved material tag."""
        return self._sample_entries(
            collection_id,
            sample_tag,
            limit=limit,
            allowed_source_tags=None,
        )

    def _sample_entries(
        self,
        collection_id: str,
        sample_tag: str,
        *,
        limit: int,
        allowed_source_tags: tuple[str, ...] | None,
    ) -> tuple[KnowledgeEntry, ...]:
        spec = self._spec(collection_id)
        if sample_tag not in spec.sample_tags:
            raise ValueError("sample tag is not enabled for this collection")
        limit = min(max(int(limit), 1), 3)
        # Tags are already indexed by FTS. The largest bundled material group has
        # fewer than 100 entries, so this remains bounded and avoids a full scan.
        hits = self._retriever(collection_id).search(
            sample_tag,
            limit=100,
            allowed_source_tags=allowed_source_tags,
        )
        candidates = [hit.entry for hit in hits if sample_tag in hit.entry.tags]
        if len(candidates) <= limit:
            return tuple(candidates)
        return tuple(random.sample(candidates, limit))

    def match_turn(
        self,
        collection_id: str,
        user_text: str,
        *,
        limit: int = 1,
    ) -> list[KnowledgeTurnMatch]:
        spec = self._spec(collection_id)
        policy = self._effective_match_policy(spec)
        mode, hits = self._retriever(collection_id).match_turn(
            user_text,
            policy=policy,
            limit=limit,
        )
        return [
            KnowledgeTurnMatch(
                collection_id=collection_id,
                hit=hit,
                match_mode=mode,
                collection_priority=spec.priority,
            )
            for hit in hits
        ]

    def build_turn_context(
        self,
        user_text: str,
        *,
        collection_ids: Iterable[str] | None = None,
        limit: int = 1,
    ) -> KnowledgeTurnContext:
        if limit <= 0:
            return KnowledgeTurnContext()
        if collection_ids is None:
            allowed = frozenset(
                spec.collection_id
                for spec in self._collections.values()
                if self._auto_context_enabled(spec) and spec.response_policy is not None
            )
        else:
            allowed = frozenset(collection_ids)
            unknown = allowed.difference(self._collections)
            if unknown:
                raise ValueError(f"unknown knowledge collection: {sorted(unknown)[0]}")
            allowed = frozenset(
                collection_id
                for collection_id in allowed
                if self._spec(collection_id).response_policy is not None
            )
        if not allowed:
            return KnowledgeTurnContext()
        route_match = self._get_routing_state().match(
            user_text,
            allowed_collections=allowed,
        )
        if route_match is None:
            return KnowledgeTurnContext()
        entry = self._get_routing_state().get_card(route_match)
        if entry is None:
            return KnowledgeTurnContext()
        selected = KnowledgeTurnMatch(
            collection_id=route_match.record.collection_id,
            hit=KnowledgeHit(entry=entry, score=route_match.score),
            match_mode=route_match.match_mode,
            collection_priority=route_match.record.priority,
        )
        policy = self._spec(selected.collection_id).response_policy
        if policy is None:
            return KnowledgeTurnContext()
        return KnowledgeTurnContext(
            text=self._render_turn_context(selected, policy),
            hit_count=1,
            match_mode=selected.match_mode,
            collection_id=selected.collection_id,
            entry_title=entry.title,
            source_tag=entry.source_tag,
        )

    def build_conversation_context(
        self,
        user_text: str,
        *,
        collection_ids: Iterable[str] | None = None,
        limit: int = 1,
    ) -> KnowledgeTurnContext:
        """Resolve a direct mention first, then a narrow explicit material request."""
        direct = self.build_turn_context(
            user_text,
            collection_ids=collection_ids,
            limit=limit,
        )
        if direct.hit_count or limit <= 0:
            return direct
        allowed = (
            frozenset(self._collections)
            if collection_ids is None
            else frozenset(collection_ids)
        )
        unknown = allowed.difference(self._collections)
        if unknown:
            raise ValueError(f"unknown knowledge collection: {sorted(unknown)[0]}")
        normalized = unicodedata.normalize("NFKC", str(user_text)).casefold()
        for spec in sorted(
            (self._spec(value) for value in allowed),
            key=lambda value: (-value.priority, value.collection_id),
        ):
            if not self._auto_context_enabled(spec) or spec.response_policy is None:
                continue
            route = next((
                candidate
                for candidate in spec.material_routes
                if any(term in normalized for term in candidate.topic_terms)
                and any(term in normalized for term in candidate.request_terms)
            ), None)
            if route is None:
                continue
            entries = self._sample_entries(
                spec.collection_id,
                route.sample_tag,
                limit=1,
                allowed_source_tags=self._effective_match_policy(spec).allowed_source_tags,
            )
            if not entries:
                continue
            selected = KnowledgeTurnMatch(
                collection_id=spec.collection_id,
                hit=KnowledgeHit(entry=entries[0], score=0.0),
                match_mode="material_sample",
                collection_priority=spec.priority,
            )
            return KnowledgeTurnContext(
                text=self._render_turn_context(selected, spec.response_policy),
                hit_count=1,
                match_mode="material_sample",
                collection_id=spec.collection_id,
                entry_title=entries[0].title,
                source_tag=entries[0].source_tag,
            )
        return KnowledgeTurnContext()

    def list_entries(
        self,
        collection_id: str,
        *,
        source_tag: str = "",
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[KnowledgeEntry, ...]:
        return self._store(collection_id).list_entries(
            source_tag=source_tag,
            limit=limit,
            offset=offset,
        )

    def get_entry(
        self,
        collection_id: str,
        *,
        source_tag: str,
        title: str,
    ) -> KnowledgeEntry | None:
        return self._store(collection_id).get_entry(source_tag, title)

    def set_entry_disabled(
        self,
        collection_id: str,
        *,
        source_tag: str,
        title: str,
        disabled: bool,
    ) -> int:
        database_path = self.database_path(collection_id)
        count = set_entry_disabled(
            get_catalog_override_path(database_path),
            source_tag=source_tag,
            title=title,
            disabled=disabled,
        )
        notify_database_changed(database_path)
        # Management writes may remove a route, so publish the new snapshot
        # before returning rather than briefly serving a disabled card.
        if self._routing_state is not None:
            self._routing_state.refresh()
        return count

    def get_status(self, collection_id: str) -> dict:
        spec = self._spec(collection_id)
        database_path = self.database_path(collection_id)
        database_exists = database_path.is_file()
        store = self._store(collection_id) if database_exists else None
        disabled = load_disabled_entries(
            get_catalog_override_path(database_path)
        )
        return {
            "collection_id": collection_id,
            "name": spec.display_name or collection_id,
            "entries": store.count() if store is not None else 0,
            "integrity_ok": store.integrity_ok() if store is not None else False,
            "auto_context": self._auto_context_enabled(spec),
            "disabled_entries": len(disabled),
            "sources": store.count_by_source_tags() if store is not None else (),
            "packs": len(self.list_packs(collection_id)),
        }

    def list_collections(self) -> tuple[dict, ...]:
        results: list[dict] = []
        for collection_id in sorted(self._collections):
            try:
                payload = self.get_status(collection_id)
                status = "ready" if payload["integrity_ok"] is True else "degraded"
                results.append({"status": status, **payload})
            except Exception as exc:
                spec = self._spec(collection_id)
                results.append({
                    "collection_id": collection_id,
                    "name": spec.display_name or collection_id,
                    "status": "degraded",
                    "integrity_ok": False,
                    "error_type": type(exc).__name__,
                    "auto_context": self._auto_context_enabled(spec),
                })
        return tuple(results)

    def set_collection_auto_context(self, collection_id: str, *, enabled: bool) -> None:
        self._spec(collection_id)
        set_collection_auto_context(
            get_collection_override_path(self.knowledge_root),
            collection_id=collection_id,
            enabled=enabled,
        )
        self._auto_context_overrides[collection_id] = bool(enabled)
        self._routing_state = None

    def install_pack(self, pack, *, subscription=None):
        from .packs import install_pack

        self._spec(pack.collection_id)
        result = install_pack(
            self.database_path(pack.collection_id),
            pack,
            subscription=subscription,
        )
        self.refresh_routing_index(background=True)
        return result

    def count_entries(self, collection_id: str, *, source_tag: str = "") -> int:
        store = self._store(collection_id)
        return store.count_by_source_tag(source_tag) if source_tag else store.count()

    def import_pack(self, path: str | Path):
        """Validate and install a local data pack into a known collection."""
        from .packs import install_pack, load_pack

        pack = load_pack(path)
        self._spec(pack.collection_id)
        result = install_pack(self.database_path(pack.collection_id), pack)
        self.refresh_routing_index(background=True)
        return result

    def remove_pack(self, collection_id: str, pack_id: str) -> int:
        from .packs import remove_pack

        self._spec(collection_id)
        removed = remove_pack(self.database_path(collection_id), pack_id)
        self._routing_state = None
        self.refresh_routing_index(background=True)
        return removed

    def list_packs(self, collection_id: str) -> tuple[dict, ...]:
        from .packs import list_installed_packs

        return list_installed_packs(self.database_path(collection_id))

    def set_pack_auto_context(
        self,
        collection_id: str,
        pack_id: str,
        *,
        enabled: bool,
    ) -> None:
        from .packs import set_pack_auto_context

        set_pack_auto_context(
            self.database_path(collection_id),
            pack_id,
            enabled=enabled,
        )
        self._routing_state = None
        self.refresh_routing_index(background=True)

    def refresh_routing_index(self, *, background: bool = False) -> None:
        state = self._get_routing_state()
        if background:
            state.refresh_in_background()
        else:
            state.refresh()

    def database_path(self, collection_id: str) -> Path:
        if collection_id in self._database_paths:
            return self._database_paths[collection_id]
        spec = self._spec(collection_id)
        return self.knowledge_root / spec.storage_directory / spec.database_filename

    def _spec(self, collection_id: str) -> CollectionSpec:
        try:
            return self._collections[collection_id]
        except KeyError as exc:
            raise ValueError(f"unknown knowledge collection: {collection_id}") from exc

    def _store(self, collection_id: str) -> KnowledgeStore:
        return KnowledgeStore(self.database_path(collection_id))

    def _retriever(self, collection_id: str) -> KnowledgeRetriever:
        return KnowledgeRetriever(self._store(collection_id))

    def _get_routing_state(self) -> KnowledgeRoutingState:
        if self._routing_state is None:
            collections = tuple(
                RouteCollection(
                    collection_id=spec.collection_id,
                    database_path=self.database_path(spec.collection_id),
                    priority=spec.priority,
                    policy=self._effective_match_policy(spec),
                    context_hints=spec.context_hints,
                )
                for spec in self._collections.values()
                if spec.response_policy is not None
            )
            self._routing_state = get_routing_state(collections)
        return self._routing_state

    def _auto_context_enabled(self, spec: CollectionSpec) -> bool:
        return self._auto_context_overrides.get(
            spec.collection_id,
            spec.auto_context_enabled,
        )

    def _effective_match_policy(self, spec: CollectionSpec) -> MatchPolicy:
        if not spec.restrict_auto_context_to_registered_sources:
            return spec.match_policy
        from .packs import enabled_pack_source_tags

        allowed_sources = tuple(sorted((
            *(spec.auto_context_source_tags or SOURCES),
            *enabled_pack_source_tags(self.database_path(spec.collection_id)),
        )))
        return replace(spec.match_policy, allowed_source_tags=allowed_sources)

    def _render_turn_context(
        self,
        match: KnowledgeTurnMatch,
        policy: ResponsePolicy,
    ) -> str:
        entry = match.hit.entry
        if match.match_mode == "weak_short":
            lines = [
                policy.weak_header,
                policy.weak_preamble,
                policy.task_instruction,
            ]
        elif match.match_mode == "material_sample":
            lines = [
                policy.confirmed_header,
                policy.sample_preamble or policy.confirmed_preamble,
                policy.task_instruction,
            ]
        else:
            lines = [
                policy.confirmed_header,
                policy.confirmed_preamble,
                policy.task_instruction,
            ]
        meaning = (entry.summary or entry.content).replace("\n", " ").strip()[:280]
        classification = get_tag_value(entry, policy.classification_tag_prefix)
        details = get_reference_details(
            entry,
            policy.detail_line_prefixes,
            max_chars=420,
        )
        lines.extend((
            f"{policy.term_label}: {entry.title}\n",
            f"{policy.summary_label}: {meaning}\n",
        ))
        if classification:
            lines.append(f"{policy.classification_label}: {classification}\n")
        if details:
            lines.append(f"{policy.detail_label}: {details}\n")
        posture = policy.type_postures.get(classification, policy.default_posture)
        source = get_source(
            entry.source_tag,
            database_path=self.database_path(match.collection_id),
        )
        lines.extend((
            f"Response posture: {posture}\n",
            f"Source: {source.name}\n",
            "==========================================================",
        ))
        return "".join(lines)
