"""Generate a P44-F2 timing/fatigue baseline JSON and Markdown report."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tests.testbench.pipeline.atomic_io import atomic_write_bytes, atomic_write_json
from tests.testbench.pipeline.recommendation_timing_analysis import (
    analyze_timing_fatigue_baseline,
)


def _markdown(report: dict[str, object]) -> str:
    input_data = report["input"]
    outcomes = report["outcomes"]
    associations = report["associations"]
    conclusion = report["conclusion"]
    lines = [
        "# P44-F2 Timing/Fatigue 离线基线分析",
        "",
        f"- Observation：{input_data['observation_count']}",
        f"- Feedback event：{input_data['feedback_event_count']}",
        f"- 输入 SHA-256：`{input_data['input_sha256']}`",
        "- elapsed 使用连续秒数；没有 5/10/30 分钟绝对时间桶门禁。",
        "- 本报告仅分析关联，不修改生产权重、interval、调度、推荐决策或 tuning。",
        "",
        "## 结果可用性",
        "",
        "| 结果 | 可用 | 样本/事件 | 说明 |",
        "|---|---|---:|---|",
    ]
    labels = outcomes["human_should_recommend"]
    false_interruption = outcomes["false_interruption"]
    missed_opportunity = outcomes["missed_opportunity"]
    explicit_feedback = outcomes["explicit_feedback"]
    lines.extend([
        "| 人工 should_recommend | "
        f"{'是' if labels['available'] else '否'} | {labels['labeled_count']} | 覆盖率 {labels['coverage_rate']} |",
        "| 误打扰 | "
        f"{'是' if false_interruption['available'] else '否'} | "
        f"{false_interruption.get('labeled_delivery_count', 0)} | "
        f"{false_interruption.get('reason') or '可分析'} |",
        "| 错失机会 | "
        f"{'是' if missed_opportunity['available'] else '否'} | "
        f"{missed_opportunity.get('labeled_pass_count', 0)} | "
        f"{missed_opportunity.get('reason') or '可分析'} |",
        "| 显式反馈 | "
        f"{'是' if explicit_feedback['available'] else '否'} | "
        f"{explicit_feedback['joined_turn_count']}/{explicit_feedback['delivered_count']} | "
        f"join rate {explicit_feedback['joined_rate']} |",
    ])
    for outcome_name, outcome in associations.items():
        lines.extend(["", f"## {outcome_name}", ""])
        if not outcome["available"]:
            lines.append(f"不可分析：`{outcome['reason']}`。")
            continue
        lines.extend([
            "| 字段 | n | Spearman ρ | 95% CI | 时间稳定 | 来源稳健 | 稳定关系 |",
            "|---|---:|---:|---|---|---|---|",
        ])
        for feature, result in outcome["features"].items():
            ci = result.get("bootstrap_95ci") or {}
            ci_text = (
                "—" if ci.get("lower") is None
                else f"[{ci['lower']}, {ci['upper']}]"
            )
            temporal = result.get("temporal_stability") or {}
            source = result.get("source_stability") or {}
            lines.append(
                f"| {feature} | {result.get('sample_count', 0)} | "
                f"{result.get('spearman_rho', '—')} | {ci_text} | "
                f"{'是' if temporal.get('stable') else '否'} | "
                f"{'是' if source.get('stable') else '否'} | "
                f"{'是' if result.get('stable') else '否'} |"
            )
    lines.extend([
        "",
        "## 结论",
        "",
        f"- 状态：`{conclusion['status']}`",
        f"- {conclusion['statement']}",
        "- 原因：" + (", ".join(f"`{reason}`" for reason in conclusion["reason_codes"]) or "无"),
        "- 若状态为 `candidate_for_shadow`，下一步也仅限单独的 Testbench 配对模拟；本报告不包含候选公式或模拟。",
        "",
        "## 边界",
        "",
        "- 显式反馈缺失不等于负反馈。",
        "- 没有人工 `should_recommend` 标签时，不能用 feedback 或 delivered 代替误打扰、错失机会。",
        "- v3 未记录 scheduler/backoff 阶段，不能据此归因于调度器。",
        "",
    ])
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("output_json", type=Path)
    parser.add_argument("output_markdown", type=Path)
    parser.add_argument("--bootstrap-repetitions", type=int, default=1_000)
    args = parser.parse_args()
    dataset = json.loads(args.input.resolve().read_text(encoding="utf-8"))
    report = analyze_timing_fatigue_baseline(
        dataset,
        bootstrap_repetitions=args.bootstrap_repetitions,
    )
    atomic_write_json(args.output_json.resolve(), report)
    atomic_write_bytes(args.output_markdown.resolve(), _markdown(report).encode("utf-8"))
    print(json.dumps({
        "output_json": str(args.output_json.resolve()),
        "output_markdown": str(args.output_markdown.resolve()),
        "conclusion": report["conclusion"],
        "production_config_modified": False,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
