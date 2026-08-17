# OpenBiliClaw Runtime

> **Current contract.** This page documents the first-party integration owned by
> the N.E.K.O main-server process.

`app/openbiliclaw_runtime.py` embeds one `OpenBiliClawCore` and hosts its existing
browser-extension API on `http://127.0.0.1:8420`. It is neither a user plugin nor
an MCP channel. Turning off those optional systems does not disable this runtime.

## Lifecycle and storage

- Main-server startup creates the Core after N.E.K.O storage initialization.
- Main-server shutdown stops the loopback Uvicorn server and lets the ASGI
  shutdown close Core tasks, queues, clients, and its database.
- Core data and configuration live below
  `<N.E.K.O data root>/integrations/openbiliclaw/`; they are not merged into the
  character-memory database.
- A bridge bind/import failure is reported as `unavailable` but does not prevent
  N.E.K.O from starting.

The browser extension keeps its established `/api/*` HTTP and WebSocket
contract. No separate `openbiliclaw start` process is required. When N.E.K.O is
not running, the listener is absent; extension builds with offline buffering can
retain events and upload them after the listener returns.

## Model boundary

At construction time, the adapter reads N.E.K.O's resolved conversation-model
profile and projects it into an in-memory OpenBiliClaw instance route. Secrets
are not copied into OpenBiliClaw's `config.toml`. Provider names are normalized
to the matching Core adapter; custom and Qwen-compatible endpoints use the
OpenAI-compatible wire adapter.

OpenBiliClaw content embeddings remain independently configured. N.E.K.O's
character-memory vectors and OpenBiliClaw's content vectors have different
schemas and must not share a store merely because both are called embeddings.

## Status and recovery

`GET /api/openbiliclaw/status` on the N.E.K.O main server is loopback-only and
returns the state, extension endpoint, data directory, degraded flag, and a
sanitized startup error. It never returns model credentials.

Two process-level recovery controls are available:

- `NEKO_OPENBILICLAW_ENABLED=0` disables only this built-in integration;
- `NEKO_OPENBILICLAW_PORT=<port>` changes the loopback bridge port (default
  `8420`) and therefore requires the browser extension endpoint to match.

The dependency is pinned to the exact OpenBiliClaw Core commit in
`pyproject.toml` and `uv.lock`. N.E.K.O's `bilibili-api-dev` supplies the shared
`bilibili_api` import; uv explicitly suppresses the conflicting upstream wheel.
