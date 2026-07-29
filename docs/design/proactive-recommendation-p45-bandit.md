# P45 主动搭话个性化推荐与来源级 Bandit

> 状态：MVP 工程实现完成，默认关闭；Shadow 可用；Canary 等待数据准入。
> 生产分支：`feat/recommend-MVP`
> 离线评估分支：`feat/recommend-testbench`
> 日期：2026-07-29

## 1. 已实现闭环

```text
既有 Scheduler / PASS / Privacy / Activity
→ 安全候选与确定性基础分
→ recommendation_preference_state_v1 / gradual_12
→ source_epsilon_greedy_v1
→ active_source
→ 显式来源反馈与 Music 自然行为
→ 偏好状态更新
→ Testbench 策略比较与 OPE
```

Bandit 的 arms 固定为 `news / music / meme`。Vision 保留原非对称屏幕路径，Video 因证据不足暂不加入。策略不产生候选、不解除过滤，也不控制 PASS、调度、隐私、Activity、去重或冷却。

## 2. 生产契约

- `PROACTIVE_RECOMMENDATION_BANDIT_MODE=off|shadow|canary`，默认 `off`。
- Canary 还要求 `PROACTIVE_RECOMMENDATION_MODE=active_source` 与 `PROACTIVE_RECOMMENDATION_PERSONALIZATION_MODE=active`；条件不完整时自动降为 Shadow。
- ε 固定 `0.05`，仅在来源最高分与 Top-1 相差不超过 `0.03` 时探索。
- Observation v5 的 `policy_decision` 保存完整 eligible arms、near-tie arms、选择、每臂概率、chosen propensity、后验和策略版本。
- `activity_propensity` 仍是 Activity 状态，不是动作概率。
- 现有运行时回滚把 `active_source` 单向降为 `shadow`，同时停止 Canary 对实际来源的影响。

正式状态使用 `recommendation_preference_state_v1`：

- 30 天半衰期；Beta(2,2) 后验。
- 3 条有效证据后启用、12 条饱和、单来源积分限制 `±0.03`。
- 每个 `turn_id + source` 只保留一个 outcome；显式来源反馈优先于 Music 自然行为。
- 原 `feedback_state_preview_v2` 保持可读，并只在首次创建正式状态时迁移聚合证据，原文件不改写。
- 用户可以通过本地受保护 API 查看或重置正式偏好状态。

## 3. Reward 与评估

来源 outcome 使用生产唯一 helper：喜欢 `+1`，不喜欢/关闭来源 `-1`，Music 播完/高完成 `+1`，中完成 `+0.5`，早关/秒关 `-1`；正常关闭、技术错误和 inferred ignored 为中性。

Testbench 比较 deterministic baseline、active personalization、ε-greedy、Thompson、UCB 和 Softmax。只有带非退化动作概率且存在有效来源 outcome 的投递行进入 OPE；旧 Freeze 明确返回 `not_ope_eligible`。报告包含 Replay、IPS、SNIPS、DR、ESS、最大 importance weight、bootstrap 95% CI、来源曝光和 HHI。

## 4. Canary 门禁

进入 5% Canary 前：硬约束/隐私违规为 0；动作概率合法；反馈精确归因且无重复累计；重置与运行时回滚可用。

扩大前至少需要 200 个可探索 encounter，每个 arm 至少 30 次曝光且有正负反馈，ESS ≥ 100；OPE reward 点估计提升 ≥ 5% 且 95% CI 下界 ≥ 0；nDCG@3 下降 ≤ 0.02、最大来源曝光增加 ≤ 5pp、HHI 增加 ≤ 0.02。系统不会自动扩大 Canary。

当前工程完成不等于效果验收完成。没有满足上述数据门禁时，结论保持 `shadow_only` 或 `insufficient_support`，生产配置仍应保持 Bandit `off`/`shadow`。
