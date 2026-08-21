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

`NekoManagedLLMProvider` reads N.E.K.O's current conversation-model snapshot on
every OpenBiliClaw background call and executes it through the existing
`create_chat_llm_async()` path. A route or model change therefore applies to the
next call without restarting Core. API credentials exist only in N.E.K.O's
resolved configuration and call-time memory; they are never written to
OpenBiliClaw's `config.toml`. Calls use the `openbiliclaw` token-usage category.
The provider maps output budgets, JSON mode, timeout, cancellation, and usage,
and intentionally does not force a temperature.

At startup the adapter migrates any standalone LLM instance in the embedded
configuration to a credential-free `neko-conversation` placeholder. The same
projection is reapplied before initial construction and every Core hot reload,
so source initialization or settings saves cannot reactivate an old direct
DeepSeek/OpenAI route. If the live conversation route is temporarily
unresolvable, the adapter fails closed instead of falling back to direct access.
N.E.K.O's bundled `free-model` service is user-chat-only and rejects background
profile or candidate analysis. The adapter disables that route, leaving Core in
degraded bridge mode without repeated requests. Configure a conversation model
that permits background use and restart N.E.K.O to enable analysis.

“Unified model” means unified routing, credentials, and final speaker—not one
model request for the entire system. OpenBiliClaw can still use the same managed
route for background profile analysis and candidate evaluation. Embedded Core
uses lazy surface copy, so background `recommendation.write_expression` is zero.
The pinned Core enforces this at periodic pool maintenance and both
candidate-admission and refresh-completion fallbacks, including after hot reload.
Its own usage ledger remains module diagnostics and must not be added to
N.E.K.O's total cost a second time.

OpenBiliClaw content embeddings remain independently configured. N.E.K.O's
character-memory vectors and OpenBiliClaw's content vectors have different
schemas and must not share a store merely because both are called embeddings.

## Single speaker and recommendation handoff

```text
OpenBiliClaw background → N.E.K.O-managed model route → structured pool
N.E.K.O proactive chat → privacy gate + Core preview up to 3 (no LLM/no consume)
                      → adapter takes rank 1 → existing Phase 1 (one OBC slot)
                      → existing Phase 2 (one four-field projection; only visible voice)
                      → successful delivery → acknowledge shown
```

- A healthy Core previews at most three evaluated, semantic-ready candidates per
  round. Preview does not refresh sources, call an LLM, or write display history.
- Core `content-eval-v8` independently gates content quality, relevance, and the
  reliability of the exact 80-character summary projection. The diagnostic
  components remain inside Core and never enter either N.E.K.O prompt.
- After fail-closed validation, the adapter takes rank 1 only. OpenBiliClaw
  occupies at most one slot in the existing Phase 1 total budget; other sources
  fill the remaining slots. No second
  Phase 1 is introduced, and selection binds by the displayed sequence number.
- Phase 2 continues to use N.E.K.O persona, memory, and language settings for
  the final line. Normal chat, proactive chat, and tools do not call
  `core.chat()`; it remains for Web, CLI, and compatibility clients.
- Only the selected, successfully committed candidate is acknowledged. `[PASS]`,
  takeover, delivery failure, degraded Core, empty pool, or preview timeout do
  not consume it and do not block the remaining proactive sources.
- Phase 1 receives only title/topic/summary/why-now, aggregate reason codes, and
  bounded selection metadata. Phase 2 receives exactly the selected
  title/topic/summary/why-now. URLs, candidate/item IDs, delivery references,
  free-form recommendation copy, full profiles, and raw behavior stay outside
  both prompts. OpenBiliClaw candidates bypass the generic Bilibili scraper.
- Defensive validation rejects confidence below 0.75, missing summary/topic,
  malformed expiry, expired candidates, or a sensitive-policy mismatch. It
  never rescores or repairs a candidate; OBC remains responsible for quality.
- N.E.K.O usage records Phase 1 and Phase 2 as `proactive.phase1` and
  `proactive.phase2`. The outer `openbiliclaw` entry is the billed total for
  OBC model calls; OBC caller rows are diagnostics and are not added again.
- The last three user messages may be read from active in-memory chat state only
  for Core's deterministic sensitive-topic gate. They are not persisted or
  sent to a model. Sensitive browsing inferences are rejected; an explicit
  current topic or user subscription permits only neutral updates.

The browser extension remains OpenBiliClaw's collection and browser-session
“hands.” N.E.K.O's plugin system and MCP do not need to be enabled, but the
extension itself still needs to be installed and configured. It buffers events
while N.E.K.O is down and replays them to `127.0.0.1:8420` after recovery.

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
