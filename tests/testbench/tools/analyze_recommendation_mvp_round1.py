"""Run the bounded four-arm Recommendation MVP candidate round."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tests.testbench.pipeline.atomic_io import atomic_write_bytes, atomic_write_json
from tests.testbench.pipeline.recommendation_mvp_round1 import (
    analyze_recommendation_mvp_round1,
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _percent(value: float | None) -> str:
    return "—" if value is None else f"{value:.2%}"


def _delta(candidate: float | None, baseline: float | None) -> str:
    if candidate is None or baseline is None:
        return "—"
    return f"{candidate - baseline:+.4f}"


def _gate_failures(gate: dict) -> str:
    failed = []
    for layer in ("all_eligible", "high_confidence"):
        for name, passed in gate[layer]["checks"].items():
            if not passed:
                failed.append(f"{layer}:{name}")
    return ", ".join(failed) or "—"


def _markdown(report: dict) -> str:
    holdout = report["metrics"]["holdout"]
    baseline = holdout["baseline"]["all_eligible"]
    lines = [
        "# Recommendation MVP 第一轮四臂配对结果",
        "",
        f"- 状态：`{report['conclusion']['status']}`",
        f"- 唯一选择：`{report['conclusion']['selected_arm']}`",
        f"- Discovery / Holdout：{report['split']['discovery_observations']} / "
        f"{report['split']['holdout_observations']}",
        f"- 独立断点：{report['split']['gap_seconds']} 秒",
        f"- 有效人工样本：{report['input_contract']['metric_eligible_count']}",
        f"- 输入契约：{'通过' if report['input_contract']['passed'] else '失败'}",
        "- 生产配置、MVP 与 tuning：均未修改",
        "",
        "## 固定四臂",
        "",
        "1. `baseline`：冻结数据中的生产分数。",
        "2. `source_calibration`：仅用 Discovery 学出的 ±0.04 封顶来源偏移。",
        "3. `delivered_history`：重复/来源历史只累计实际投递，公式常量不变。",
        "4. `combined`：同时应用 2 和 3。",
        "",
        "## Holdout 指标",
        "",
        "| Arm | Hit@1 | ΔHit | MRR | nDCG@3 | ΔnDCG | HHI | 最大来源曝光 | Top-1变化 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for arm in ("baseline", "source_calibration", "delivered_history", "combined"):
        metric = holdout[arm]["all_eligible"]
        hit = metric["hit_at_1"]
        lines.append(
            f"| `{arm}` | {hit['numerator']}/{hit['denominator']} "
            f"({_percent(hit['value'])}) | "
            f"{hit['numerator'] - baseline['hit_at_1']['numerator']:+d} | "
            f"{metric['mrr']} | {metric['ndcg_at_3']} | "
            f"{_delta(metric['ndcg_at_3'], baseline['ndcg_at_3'])} | "
            f"{metric['source_hhi']} | {_percent(metric['max_source_exposure'])} | "
            f"{metric['changed_top1_count']} |"
        )
    lines.extend([
        "",
        "## 来源校准参数",
        "",
        "| 来源 | Discovery候选 | relevance档位 | 原始gap | 应用偏移 | 支持 |",
        "|---|---:|---|---:|---:|---|",
    ])
    for row in report["source_calibration"]["sources"]:
        levels = ",".join(str(value) for value in row["relevance_levels"])
        lines.append(
            f"| {row['source']} | {row['candidate_rows']} | {levels} | "
            f"{row['mean_normalized_relevance_minus_score']} | {row['adjustment']:+.4f} | "
            f"{'是' if row['supported'] else '否'} |"
        )
    lines.extend([
        "",
        "## 准入门禁",
        "",
        "| Arm | 通过 | 失败项 |",
        "|---|---|---|",
    ])
    for arm, gate in report["candidate_gates"].items():
        lines.append(
            f"| `{arm}` | {'是' if gate['passed'] else '否'} | {_gate_failures(gate)} |"
        )
    lines.extend([
        "",
        "## 结论",
        "",
        f"- 第一轮选择：`{report['conclusion']['selected_arm']}`。",
        f"- 机械结论：{report['conclusion']['next_step']}。",
        "- 排序候选不改变 PASS、搭话时机、生成文本或投递层去重。",
        "- 本报告到此结束，不触发新标注或新一轮 Testbench 研究。",
        "",
        "## 限制",
        "",
    ])
    lines.extend(f"- {item}" for item in report["limitations"])
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source_freeze", type=Path)
    parser.add_argument("adjudicated_workbook", type=Path)
    parser.add_argument("output_json", type=Path)
    parser.add_argument("output_markdown", type=Path)
    args = parser.parse_args()

    source_path = args.source_freeze.resolve()
    labels_path = args.adjudicated_workbook.resolve()
    source = json.loads(source_path.read_text(encoding="utf-8"))
    labels = json.loads(labels_path.read_text(encoding="utf-8"))
    report = analyze_recommendation_mvp_round1(
        source,
        labels,
        source_sha256=_sha256(source_path),
        labels_sha256=_sha256(labels_path),
    )
    atomic_write_json(args.output_json.resolve(), report)
    atomic_write_bytes(args.output_markdown.resolve(), _markdown(report).encode("utf-8"))
    print(json.dumps({
        "output_json": str(args.output_json.resolve()),
        "output_markdown": str(args.output_markdown.resolve()),
        "status": report["conclusion"]["status"],
        "selected_arm": report["conclusion"]["selected_arm"],
        "passing_arms": report["conclusion"]["passing_arms"],
        "production_config_modified": report["production_config_modified"],
        "mvp_modified": report["mvp_modified"],
        "tuning_modified": report["tuning_modified"],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
