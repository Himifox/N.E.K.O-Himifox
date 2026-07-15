"""Coverage and integrity audit for recommendation scenario collections."""
from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from typing import Any

from tests.testbench.pipeline.recommendation_suite import canonical_builtin_scenarios, verify_builtin_manifest

EXPECTED_FACTORS = {"freshness", "quality", "interest", "risk", "candidate_repeat",
                    "source_repeat", "active_bias_boundary", "active_bias_fallback",
                    "extreme_input", "context_preference"}


def build_coverage_report() -> dict[str, Any]:
    scenarios = canonical_builtin_scenarios()
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    factors = Counter()
    targets = Counter()
    for scenario in scenarios:
        groups[_input_hash(scenario)].append(scenario)
        if scenario.get("factor_under_test"):
            factors[str(scenario["factor_under_test"])] += 1
        for source in scenario.get("oracle", {}).get("acceptable_top1_sources", []):
            targets[source] += 1
    duplicates = [[s["id"] for s in rows] for rows in groups.values() if len(rows) > 1]
    conflicts = [[s["id"] for s in rows] for rows in groups.values()
                 if len({json.dumps(s.get("oracle", {}), sort_keys=True) for s in rows}) > 1]
    total_targets = sum(targets.values())
    manifest = verify_builtin_manifest()
    return {"scenario_count": len(scenarios), "unique_input_count": len(groups),
            "suite_id": manifest.get("manifest", {}).get("suite_id"), "manifest_ok": manifest.get("ok"),
            "factor_coverage": dict(sorted(factors.items())),
            "missing_factors": sorted(EXPECTED_FACTORS - set(factors)),
            "duplicate_groups": duplicates, "oracle_conflicts": conflicts,
            "source_target_distribution": {k: round(v / total_targets, 4) for k, v in sorted(targets.items())} if total_targets else {}}


def _input_hash(scenario: dict[str, Any]) -> str:
    if scenario.get("kind") == "sequence":
        value = {"stage": scenario.get("stage"), "base_context": scenario.get("base_context"),
                 "base_inputs": scenario.get("base_inputs"), "steps": [{k: v for k, v in step.items() if k != "oracle"}
                                                                          for step in scenario.get("steps", [])]}
    else:
        value = {"stage": scenario.get("stage"), "context": scenario.get("context"), "inputs": scenario.get("inputs")}
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
