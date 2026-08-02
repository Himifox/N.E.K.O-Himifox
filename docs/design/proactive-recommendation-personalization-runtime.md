# 主动搭话个性化推荐运行合同

> 状态：已实现，默认关闭
> 更新：2026-07-28

## 功能边界

个性化只改变候选资源之间的相对排序，不负责判断是否启动主动搭话，不修改
PASS、scheduler、activity、隐私、去重、生成、投递或 tuning。

个性化只消费 `feedback_state_preview_v2.source_affinity.persistent.sources`：

- Music 完整播放、完成度、秒关和早关可形成精确资源证据；
- “少推荐此类内容”只有在 turn/source/candidate 与实际投递精确匹配时形成负向证据；
- 普通回复和继续聊天属于全局聊天接受度，不会被错误算成 news、meme、vision 或 music 偏好；
- inferred ignored、技术错误、候选错配和无 pending 记录不形成来源偏好。

## 渐进积分

少于 3 条持久证据时不调整。达到门槛后采用已登记的 `gradual_12`：

```text
direction = affinity_preview / 0.20
confidence = min(1, evidence_count / 12)
delta = clip(direction × confidence × 0.03, -0.03, +0.03)
final_score = clip(baseline_score + delta, 0, 1)
```

全正向证据下，第 3、6、12 条对应 `+0.0075、+0.015、+0.03`；负向证据
按同一公式对称回落。状态在每个 recommendation turn 开始时冻结，后续反馈不能回写
该 turn 的排序，避免未来信息泄漏。

## 运行模式

```text
PROACTIVE_RECOMMENDATION_PERSONALIZATION_MODE=off
PROACTIVE_RECOMMENDATION_PERSONALIZATION_MODE=shadow_compare
PROACTIVE_RECOMMENDATION_PERSONALIZATION_MODE=active
```

- `off`：完全沿用 baseline，Observation 不增加个性化字段；
- `shadow_compare`：记录 baseline 与个性化反事实分数，不改变实际排名；
- `active`：按个性化最终分数排序。

真正影响投递还必须同时设置：

```text
PROACTIVE_RECOMMENDATION_MODE=active_source
```

主动推荐后的显式反馈按钮已取消，避免每次投递额外插入反馈消息打扰用户。
个性化继续从明确的文字反馈与音乐播放、关闭等自然行为中学习。

当且仅当个性化积分改变 Top-1 时，该结果可作为接近候选的平局裁决，不再被旧的
0.05 分差门二次挡回；没有个性化 Top-1 翻转时，原分差门保持不变。

## 可观测与回滚

Observation v4 记录 `personalization.mode`、是否消费、baseline/个性化 Top-1，
以及每个候选的 baseline score、delta、final score 和前后名次。合法安全导出保留
`ranking_consumed`，但 tuning 始终不消费该状态。

回滚只需将 `PROACTIVE_RECOMMENDATION_PERSONALIZATION_MODE=off` 并重启；历史状态、
Feedback、Freeze 和日志不删除、不改写。
