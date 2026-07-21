# Moegirl Knowledge

`moegirl-knowledge` is a public, source-attributed knowledge domain for ACG
and internet-meme terminology. It is deliberately separate from the per-character
conversation-memory pipeline: it never writes `recent.json`, `facts.json`,
`reflections.json`, or `persona.json`.

## Phase 1: offline foundation

The first phase owns only the local SQLite + FTS5 store under the
ConfigManager-owned `<knowledge_dir>/moegirl-knowledge/knowledge.db` path.
It stores a title, aliases, tags, content, a short summary, source URL/page ID,
license, content hash, sync timestamp, and status. The FTS index is maintained
in the same transaction as the entry row.

The Main Server starts a bounded background synchronizer after its normal
runtime initialization. It queries a small curated seed catalog, writes only
to this database, and retries on a 24-hour cadence. Failures retain the
previous database and record a degraded state in `sync_state.json`; they never
block startup or a conversation.

Every character receives the built-in `search_moegirl_knowledge` tool. It
reads only the local database and returns at most three source-attributed,
short cards. On a local miss it can perform one bounded public-source lookup
and retain the returned canonical page locally for future recall. It must not
be used for facts about the user, a character's past, or a time-specific event;
those remain `recall_memory` responsibilities.

The synchronizer does not automatically turn a search term or a page redirect
into an `aliases` value. A page can document a related or derived meme without
being semantically interchangeable with it. Later catalog tooling must model
such relations separately.

Candidate discovery follows the verified `mcp-server-moegirl-wiki` pattern:
ranked `generator=search` results with a short rendered extract are preferred;
empty results fall back to `opensearch` title matching. The source adapter keeps
up to five ranked candidates, rejects candidates whose title and extract do not
support the requested term, then fetches and validates the full rendered page
before it can be stored. Candidate discovery is never a relevance guarantee:
weak API search hits are rejected rather than polluting the local library.

## Degradation and provenance

Read operations return no results when the database is unavailable or corrupt;
the store never silently deletes or rebuilds user data. Each entry retains its
source URL and license field so later synchronization and tool rendering can
attribute the source. Public content must be treated as untrusted data rather
than instructions.

## Verification

Run the focused offline suite with `uv run pytest tests/unit/test_moegirl_knowledge_store.py tests/unit/test_moegirl_knowledge_sync.py tests/unit/test_moegirl_knowledge_config.py tests/unit/test_moegirl_wiki_api_source.py -q`.
