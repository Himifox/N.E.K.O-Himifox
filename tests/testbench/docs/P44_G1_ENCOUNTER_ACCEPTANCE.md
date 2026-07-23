# P44-G1 主动搭话接受度离线分析

> 状态：已完成首份真实 preview cohort 只读报告；结论为 `descriptive_only`。
> 分支：`feat/recommend-testbench`

## 1. 评估单位

唯一评估单位是一次实际投递的主动搭话 encounter。每次 encounter 都包含猫娘
主动发起的聊天，因此回复、继续对话、明确拒绝和设置变更等聊天反馈对所有素材
共同适用：news、meme、vision、普通话题和 music 都不能丢掉这一层。

music 还具有播放完成、跳过、关闭和技术错误等资源行为。报告同时展示：

1. 全部 encounter 的共同聊天反馈；
2. 按素材来源分组的聊天接受度；
3. music 的资源行为反馈；
4. 生产 `reward_score_v2_preview_v2` 的组合 reward。

按素材分组仅用于描述“这类素材承载的搭话是否被接受”，不会把一次普通回复自动
写成 news、meme 或 vision 的长期来源偏好。

## 2. 唯一 reward 来源

Testbench adapter 延迟直接调用生产：

- `join_observations_with_reward_score_v2_preview`；
- `summarize_reward_score_v2_preview`。

Testbench 不复制事件分值或 reward 公式。共同聊天层和 music 资源层只汇总生产
helper 已公开的 component，不重新为事件赋分。

## 3. 输入门禁

- `feedback_state_preview.version == feedback_state_preview_v1`；
- 有效 `lanlan_name + turn_id`；
- `recommendation_mode == shadow`；
- `delivered == true`；
- 最终投递通道为 `chat` 或 `music`；
- 仅使用 `as_of` 截点前的 observation 与 feedback；
- 决策时状态只读 observation 内嵌快照，不读取当前 state 文件。

正式描述性报告至少需要 50 条 preview observation 和 15 个实际投递 encounter。
任一投递通道少于 8 个有效共同聊天反馈时，不对该通道输出 preview 分桶比较。
inferred ignored、纯技术错误、未知事件和归因失败均单独报告，不进入显式聊天
接受度主指标。

## 4. 输出与状态

JSON/Markdown 报告包含：

- 全体、chat/music 投递通道及素材来源的样本量；
- 共同聊天反馈、music 资源行为和组合 reward；
- 正负中性、component、决策时 preview 分桶；
- 时间前后段、技术事件、future cutoff 和归因问题；
- 输入 SHA-256 与固定 `as_of`。

状态仅允许：

- `insufficient_evidence`；
- `descriptive_only`；
- `candidate_for_scheduler_shadow_design`。

最后一种状态也只允许另行评审 scheduler/routing Shadow 设计。本阶段不生成来源
权重、不做来源重排、不修改 MVP、投递、scheduler 或 tuning。

## 5. 后续 HOLD

P44-G2（重复、meme、单候选恢复）、P44-G3（来源多样性）和 OPE/contextual
bandit 继续 HOLD，不因 G1 报告自动启动。

## 6. 首份真实报告

- Freeze：`shadow-p44g1-preview-v1-20260722-151543.json`；
- 分析：`shadow-p44g1-preview-v1-20260722-151543-analysis.json/.md`；
- 输入 SHA-256：`59aaee1297e0b2e40b94c7544fbbdc09ec0787f9dd105a7e885096a1ec40434b`；
- preview observation：260；实际投递 encounter：161；
- 共同聊天显式反馈：chat 35、music 6；正向 41、负向 0；
- inferred ignored：112，保持单独报告；
- 结论：`descriptive_only`。

阻塞原因是 music 的共同聊天反馈少于 8、没有明确负向聊天反馈、决策时 preview
没有形成时间前后方向一致的关系，而且 preview-v1 仍把 music 的聊天与播放证据
放在同一通道状态中，不能完整表达“所有主动搭话共享聊天反馈”。因此本轮不生成
“降低主动搭话”、来源权重或 scheduler Shadow 候选，也不在本分支修改 MVP schema。

## 7. v2 只读同步

2026-07-23 起，Testbench importer 和派生安全视图同时识别
`feedback_state_preview_v1` / `feedback_state_preview_v2`。现有 preview-v1 freeze、
Golden、人工 annotation、分析报告和原始 JSONL 不迁移、不重写，也不据此重新解释
本报告结论。

v2 只读同步只用于后续新数据的安全导入和导出验证。它不让 preview 进入 baseline
排名，不启动新的 G1 候选分析，也不修改生产权重、scheduler 或 tuning。
