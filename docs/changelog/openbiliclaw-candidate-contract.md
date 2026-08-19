# OpenBiliClaw proactive candidate contract

The built-in integration now consumes Core's privacy-bounded
`preview_proactive_candidates()` result instead of projecting the legacy
recommendation object directly.

## Behavior change

- Tracking, Phase 1, and Phase 2 data are separate objects.
- Core ranks up to three bounded candidates internally; the adapter validates
  them and sends rank 1 only, so Phase 1 receives at most one OBC slot.
- Phase 2 receives one selected `title/topic/summary/why_now` projection and no
  longer sends OpenBiliClaw candidates through the generic Bilibili scraper.
- Successful committed text plus the delivered link is required before the
  tracking-only delivery reference is acknowledged.
- Sensitive-topic decisions occur in Core before Phase 1. The last three user
  messages are read from active memory only and are not persisted or modeled by
  this handoff.

## Compatibility

Normal chat and proactive chat still never call `core.chat()`. Existing public
response links remain `title/url/source/mode`. The plugin system, MCP, browser
extension endpoint, model ownership, and two-call Phase 1/Phase 2 architecture
are unchanged. N.E.K.O pins OpenBiliClaw Core commit `9cd7d6eae` and starts it
with lazy surface copy, so background `recommendation.write_expression` is zero.

The contract is covered by prompt-boundary unit tests, single-speaker tests, and
a real-Core integration test that confirms Core can rank three while Phase 1
receives rank 1 and only that delivered candidate is consumed. Phase usage is
split into `proactive.phase1` and `proactive.phase2` without double-counting the
outer `openbiliclaw` billed total.
