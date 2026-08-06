# 主动搭话推荐系统：当前设计边界与应用范围

> 状态：**当前规范（Normative / Single Source of Truth）**
> 文档与生产实现归属分支：`feat/recommend-MVP`
> 当前实现分支：`feat/recommend-MVP`（Testbench 分支生产代码副本已于 2026-07-26 追平截点 `3c0626bf`，observation v3 的 Testbench adapter 同步同日完成）
> 最近更新：2026-08-06
> 当前阶段：P44-G1 第一部分已完成；Testbench 侧 G1-R1/R2 只读模拟已完成，正式状态 `hold_for_negative_evidence`，`candidate_for_shadow=false`

本文只回答四个当前问题：系统已经具备什么、各组件负责什么、当前允许在哪里使用、当前停止点是什么。历史执行记录和远期研究路线不得覆盖本文的当前结论。

## 1. 当前结论

1. 推荐系统的隐私、过滤、日志和反馈关联契约已经可用。
2. P44-E2 裁决后的 Golden Candidate 有效样本为 128，语义校验和 Validator 通过；它支持离线评估，不足以证明生产个性化已经改善。
3. P44-F1 得出 `no_universal_threshold_candidate`：不得把统一分数阈值直接写入生产。
4. timing observation schema v3 已完成只读观测，并冻结首个正式 baseline：105 条有效 observation、30 个显式关联 feedback turn、0 条 timing 契约错误。
5. P44-F2-B 已完成连续变量关联分析；因为同 cohort 人工 `should_recommend` 标签为 0，误打扰和错失机会均不可计算，正式结论为 `no_candidate`。
6. `recent_delivery_count_30m` 与显式反馈分数在本 cohort 中存在稳定相关，但这只是观察性辅助证据，不能替代人工决策标签，也没有生成疲劳公式或候选模拟。
7. 生产推荐权重、调度曲线和投递行为保持不变；`PROACTIVE_RECOMMENDATION_TUNING_MODE=off`。
8. 第一轮四臂评估的三个候选均因来源集中度护栏失败，唯一选择为 `baseline`；候选参数不得进入 MVP。
9. 原基线 `active_source` 只允许开发者通过启动环境显式启用，并可在进程内单向回退到 `shadow`；自动调权、持久个性化、在线探索和普通用户放量仍为 **HOLD**。
10. P44-G0-A～D 已补齐 reward、个人相对回复速度及临时/持久聚合状态 preview；另有默认关闭的可打扰性 Shadow 聚合，它们均不进入排序、PASS、投递或 tuning。
11. `feedback_state_preview_v2` 将全局搭话接受度与实际素材来源偏好分开；该语义拆分已随 G1 第一部分落地（`37c40080`，2026-07-24 补音乐反馈经 preview state 归类的修复 `01514161`/`3c0626bf`），不实现个性化重排。
12. Recommendation Testbench 已完成 `feedback_state_preview` v2 的只读 safe-view 同步（P54，2026-07-23），并于 2026-07-26 将其生产代码副本追平本分支截点 `3c0626bf`（推荐单测 118/118、烟测 p41–p56 14/14 全绿）。observation v3 的 Testbench adapter 同步已于同日完成：导入与安全导出直接采用生产 sanitizer 的 `decision_context.timing` 输出，原 v2 兼容桥接层退役为漂移拒绝门（`production_timing_sanitizer_drift`）——生产侧 timing 语义漂移会在 Testbench 导入 chokepoint 被显式拒绝，而非静默修补。
13. Testbench 侧已完成 P44-G1 首份真实接受度报告（260 observation / 161 投递 encounter，`descriptive_only`）、G1-R1 有界个性化模拟（`impact_only`）与 G1-R2 渐进响应曲线（`gradual_12` 仅过机械门禁，`hold_for_negative_evidence`）；`candidate_for_shadow=false`，三者均不构成本文第 7 节意义上的 MVP 变更申请。

正式 timing-v3 baseline：

- 文件：`shadow-p44f2-timing-v3-baseline-20260721-103709.json`
- SHA-256：`E79E2B3258E55A29109525CDBB00E511EE7B4142E0A204EC40DF8E2961A88BD7`
- 截点：2026-07-21 10:33:52（Asia/Shanghai）
- 分析报告：`shadow-p44f2-timing-v3-baseline-20260721-103709-analysis.md`
- 分析输入摘要：`692c19ef7092ac6b82894fd0f20144909491e0ede56511aa2232a702a7d43fdf`

## 2. 组件边界

| 组件 | 当前职责 | 明确不负责 |
|---|---|---|
| 前端调度器与主动搭话路由 | 决定何时产生一次搭话机会；执行曲线回退、抖动、输入放缓、固定模式及 privacy/activity 等硬门控 | 不向推荐器暴露 backoff level 作为兴趣信号；不由推荐器重复实现时间调度 |
| 推荐器 | 对已经通过硬约束的安全候选做确定性、可解释排序；在 Shadow 中记录候选和分数 | 不新增第二套调度器；不根据 timing v3 直接改变生产排序；不新增生产 `PASS` 阈值 |
| 投递层 | 执行最终内容生成、文本相似度/BM25 去重、投递和现有退出路径 | 不把技术失败当成用户负反馈 |
| Observation / Feedback | 保存脱敏白名单字段；通过 `turn_id` 关联显式反馈；提供 point-in-time 快照 | 不保存完整对话、屏幕原文、截图、私人 URL、token/cookie；不自动写权重 |
| Recommendation Testbench | 重放冻结数据、模拟候选规则、计算 Gate/Rank/Product 指标和准入结论 | 不写生产配置；不把模拟结果自动应用到 MVP；不以缺少标注的指标宣称提升 |
| Tuning | 当前只允许 `off`；历史 preview 只作诊断 | 不启用 `manual`/`auto_safe`，不自动调权、暂停或回滚生产策略 |

边界原则：

- 调度器回答“什么时候产生搭话机会”。
- 推荐器回答“这个机会下哪个安全候选更合适”。
- 路由现有的 skip/pass 继续负责是否实际投递；`should_recommend`/`PASS` 当前只作为 Testbench 评价维度。
- 同一风险只保留一层主要软惩罚和一层最终硬保护，禁止在调度、推荐和投递三层重复扣分。

## 3. 当前推荐器应用范围

### 3.1 允许

- 开发者知情环境中的 `shadow` 数据采集。
- 原生产基线在开发者知情环境中的启动时 `active_source` opt-in；必须保留状态可见性与单向 `active_source -> shadow` 回退。
- Recommendation Testbench 中的离线回放、审计和候选模拟。
- 对 music、news、video、meme、vision 等当前安全候选进行确定性排序。
- 继续使用现有来源、候选 ID、source streak 和投递层去重规则。
- 使用现有人工裁决 Golden 做 Gate 与 Rank 分离评估。
- 通过有效 `turn_id` 对显式反馈计算 `report_score_v2`；来源级 Bandit 使用可重放的 `reward_score_v4_preview`，旧 `reward_score_v2_preview` 仅保留兼容诊断。
- 在 Shadow 中分别保存全局搭话接受度与已验证素材来源 affinity，并把决策前快照写入后续 observation。

### 3.2 暂不允许

- 将第一轮的 `source_calibration`、`delivered_history` 或 `combined` 候选写入生产权重、排序或 tuning。
- 通过运行时 API 将 `shadow`/`off` 提升为 `active_source`，或向普通用户默认开启 `active_source`。
- 在没有新立项的情况下继续修改 `feat/recommend-MVP` 或 `feat/recommend-testbench` 的推荐策略、调度或反馈行为。
- 新增推荐层时间硬门控或再次实现调度回退。
- 把 `backoff_level`、`backoff_tier`、scheduler policy 等调度内部状态加入推荐画像。
- 将一次回复、一次播放或一次快速响应持久化为长期兴趣。
- 将通用回复、继续对话或 inferred ignored 解释为 Music、News、Meme 或 Vision 来源偏好。
- 让 `reward_score_v2_preview` 或相对回复速度 bonus 影响候选分数、PASS、投递、tuning 或持久画像。
- 引入 MMR、DPP、向量数据库、Feature Store、MABWiser/Mab2Rec、OBP 或其他新运行时依赖。
- 启用 epsilon exploration、contextual bandit、OPE 或多用户 Canary。
- 要求本轮覆盖 `gaming`；`away`/`busy` 只在自然出现时记录。
- 为了重新分批而删除、归档或轮转现有 observation/feedback 日志；批次只用 immutable freeze/cutoff 划分。

### 3.3 证据适用范围

- 当前数据主要来自单一开发者/本地角色，不代表多用户总体效果。
- 现有 timing-v3 cohort 的有意义 activity 主要是 `idle` 和 `chatting`；`unknown` 不得算作个性化覆盖。
- `focused_work` 缺失或样本不足时，只能报告限制，不能外推该状态效果。
- 显式反馈稀疏；`inferred ignored` 不能计入显式反馈门槛，也不能直接作为强负例。

## 4. timing v3 的准确语义

当前 observation 的 `decision_context.timing` 子对象只允许以下字段：

```json
{
  "decision_context": {
    "timing": {
      "configured_interval_seconds": 60,
      "elapsed_since_last_delivery_seconds": 143,
      "recent_delivery_count_30m": 2,
      "recent_delivery_count_2h": 5,
      "consecutive_unanswered_deliveries": 1
    }
  }
}
```

- `configured_interval_seconds` 是用户设置的基础间隔；当前 UI 常规范围为 10–120 秒。
- `elapsed_since_last_delivery_seconds` 是实际成功投递间隔，可能因曲线回退、跳过或运行状态而超过 120 秒。
- 30 分钟/2 小时计数描述全局主动投递负载，不等同于仅由 Recommendation 产生的曝光。
- 连续未回应数只统计 Recommendation pending feedback 窗口内尚未收到显式回复的投递。
- 这些字段目前只用于观测和 Testbench；生产排序不得读取它们。

不新增 schema v4 调度字段。若未来需要独立审计调度器，应建立调度器自己的测试契约，不扩张 Recommendation observation。

## 5. P44-F2 结项与当前停止点

P44-F2 的两侧工作均已结束：

1. MVP 侧完成五个 timing v3 只读字段，没有改变调度或排序。
2. Testbench 侧删除了 `5/10/30` 分钟绝对间隔桶门禁，改用连续秒数关联分析。
3. 105/30 freeze 可复现，P48 同时覆盖有标签的 synthetic positive control 与真实无标签 cohort。
4. 真实 timing cohort 没有同批人工 `should_recommend` 标签，因此不能计算误打扰或错失机会，也不能据此提出 production/Shadow fatigue candidate。
5. 正式状态为 `no_candidate`；没有生成候选公式，没有运行真实 cohort 候选模拟，也没有生产写入。

当前停止点：

- 不因为 `no_candidate` 自动转入重复惩罚、来源多样性、个人回复时延或持久兴趣；
- 不为了得到候选而用 `delivered`、feedback join 或缺失反馈替代人工标签；
- 若要重启 timing/fatigue 研究，必须先单独决定是否为同 cohort 补充合规人工决策标签或重新采集带标签数据；
- 任何新方向都需要新的目标、分支归属和验收门禁，不沿用 P44-F2 的授权。

### 5.1 第一轮选择与第二轮 MVP 收口

第一轮在同一冻结数据上比较 `baseline`、`source_calibration`、`delivered_history` 和 `combined`。三个候选虽然改善部分排序指标，但均触发 HHI 与最大来源曝光护栏，因此正式结论为 `baseline_retained`。第二轮不是新一轮调参，而是把该结论落实为可安全试用的 MVP：

1. 修正活动状态接线，推荐器读取真实 `activity_snapshot.state`，不再误用压缩后的 `propensity`。
2. `active_source` 只接受进程启动时的开发者环境配置；不存在运行时提升接口。
3. 暴露非敏感的 configured/effective mode 状态，便于确认实际运行模式。
4. 提供进程内单向回退到 `shadow`；回退不写配置，重启后仍以启动配置为准。
5. 保持 PASS、搭话调度、投递去重、来源权重和 `PROACTIVE_RECOMMENDATION_TUNING_MODE=off` 不变。

第二轮完成只代表“原基线可供开发者 opt-in 验证”，不代表候选调参通过、普通用户放量或生产效果得到多用户证明。

### 5.2 P44-G0-A：反馈奖励预览

P44-G0-A 是学术路线中“显式反馈归因 → 个性化状态”之间的第一步，仅冻结奖励语义和可审计输出：

1. 原始 feedback event 与 `report_score_v1` 保持不变；报表、校准和调权统一从原始事件派生 `report_score_v2`。
2. 新回复只写 `user_reply`；历史 `user_reply_fast` 与 `user_reply` 在 v2/v3 中均计 `+0.15`，回复时延不再作为质量奖励。
3. `user_continue`、音乐完成/播完、明确关闭等事件按独立 component 计算；`ignored` 与 `mini_game_ignored` 视为缺失/右截尾，不进入均分、正负率、校准、active-ready、Bandit 或调权。
4. `music_error` 与 `autoplay_blocked` 明确计 0，不得污染偏好。
5. feedback 必须通过 `lanlan_name + turn_id` 与已投递 observation 关联，并校验来源及可验证的 candidate ID；归因失败的事件不计 reward。
6. `/api/proactive/recommendation/summary` 同时保留旧 v2 诊断并输出来源专用的 `reward_score_v4_preview`；`user_reply` 与 `user_continue` 只更新全局搭话接受度，不进入来源 reward。Bandit 只消费 v4，排序、PASS、投递和 tuning 不读取旧 v2。
7. Bandit 状态使用 `recommendation_bandit_state_v2` 与独立 v2 文件冷启动；旧 v1 文件保持只读，不迁移、不改写，也不与新 reward 合同混用。

### 5.3 P44-G0-B：个人相对回复速度预览

P44-G0-B 作为旧 `reward_score_v2_preview` 的兼容诊断保留，不进入质量、校准、调权、来源偏好或 Bandit：

1. 基线从当前日志窗口内、归因有效且早于本次回复的历史延迟重放，不新增画像文件或逐条延迟副本。
2. 少于 5 次有效历史回复时，relative-speed component 保持 0。
3. 达到 5 次后，对 `log(1+latency)` 使用中位数与 MAD；只对快于本人历史分布的回复给最高 `+0.05` bonus。
4. 与本人基线相同或更慢时不扣分；因此慢性子不会因超过固定 60 秒而被判为低兴趣。
5. 结果继续只在 summary 中审计，`personalization_state_consumed=false`，生产排序、PASS、投递、tuning 和持久画像均不读取。

临时/持久状态的生产消费、衰减以及 reward 对排序的影响仍属于后续独立步骤，不在 P44-G0-B 授权内。

### 5.4 P44-G0-C/D：临时/持久状态与 observation preview

可打扰性实验与质量状态分离：`PROACTIVE_RECOMMENDATION_AVAILABILITY_MODE=off|shadow` 默认 `off`。Shadow 的长期状态只保存带 30 天半衰期的聚合统计，按投递时活动状态、输入方式和本地六小时时段分桶；10 分钟无回复记右截尾。为避免服务重启丢失截尾曝光，同一状态文件还短暂保存最长 10 分钟的哈希曝光键、投递时间和分桶上下文，完成回复或截尾后立即删除；不保存原始 turn ID、来源、回复正文或对话文本。精确桶少于 30 次原始曝光或 10 次原始回复时，依次回退到活动状态、输入方式和全局；30 天衰减权重只用于响应率和延迟估计，不用于样本门槛。每次投递的 observation 会记录当时的 `available/uncertain/unavailable/insufficient` 与反事实 `1x/2x/4x` 间隔倍率，但 scheduling、interval、gate consumed 始终为 false。

1. 临时兴趣只在进程内保存，TTL 为 2 小时；不同显式反馈可累积，但过期后自动删除。
2. 历史 v1 文件 `proactive_recommendation_feedback_state_preview.json` 保持只读；v2 使用独立文件 `proactive_recommendation_feedback_state_preview_v2.json` 保存聚合证据，两者均不保存 turn ID、回复正文、标题、URL 或逐条 latency。
3. 单条显式证据只改变临时兴趣；持久证据少于 3 条时 `affinity_preview=0`。同一 turn 的同组反馈不重复累计持久证据。
4. 状态仅由成功写入 JSONL、可归因到已投递 Shadow observation 的 feedback 更新；孤儿反馈、技术零分和 `active_source` 不更新状态。
5. 每条新的 Shadow observation 记录决策前 `feedback_state_preview`；字段经过独立白名单与数值边界清洗。
6. preview 不进入候选 `score_breakdown`，推荐排序、PASS、投递、tuning 和生产权重均不读取，因此当前 baseline 排序不变。

实际个性化消费、持久 affinity 衰减及生产启用仍需独立候选评估，不在 P44-G0-C/D 授权内。

### 5.5 P44-G1 第一部分：feedback preview v2 语义边界

1. `conversation_acceptance` 只描述用户是否接受本次主动搭话。通用回复、继续对话与关闭主动搭话只更新该状态，不更新任何素材来源 affinity。
2. `source_affinity` 只接受实际投递素材的可验证来源行为；当前实现支持具有 candidate ID 的 Music 行为及明确来源关闭事件。
3. `ignored` 仅保留为可打扰性观测，不写入质量或 v2 临时/持久状态；`music_error`、`autoplay_blocked` 和其他技术零分也不更新状态。
4. 来源 affinity 必须同时匹配 pending `turn_id`、实际来源与 candidate ID；Shadow 中未实际投递的候选不得获得偏好证据。
5. 临时 TTL 继续为 2 小时，持久 preview 继续要求至少 3 条合格证据；同一 turn 的同组事件不重复累计持久证据。
6. v1 不迁移为 v2 来源偏好，避免继承“愿意聊天等于喜欢素材”的旧语义；v1 状态文件和历史 observation 保持只读，原始 JSONL 不删除、不重写，v2 从独立文件的冷状态开始。
7. v2 仍标记 `ranking_consumed=false` 和 `tuning_consumed=false`，不改变 baseline 分数、PASS、scheduler、投递或 tuning。

本部分不包含个性化调整公式、Shadow 重排、News/Meme/Vision 推断偏好、持久衰减、新反馈 UI 或 Testbench 候选分析。

本部分已完成：语义拆分随 `37c40080` 落地（2026-07-23），音乐反馈经 preview state 归类的两项修复（`01514161`、`3c0626bf`）于 2026-07-24 收尾。

### 5.6 P44-G1 Testbench 侧 R1/R2（只读模拟，无 MVP 变更）

R1/R2 在 `feat/recommend-testbench` 上完成，证据文档为
`tests/testbench/docs/P44_G1_R1_BOUNDED_PERSONALIZATION.md` 与
`tests/testbench/docs/P44_G1_R2_PERSONALIZATION_RESPONSE_CURVES.md`：

1. R1 对决策前快照中 `source_affinity.persistent` 做 ±0.03 硬裁剪的有界模拟，结论 `impact_only`：20 个 warm-state Music 候选平均分 0.5614→0.5658，Top-1 翻转 0，HHI 与最大来源曝光不变。
2. R2 解释 R1 的恒定 +0.006（生产 affinity 达证据门槛后固定 0.2），并比较证据置信度渐进曲线；`gradual_12` 仅通过机械门禁（Music 平均分 0.5614→0.5755，触顶 0%），正式状态 `hold_for_negative_evidence`。
3. 当前来源证据仅 Music 正向 11 / 负向 0，缺人工 outcome 与反事实标签；`candidate_for_shadow` 固定为 false。
4. 两轮均不修改生产权重、排序、PASS、投递、scheduler 或 tuning，也不沿用为第 7 节的 MVP 变更授权；下一步属于研究 Backlog 的"定向补充真实负向来源证据"，需单独立项。

### 5.7 P45-R3：稀疏来源反馈校准

P45-R3 修复 Music 可自动获得大量播放证据、News/Meme 主要依赖稀疏明确反馈所造成的跨来源偏移，保持候选构建、基础评分、过滤、PASS、调度、投递和 tuning 不变：

1. `source_preference_score_v2` 以逐条 outcome 为唯一计算依据；旧聚合桶仅保留审计，不再参与个性化积分，原始 feedback/observation 日志不删除、不重写。
2. 明确的来源喜欢、不喜欢、疲劳、关闭来源，以及可验证候选的明确拒绝，归为 `explicit_source`；每一份净有效证据贡献 `0.005`，来源积分限制在 `±0.03`。
3. Music 播完、高/中完成度、早关和秒关归为 `resource_behavior` 辅助证据；它们使用更低强度，Music 行为积分限制在 `±0.01`，不能仅凭自动播放行为长期压过其他来源。
4. 来源偏好统一使用 7 天半衰期，使近期明确反馈更快生效，旧偏好逐周减半；Bandit posterior 继续使用独立的 30 天半衰期，P45-R3 不改变 Bandit 决策合同。
5. 普通回复、继续聊天、没有反馈、技术失败和 inferred ignored 均不形成来源偏好；缺少反馈保持中性。
6. 同一物理状态文件和 `recommendation_preference_state_v1` 版本继续兼容读取；`preference_score_contract` 升为 v2，并在 Summary 暴露显式证据、资源行为证据、当前采用的信号基础和迁移诊断。
7. Bandit 保持 Shadow/default-off 边界；本次只校准来源个性化积分，不启用探索、不修改生产权重，也不声称已证明线上推荐效果提升。

## 6. Testbench 准入原则

硬门禁：

- forbidden/privacy/URL/secret/candidate alignment/version 错误为 0；
- execution error 和 hard constraint violation 为 0；
- observation timing 字段有效；
- 反馈只按有效 `turn_id` 显式关联；
- baseline 与 candidate 使用同一 freeze 配对比较；
- production config、weights 和 tuning 均未修改。

质量判断：

- Gate 与 Rank 分开报告；负例不计算排序命中。
- 必须同时报告分子、分母和样本不足限制。
- 当前小样本不使用缺少功效依据的 `+2pp`、`-0.01` 等伪精确生产阈值。
- 候选若减少误打扰但明显增加 missed opportunity，应判定为 HOLD/NO-GO。
- 没有稳定改善证据时结论是 HOLD，不是“未观察到退化即通过”。

## 7. 进入 MVP 修改的条件

只有同时满足以下条件，Testbench 候选才可申请单独的 MVP 变更：

1. 候选公式、参数范围和适用 activity 已冻结。
2. 在同一 Golden/freeze 上相对基线有可解释改善，且隐私、执行、硬约束和排序护栏不退化。
3. 改动只涉及一个职责清晰的机制，可通过 feature flag 或常量回滚。
4. 新代码先保持 Shadow/preview，不立即改变实际投递。
5. 新 Shadow cohort 复核后，再单独决定是否进入开发者 opt-in。

通过这些条件只代表“允许提交一个最小 MVP 候选”，不等于允许自动调权或全量上线。

## 8. 研究 Backlog（未批准实施）

以下项目保留研究价值，但不是当前或默认下一阶段：

- 定向补充真实负向来源证据（当前 Music 亲和度证据为正向 11 / 负向 0，是 G1-R1/R2 效果验收的共同阻塞项）；
- 临时/持久 preview 的实际消费与持久 affinity 衰减；
- `reward_score_v2` 对画像或排序的实际消费（G0-A～D 仅批准 preview）；
- semantic repeat、MMR 与单候选恢复；
- propensity 日志、contextual bandit 与 OPE；
- 多用户 A/B、Canary 和自动 tuning。

每一项必须基于新的证据和独立设计评审立项，不能因出现在研究路线文档中而自动进入开发计划。

## 9. 文档角色

- 本文：当前事实、设计边界、应用范围和停止点。
- `proactive-recommendation-mvp-p0-p1-plan.md`：历史 P0/P1/P44 执行与决策记录，不再作为当前计划。
- `shadow-round-2-structure-audit.md`：不可变历史审计快照。
- `proactive-recommendation-academic-technical-route.md`：远期研究依据与 Backlog，不构成实施授权。
