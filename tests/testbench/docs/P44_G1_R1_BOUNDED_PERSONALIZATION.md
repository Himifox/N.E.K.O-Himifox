# P44-G1-R1 · 有界个性化排名影响模拟

状态：`complete / impact_only`
分支：`feat/recommend-testbench`
完成日期：2026-07-24

## 边界

R1 只模拟 observation 决策前快照中的
`feedback_state_preview_v2.source_affinity.persistent` 对安全候选相对分数的影响。

- 候选：`persistent_source_affinity_max_003_v1`；
- `delta = affinity_preview × 0.03`，并硬裁剪到 `[-0.03, +0.03]`；
- 证据总数低于 `min_explicit_evidence` 时 delta 为 0；
- `conversation_acceptance` 是所有主动搭话共享状态，只展示，不改变来源相对排名；
- 不增加或恢复候选，不绕过过滤，不读取当前 state 文件；
- 不修改生产权重、PASS、scheduler、投递或 tuning。

输出状态只允许 `impact_only` 或 `insufficient_evidence`。没有反事实或人工结果标签
时，禁止输出“个性化有效”或 `candidate_for_shadow`。

## 首次真实运行

- Freeze：`shadow-p44g1-v2-20260724-112124.json`；
- Freeze observation：131；feedback event：23；
- 输入 SHA-256：`7d5be2e7c985cdf880f73fc1b26b8405b3d3fdaa626725ae237493ea621bf49f`；
- Warm-state observation：20；
- 调整候选：20；Top-1 翻转：0；
- Music 平均分：0.5614 → 0.5658，平均 +0.0044，最大实际 +0.0060；
- 其他来源平均分不变；
- HHI：0.3054 → 0.3054；最大来源曝光：45.04% → 45.04%；
- source evidence：music 正向 11、负向 0；
- 结论：`impact_only`，`candidate_for_shadow=false`。

该结果说明当前有界候选对排名几乎没有行为影响，不说明它提升了推荐效果。缺少负向
来源证据、人工 outcome 标签和反事实信息仍是正式效果验收的阻塞项。

## 验收

`p55_bounded_personalization_smoke.py` 固定验证：

1. 决策时快照与 `as_of`，未来 observation 不泄漏；
2. 冷状态不调整，达到证据门槛的来源才调整；
3. 任何 candidate delta 不超过 ±0.03；
4. conversation acceptance 不进入来源排序；
5. 候选集合不变，输入可重复、无非有限分数；
6. 输出包含逐来源分数、曝光、HHI 和 Top-1 翻转；
7. 生产 config、ranking 和 tuning 均未修改。
