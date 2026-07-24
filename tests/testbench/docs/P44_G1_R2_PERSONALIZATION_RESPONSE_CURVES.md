# P44-G1-R2 · 渐进式个性化积分响应曲线

状态：`complete / hold_for_negative_evidence`

分支：`feat/recommend-testbench`

完成日期：2026-07-24

## 目的与边界

R2 解释 R1 的 `+0.006 / 0 Top-1 flip`，并比较有证据置信度的渐进积分曲线。
它只读取 observation 内嵌的 point-in-time `feedback_state_preview_v2`：

- 不读取或写入当前生产 state；
- 不改变候选、过滤、基础分、生产权重、PASS、投递或 tuning；
- 从生产模块延迟读取 persistent affinity 上限和最低证据数，避免合同漂移；
- `conversation_acceptance` 不参与来源相对排序；
- 最大绝对积分始终为 `0.03`。

生产 preview 的 affinity 描述正负证据方向，在全正向证据下达到门槛后固定为 `0.2`。
R1 因此始终得到 `0.2 × 0.03 = +0.006`。R2 不改变该状态，只在 Testbench
候选中增加证据置信度：

```text
direction = affinity_preview / production_affinity_max
confidence = min(1, evidence / saturation_evidence)
delta = clip(direction × confidence × 0.03, -0.03, +0.03)
```

固定比较 `current_v1`、`gradual_8`、`gradual_12` 和 `gradual_20`，默认机械候选为
`gradual_12`。

## 首次真实运行

- Freeze：`shadow-p44g1-v2-20260724-112124.json`；
- observation：131；feedback event：23；
- R2 输入 SHA-256：`cf5c2fefa7e8f1bae71189e18029e0c4ffa1bf99bec51cb9709f7105999fa7f9`；
- 可调整 Music 候选：20，其中 15 条原本已经是 Top-1；
- 其余 5 条距 Top-1 为 0.034、0.048、0.073、0.088、0.108，均超过 0.03 上限；
- R1 baseline/current_v1 对照完全一致；硬违规为 0。

| Variant | 中位积分 | P90 | 最大值 | Top-1 flip | Top-3 换位 | 触顶率 |
|---|---:|---:|---:|---:|---:|---:|
| current_v1 | 0.0060 | 0.0060 | 0.0060 | 0 | 1 | 0% |
| gradual_8 | 0.0300 | 0.0300 | 0.0300 | 0 | 1 | 60% |
| gradual_12 | 0.0200 | 0.0253 | 0.0275 | 0 | 1 | 0% |
| gradual_20 | 0.0120 | 0.0152 | 0.0165 | 0 | 1 | 0% |

`gradual_12` 下 Music 全部候选平均分从 0.5614 增至 0.5755，平均增量
`+0.0142`。HHI 保持 0.3054，最大来源曝光保持 45.04%。它通过登记的机械门禁；
`gradual_8` 因 60% 可调整候选触顶而过强。

正式状态仍为 `hold_for_negative_evidence`：当前来源证据只有 Music 正向 11、负向 0，
也没有反事实或人工 outcome 标签。该结果证明 `gradual_12` 在当前样本上“渐进且可测”，
不证明它改善用户体验，不产生 Shadow 或生产候选。

## 产物与验收

只读 API：

```http
POST /api/recommendation-testbench/personalization/response-curves
```

CLI 原子生成 JSON 与 Markdown 派生报告，不覆盖 Freeze、Golden、日志或 R1 报告。
Markdown 包含每条 observation 的完整资源分数、证据轨迹及非 Top-1 Music 分差。

`p56_personalization_response_curve_smoke.py` 固定验证最低证据门槛、3/6/8/12/20
证据轨迹、正负对称、混合证据回落、±0.03 上限、Top-3 换位、单候选、未来信息隔离、
R1 一致性、严格 JSON 和逐资源 Markdown 输出。

下一步不是修改 MVP，而是定向补充真实负向来源证据；在正负信号与人工结果标签同时
具备前，`candidate_for_shadow` 固定为 false。
