# Public Meme Knowledge

`moegirl-knowledge` is a local public-knowledge domain for ACG and internet
meme terminology. It is separate from character memory and never writes
`recent.json`, `facts.json`, `reflections.json`, or `persona.json`.

## Runtime boundary

The Main Server owns `<knowledge_dir>/moegirl-knowledge/knowledge.db` and imports
the bundled CHIME asset during startup. Normal runtime code is local-only:

- ordinary text turns perform title, alias, and recognition matching in SQLite;
- a confirmed match supplies one ephemeral response card to the current model
  request and is removed before conversation history is persisted;
- `search_public_meme_knowledge` performs local FTS5/BM25 retrieval and returns
  at most three compact cards;
- a local miss returns immediately and never queues a crawler or encyclopedia
  request.

Geng8 and Moegirl adapters are isolated maintenance code. They are not imported
by Main Server startup, chat streaming, the public-knowledge tool, or the normal
`knowledge.moegirl_knowledge.sources` package surface.

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

The bundled CHIME entry “水灵灵” is currently tagged
`quality:stale-usage` based on observed current usage. Explicit local search can
still return it with an outdated-usage warning, but it cannot inject an
automatic conversation card.

## Local management

The Main Server exposes:

```text
GET  /api/moegirl-knowledge/status
GET  /api/moegirl-knowledge/entries
GET  /api/moegirl-knowledge/entry
POST /api/moegirl-knowledge/entry/disabled
POST /api/moegirl-knowledge/chime/reimport
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
The store does not delete the database automatically. CHIME reimport is an
explicit, authenticated local maintenance operation. External acquisition
failures cannot affect chat because external acquisition is not part of the
runtime.

## Verification

Run the focused local suite with:

```text
uv run pytest tests/unit/test_moegirl_knowledge_store.py \
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
