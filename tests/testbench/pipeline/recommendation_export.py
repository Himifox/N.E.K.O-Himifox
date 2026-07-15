"""JSON and transparent Markdown exports for recommendation runs."""
from __future__ import annotations

import json
from typing import Any


def build_report_json(run: dict[str, Any]) -> str:
    return json.dumps(run, ensure_ascii=False, indent=2)


def build_report_markdown(run: dict[str, Any]) -> str:
    manifest, selection = run.get("suite_manifest") or {}, run.get("selection") or {}
    lines = [f"# Recommendation Testbench — {run.get('name')}", "",
             f"- Run: `{run.get('id')}`", f"- Suite: `{manifest.get('suite_id')}`",
             f"- Suite hash: `{manifest.get('content_hash')}`", f"- Input hash: `{run.get('input_hash')}`",
             f"- Execution: **{run.get('execution_status')}**", f"- Contracts: **{run.get('contract_status')}**",
             f"- Data quality: **{run.get('data_quality_status')}**", f"- Quality gate: **{run.get('quality_gate')}**", "",
             "## Selection", "",
             f"- Builtin selected: {selection.get('builtin_selected')}", f"- User copies selected: {selection.get('user_selected')}",
             f"- Ranking eligible: {selection.get('ranking_eligible')}", f"- Relevance labeled: {selection.get('relevance_labeled')}",
             f"- Contract only: {selection.get('contract_only')}", f"- Sequence cases: {selection.get('sequence_cases')}",
             f"- No-candidate cases: {selection.get('no_candidate_cases')}", "", "## Transparent metrics", ""]
    for variant, metric in (run.get("metrics") or {}).items():
        detail = metric.get("transparent_metrics") or {}; hit = detail.get("hit_at_1") or {}; ndcg = detail.get("ndcg_at_3") or {}
        hard, errors = detail.get("hard_constraints") or {}, detail.get("execution_errors") or {}
        lines.extend([f"### {variant}", "",
                      f"- Hit@1: {hit.get('numerator')}/{hit.get('denominator')} = {hit.get('value')}",
                      f"- nDCG@3: {ndcg.get('value')} over {ndcg.get('denominator')} labeled scenarios",
                      f"- Hard constraints: {hard.get('violations')}/{hard.get('evaluated_cases')}",
                      f"- Execution errors: {errors.get('errors')}/{errors.get('executed_cases')}",
                      f"- Max exposure: {metric.get('max_source_exposure')}", f"- HHI: {metric.get('source_hhi')}",
                      f"- Repeat rate: {metric.get('candidate_repeat_rate')}", ""])
    lines.extend(["## Paired comparisons", ""])
    for variant, result in (run.get("comparisons") or {}).items():
        lines.append(f"### {variant}: {result.get('wins')} wins / {result.get('losses')} losses / {result.get('ties')} ties / {result.get('not_comparable')} not comparable")
        for label, key in (("Wins", "win_details"), ("Losses", "loss_details"), ("Ties", "tie_details")):
            lines.append(f"- {label}:")
            entries = result.get(key) or []
            if not entries: lines.append("  - None")
            for row in entries:
                lines.append(f"  - `{row.get('scenario_id')}`: {row.get('baseline_top1')} → {row.get('candidate_top1')}; "
                             f"acceptable {row.get('baseline_acceptable')} → {row.get('candidate_acceptable')}; {row.get('reason')}")
    coverage = run.get("coverage_snapshot") or {}
    lines.extend(["", "## Coverage", "", f"- Unique inputs: {coverage.get('unique_input_count')}/{coverage.get('scenario_count')}",
                  f"- Factors: `{json.dumps(coverage.get('factor_coverage') or {}, ensure_ascii=False, sort_keys=True)}`",
                  f"- Missing factors: `{json.dumps(coverage.get('missing_factors') or [], ensure_ascii=False)}`", "",
                  "## Resource weight changes", "", "> Offline simulation only; production configuration was not modified."])
    for variant, rows in (run.get("weight_changes") or {}).items():
        lines.extend(["", f"### {run.get('baseline_variant')} → {variant}", "",
                      "| Resource | Effective weight | Δ weight | Tuning | Δ tuning | Score | Δ score | Exposure | Δ exposure |",
                      "|---|---|---:|---|---:|---|---:|---|---:|"])
        for row in rows:
            before, after, delta = row.get("baseline") or {}, row.get("current") or {}, row.get("delta") or {}
            pair = lambda key: f"{before.get(key)} → {after.get(key)}"
            lines.append(f"| {row.get('source')} | {pair('source_weight')} | {delta.get('source_weight')} | "
                         f"{pair('tuning_adjustment')} | {delta.get('tuning_adjustment')} | {pair('score')} | "
                         f"{delta.get('score')} | {pair('top1_exposure')} | {delta.get('top1_exposure')} |")
    lines.extend(["", "> This report is offline-only and did not modify production recommendation tuning."])
    return "\n".join(lines) + "\n"
