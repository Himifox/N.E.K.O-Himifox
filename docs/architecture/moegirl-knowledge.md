# Public Meme Knowledge

`moegirl-knowledge` is a local public-knowledge domain for ACG and internet
meme terminology. It is separate from character memory and never writes
`recent.json`, `facts.json`, `reflections.json`, or `persona.json`.

## Runtime boundary

The Main Server owns `<knowledge_dir>/moegirl-knowledge/knowledge.db`. Knowledge
content is installed separately; startup no longer imports a bundled dataset.
Normal runtime code is local-only:

- ordinary text turns perform title, alias, and recognition matching in SQLite;
- a confirmed match supplies one ephemeral response card to the current model
  request and is removed before conversation history is persisted;
- `query_public_knowledge(query, collection="meme", mode="lookup", limit=3)`
  performs local FTS5/BM25 retrieval and returns at most three compact cards;
- a local miss returns immediately and never queues a crawler or encyclopedia
  request.

Geng8 and Moegirl adapters are isolated maintenance code. They are not imported
by Main Server startup, chat streaming, the public-knowledge tool, or the normal
`knowledge.moegirl_knowledge.sources` package surface.

## Generic service boundary

New project code uses `knowledge.api` and `KnowledgeService`. The existing
`build_meme_turn_context`, `MoegirlKnowledgeStore`, and
`MoegirlKnowledgeRetriever` names remain compatibility entrypoints, so the
conversation core, tool registration, memory service, and Main Server lifecycle
do not depend on the generic implementation.

Collection behaviour is project-owned `CollectionSpec` data: storage location,
priority, automatic-context permission, matching thresholds, and response
policy. A future collection reuses the same query, matching, management, and
card-rendering methods instead of adding another set of domain functions.

## Entry contract

The business row contains only five fields:

```text
title / terms / tags / summary / content
```

`terms` contains `alias` and `recognition` arrays. Every entry has exactly one
`source:*` tag. Source homepage and license policy live in the source registry,
while import health remains in source-level state files.

Automatic conversation matching has two modes:

- `strong`: titles and aliases with at least three normalized characters, or
  explicit `recognition` terms with at least two characters;
- `weak_short`: used only after a strong miss for a two-character CHIME title
  or alias that has a source type and at least one source example.

Weak cards tell the existing text model to use the meme sense only when the
whole sentence is clearly non-literal. Literal, medical, safety, financial,
legal, and other serious contexts must ignore the card. A disabled entry or an
entry tagged `quality:stale-usage` never participates in automatic matching.
Strong matches always take priority, and one turn receives at most one card.

Both modes use the same ephemeral task lifecycle. The task asks for a relevant
reaction, stance, light joke, or natural follow-up instead of merely repeating
the user's sentence. It remains absent from history, memory, TTS input, and
subsequent turns.

When the CHIME data package is installed, its “水灵灵” entry is tagged
`quality:stale-usage` based on observed current usage. Explicit local search can
still return it with an outdated-usage warning, but it cannot inject an
automatic conversation card.

## Local data packs

`KnowledgeService.import_pack()` accepts an explicitly selected local JSON pack.
It never downloads, scans for, or executes a pack. Pack entries use the same
five fields; pack ID, source homepage, license, entry count, and automatic
context permission live once in `packs.json` beside the collection database.

Community source tags are derived as `source:community.<pack_id>` and cannot
spoof built-in sources. Import atomically replaces only that source slice. A
community pack is searchable immediately but is excluded from automatic turn
matching until the user explicitly enables that pack. Packs cannot provide
Python, prompts, matching policies, response policies, or network configuration.

## Local management

The Main Server exposes:

```text
GET  /api/moegirl-knowledge/status
GET  /api/moegirl-knowledge/entries
GET  /api/moegirl-knowledge/entry
POST /api/moegirl-knowledge/entry/disabled
```

The entries endpoint supports pagination, source filtering, and production
retrieval diagnostics. The detail endpoint returns one complete five-field
card selected by source and title. Write operations use the existing CSRF and
Origin validation.

Disabled entries are recorded in `catalog.override.json` by source and title;
the SQLite row remains intact and can be restored. Disabled cards are excluded
from both explicit retrieval and automatic turn delivery.

## Degradation

Read operations return no results when the database is unavailable or corrupt.
The store does not delete the database automatically. Knowledge data is
installed through local packages; external acquisition is not part of the
conversation runtime and cannot delay chat.

## Verification

Run the focused local suite with:

```text
uv run pytest tests/unit/test_moegirl_knowledge_store.py \
  tests/unit/test_knowledge_service.py \
  tests/unit/test_knowledge_packs.py \
  tests/unit/test_chime_source.py \
  tests/unit/test_moegirl_turn_context.py \
  tests/unit/test_moegirl_ephemeral_response.py \
  tests/unit/test_moegirl_knowledge_runtime.py \
  tests/unit/test_moegirl_fallback_layers.py \
  tests/unit/test_moegirl_knowledge_management.py \
  tests/unit/test_public_meme_local_context.py -q
```

The isolated remote-adapter tests use fixture HTML and do not contact websites:

```text
uv run pytest tests/unit/test_first_relevant_source.py \
  tests/unit/test_geng8_tag_source.py -q
```
