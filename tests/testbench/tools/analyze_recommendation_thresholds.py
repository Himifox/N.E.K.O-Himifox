"""Generate P44-F1 offline PASS/no-op threshold analysis artifacts."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tests.testbench.pipeline.atomic_io import atomic_write_bytes, atomic_write_json
from tests.testbench.pipeline.recommendation_threshold_analysis import (
    analyze_pass_noop_thresholds,
)


def _percent(metric: dict) -> str:
    value = metric.get("value")
    return "—" if value is None else f"{float(value):.2%}"


def _markdown(report: dict) -> str:
    baseline = report["production_baseline"]
    selected = report["exploratory_candidates"]["selected_for_report"]
    best = report["exploratory_candidates"]["best_accuracy"]
    threshold = selected["threshold"]
    impact = report["selected_threshold_impact"]
    score = report["score_distribution"]
    no_candidate = (
        report["conclusion"]["production_candidate_status"]
        == "no_universal_threshold_candidate"
    )
    finding = (
        "不存在能同时保持或改善决策准确率、误打扰率和错失机会率的"
        "非零通用阈值。"
        if no_candidate
        else "至少存在一个在三项准入指标上严格优于生产基线的非零阈值。"
    )
    lines = [
        "# P44-F1 PASS/no-op 离线阈值分析",
        "",
        f"- 有效样本：{report['eligible_count']}",
        f"- 排除样本：{report['excluded_count']}",
        f"- Score ROC-AUC：{score['roc_auc']}",
        f"- 正样本平均分：{score['positive_mean']}",
        f"- 负样本平均分：{score['negative_mean']}",
        "",
        "## 分析模型",
        "",
        "`最终开口 = 生产当时确实开口 AND Top-1 分数 >= 阈值`。",
        "该门只会额外抑制，不能恢复生产当时已经 PASS 的场景。",
        "",
        "## 生产基线与探索候选",
        "",
        "| 指标 | 生产基线 | 5pp 约束内候选 | 无约束最高准确率 |",
        "|---|---:|---:|---:|",
        f"| 阈值 | 无新增门 | {threshold} | {best['threshold']} |",
        f"| 决策准确率 | {_percent(baseline['decision_accuracy'])} | "
        f"{_percent(selected['decision_accuracy'])} | "
        f"{_percent(best['decision_accuracy'])} |",
        f"| Precision | {_percent(baseline['precision'])} | "
        f"{_percent(selected['precision'])} | {_percent(best['precision'])} |",
        f"| Recall | {_percent(baseline['recall'])} | "
        f"{_percent(selected['recall'])} | {_percent(best['recall'])} |",
        f"| F1 | {_percent(baseline['f1'])} | "
        f"{_percent(selected['f1'])} | {_percent(best['f1'])} |",
        f"| 误打扰率 | {_percent(baseline['false_interruption_rate'])} | "
        f"{_percent(selected['false_interruption_rate'])} | "
        f"{_percent(best['false_interruption_rate'])} |",
        f"| 错失机会率 | {_percent(baseline['missed_opportunity_rate'])} | "
        f"{_percent(selected['missed_opportunity_rate'])} | "
        f"{_percent(best['missed_opportunity_rate'])} |",
        f"| 实际开口数 | {baseline['selected_count']} | "
        f"{selected['selected_count']} | {best['selected_count']} |",
        "",
        "## 被新增阈值压掉的投递",
        "",
        "| Turn | 分数 | 来源 | 人工应推荐 | 变化 |",
        "|---|---:|---|---|---|",
    ]
    for row in impact["suppressed_deliveries"]:
        lines.append(
            f"| `{row['turn_id']}` | {row['score']} | {row['source']} | "
            f"{row['should_recommend']} | {row['change']} |"
        )
    if not impact["suppressed_deliveries"]:
        lines.append("| — | — | — | — | 当前候选未改变任何投递 |")
    lines.extend([
        "",
        "## 来源影响",
        "",
        "| 来源 | 基线开口 | 保留 | 抑制 | 保留率 |",
        "|---|---:|---:|---:|---:|",
    ])
    for row in impact["source_impact"]:
        retention = row["retention_rate"]
        retention_text = "—" if retention is None else f"{retention:.2%}"
        lines.append(
            f"| {row['source']} | {row['production_delivered_count']} | "
            f"{row['kept_count']} | {row['suppressed_count']} | "
            f"{retention_text} |"
        )
    lines.extend([
        "",
        "## 结论边界",
        "",
        "- 生产候选状态："
        f"`{report['conclusion']['production_candidate_status']}`。",
        f"- 机械判定：{finding}",
        "- 这是探索候选，不是生产调参建议。",
        "- 下一阶段应独立模拟时间/疲劳、重复惩罚和来源多样性。",
        "- 本次没有修改生产权重、阈值或 tuning。",
        "",
    ])
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("output_json", type=Path)
    parser.add_argument("output_markdown", type=Path)
    parser.add_argument(
        "--missed-opportunity-max-increase",
        type=float,
        default=0.05,
    )
    args = parser.parse_args()
    workbook = json.loads(args.input.resolve().read_text(encoding="utf-8"))
    report = analyze_pass_noop_thresholds(
        workbook,
        missed_opportunity_max_increase=args.missed_opportunity_max_increase,
    )
    atomic_write_json(args.output_json.resolve(), report)
    atomic_write_bytes(
        args.output_markdown.resolve(),
        _markdown(report).encode("utf-8"),
    )
    selected = report["exploratory_candidates"]["selected_for_report"]
    print(json.dumps({
        "output_json": str(args.output_json.resolve()),
        "output_markdown": str(args.output_markdown.resolve()),
        "eligible_count": report["eligible_count"],
        "excluded_count": report["excluded_count"],
        "roc_auc": report["score_distribution"]["roc_auc"],
        "selected_threshold": selected["threshold"],
        "selected_metrics": selected,
        "production_config_modified": False,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
