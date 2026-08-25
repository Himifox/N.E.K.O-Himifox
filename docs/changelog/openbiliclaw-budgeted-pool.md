# OpenBiliClaw budgeted pool maintenance

N.E.K.O pins OpenBiliClaw Core `af98eae5a` and injects its canonical bounded proactive policy alongside
`surface_copy_mode="lazy"`.

- Valid recommendation inventory is capped at 30; 10 is a soft target, not a
  startup fill requirement.
- Refill starts only below 4 ready items, uses one worker and at most 10 items,
  then observes a persisted 15-minute cooldown with empty-result backoff up to
  6 hours.
- Background OBC input is capped per local calendar day at 100,000 tokens:
  Discovery 50,000, Recommendation 20,000, and Soul 30,000. The provider is not
  called when either the group or total budget would be exceeded.
- Background OBC output is independently capped at 20,000 tokens per local day.
- N.E.K.O Phase 1/2 stay outside those background limits. Their successful
  provider-reported input/output/cache usage is mirrored from TokenTracker to
  Core's shared ledger as `embedded.proactive.phase1/phase2`. Lazy mode still
  performs zero background `recommendation.write_expression` calls.

Standalone OpenBiliClaw defaults are unchanged. The policy is stable across
Core reloads, and persisted cooldown state prevents a restart from creating a
refill burst.
