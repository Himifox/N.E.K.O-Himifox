# 主动搭话推荐系统 MVP：P44-G0 阶段性汇报

> 文档性质：**阶段性标记（Snapshot / Non-normative）**
> 适用分支：`feat/recommend-MVP`
> 标记阶段：`P44-G0`
> 记录日期：2026-07-22
> MVP 截点：`6f3952ce feat(recommendation): add shadow feedback state preview`
> 配套 Testbench 截点：`ae5f9193 feat(testbench): sync feedback state preview contract`
> 当前规范：[proactive-recommendation-current-scope.md](./proactive-recommendation-current-scope.md)

## 0. 使用声明

本文只记录主动搭话推荐系统在上述截点的功能、评分公式、证据和限制，作为后续评审时的阶段性参照。

本文：

- 不替代当前规范、产品配置或学术技术路线；
- 不授权修改生产权重、阈值、调度曲线、PASS、投递或 tuning；
- 不授权 P44-G1/G2/G3、自动个性化、在线探索或普通用户放量；
- 不把 Shadow preview 描述为已经进入生产排序的个性化能力；
- 不因后续实现变化而自动更新，引用时必须同时注明截点。

## 1. 阶段结论

截至本阶段，Recommendation MVP 已形成可运行、可解释、可审计、可回滚的主动搭话素材排序闭环，能够支持受控的内部开发者 Dogfood。

当前系统的准确定位是：

> **调度器已经产生一次搭话机会后，对安全候选素材进行确定性排序，并在满足保守门槛时影响实际素材通道。**

它目前不是：

- 独立决定何时主动搭话的第二套调度器；
- 根据反馈自动调权的在线学习系统；
- 已经消费临时/持久兴趣的个性化推荐器；
- 已获准默认向普通用户开启的成熟推荐系统。

阶段状态：

| 维度 | 当前判断 |
|---|---|
| 工程闭环 | 已完成 MVP |
| 隐私、过滤、日志、反馈归因 | 已具备并通过既有契约验证 |
| Shadow 观测 | 可用 |
| 开发者 `active_source` opt-in | 可用于受控内部测试 |
| 个性化反馈状态 | Preview-only，不影响行为 |
| 自动 tuning / 在线学习 | `HOLD / off` |
| 普通内部用户默认开启 | `HOLD` |
| 外部或生产放量 | `HOLD` |

## 2. 当前运行路径

1. 现有调度器产生搭话机会。
2. 执行 privacy、activity、来源开关等硬门控。
3. 构建候选素材。
4. 过滤危险、关闭、重复或不适合的候选。
5. 执行可解释线性评分。
6. 应用来源重复、连续曝光与候选重复惩罚。
7. `active_source` 满足门槛时偏置实际素材通道。
8. 投递层生成搭话文本并执行最终去重。

组件边界保持不变：调度器回答“何时产生机会”，推荐器回答“这次机会下哪个安全候选更合适”，投递层负责最终文本生成、相似度/BM25 去重和已有退出路径。

## 3. 已进入实际排序的功能

### 3.1 候选来源

推荐器能够构建或识别以下候选：

- `news`、`video`、`home`、一般 `web`；
- `music`；
- `meme`；
- `vision` / 屏幕上下文；
- `personal` / 个人动态；
- `topic_hook` / 兴趣话题；
- `mini_game` / 小游戏邀请。

当前 `active_source` 可直接映射到既有 Phase-2 通道的来源主要是 WEB、MUSIC 和 MEME。Vision、topic hook 与小游戏仍受原有路由和投递逻辑控制。

### 3.2 硬过滤与风险处理

排序前会处理：

- 来源是否启用；
- privacy-sensitive 来源与 screen/privacy 风险；
- 明确 duplicate 风险；
- busy、away 等活动状态；
- placeholder 与高风险话题；
- 候选是否存在可用素材。

硬过滤优先于评分。被过滤候选不会因为其他维度得分高而重新进入排序。

### 3.3 评分公式

当前实现位于 [main_logic/proactive_recommendation/](../../main_logic/proactive_recommendation/)。

最终分数的计算方式如下：

1. 将所有基础加分项相加。
2. 减去打扰成本、风险惩罚和多样性惩罚。
3. 加上来源固定调整和 tuning 调整。
4. 最终结果限制在 `0～1` 之间。

简写为：**最终分数 = 基础加分 − 成本与风险 − 多样性惩罚 + 来源调整 + tuning 调整**。

各项语义如下：

| 项目 | 系数或范围 | 当前作用 |
|---|---:|---|
| `sourceWeight` | `+0.20` | 最近使用较少的来源获得更高相对权重 |
| `freshness` | `+0.15` | 新鲜、完整素材高于旧素材或 placeholder |
| `contextMatch` | `+0.25` | 当前 activity 与候选是否匹配 |
| `interestMatch` | `+0.15` | 当前静态兴趣估计；不是 G0 反馈画像 |
| `novelty` | `+0.15` | 最近未使用的来源获得优势 |
| `quality` | `+0.10` | 标题、URL、来源等素材完整度 |
| `interactionValue` | `+0.05` | 候选的预设互动潜力 |
| `interruptionCost` | `-0.25` | 忙碌或受限状态下的打扰成本 |
| `riskPenalty` | `-0.30` | 屏幕、隐私、placeholder、风险话题等 |
| `sourceAdjustment` | 直接加减 | 当前 News 固定调整为 `-0.05` |
| `tuningAdjustment` | `[-0.15, +0.15]` | 当前 tuning 为 `off`，实际为 0 |
| `diversityPenalty` | 最多 `-0.30` | 来源重复、连续曝光和相同候选重复 |

### 3.4 Activity 对排序的影响

- 普通 idle/chatting 下，普通候选通常取得 `contextMatch=0.7`；
- focused work、gaming 或 restricted 状态下，Vision/window/topic hook 的上下文匹配和打扰成本优于其他普通素材；
- stale returning 下，topic hook 与 personal 获得更高上下文匹配，整体打扰成本降低；
- busy/away 等状态除特定来源外可直接过滤候选。

活动状态主要通过 `contextMatch` 与 `interruptionCost` 同时改变排序，而不是形成新的时间调度 gate。

### 3.5 重复与来源多样性

当前重复处理包括：

1. `novelty` 随最近同来源出现次数下降：`1.0 → 0.65 → 0.30 → 0.15`；
2. 最近 8 次 Shadow 来源中，同来源每出现一次扣 `0.04`，最高扣 `0.16`；
3. 同来源连续出现每次扣 `0.06`，最高扣 `0.12`；
4. 相同 candidate 再次出现扣 `0.12`；
5. diversity penalty 合计最高扣 `0.30`；
6. 投递层继续执行文本相似度/BM25 最终去重。

因此系统已经具有基础重复控制，但尚未引入 semantic repeat、MMR、DPP 或单候选恢复公式。

### 3.6 来源历史权重

既有来源层使用最近一小时投递历史计算衰减权重：

1. 对每次历史投递，根据距离当前时间的秒数计算指数衰减值：`exp(-0.002 × age_seconds)`。
2. 将同一来源的全部衰减值相加，得到 `raw_score`。
3. 计算该来源的新鲜度：`freshness = 1 ÷ (1 + 0.30 × raw_score)`。
4. 最后在所有候选来源之间归一化。

各来源 freshness 随后归一化。近期使用越频繁的来源，进入候选和评分时的相对权重越低。只剩一个来源时不会因该机制被剔除。

实现位于 [main_routers/system_router/proactive_sources.py](../../main_routers/system_router/proactive_sources.py)。

### 3.7 `active_source` 应用门槛

即使某个候选排名第一，只有同时满足以下条件才会实际偏置素材通道：

- 当前决策位于 Phase-1 material 阶段；
- 来源可映射为 WEB、MUSIC 或 MEME；
- 候选存在真实素材链接；
- diversity penalty 小于 active overuse 门槛；
- Top-1 与 Top-2 的分差达到配置门槛，当前默认 `0.05`。

任一条件不满足时，系统保留原有路由结果，不强制应用推荐偏置。

## 4. 已实现但尚未改变推荐的 G0 能力

P44-G0-A～D 已完成以下 Shadow-only 能力：

- 可重算的 `reward_score_v2_preview`；
- 回复、继续话题、音乐完成/播完等反馈 component；
- 技术错误与 autoplay failure 不污染偏好；
- 基于用户本人历史的相对回复速度 bonus；
- 少于 5 条有效历史回复时速度信号保持中性；
- 慢于本人历史不扣分；
- 2 小时进程内临时兴趣 preview；
- 来源级持久正负证据计数；
- 少于 3 条持久证据时 `affinity_preview=0`；
- 决策前 `feedback_state_preview` 写入后续 Shadow observation；
- `turn_id`、来源与候选的显式反馈归因。

这些状态目前没有进入评分公式。生产评分中尚未加入：

- `temporary_interest`；
- `persistent_affinity`；
- `relative_reply_speed_bonus`。

所以一次积极回复不会自动提高下一次同来源候选的生产得分。G0 只建立未来离线候选所需的可审计证据基础。

## 5. 已有证据与效果边界

P44-E2 轻量裁决后的有效指标样本为 128，已有报告中的主要指标为：

| 指标 | 裁决后结果 |
|---|---:|
| 决策准确率 | 57.81% |
| 误打扰率 | 50.98% |
| 错失机会率 | 36.36% |
| Hit@1 | 57.41% |
| MRR | 86.11% |
| nDCG@3 | 85.92% |

阶段解释：

- 正例中的候选排序已经具备一定能力；
- “本次是否应该搭话”的判断仍然偏弱；
- P44-F1 没有找到可接受的统一分数阈值；
- 第一轮来源校准、投递历史和组合候选均因来源集中度护栏失败，正式保留 baseline；
- P44-F2 timing/fatigue 因同 cohort 缺少人工 `should_recommend` 标签而得到 `no_candidate`；
- 当前数据主要来自单一开发者和有限 activity，不能外推为多用户产品效果。

## 6. 内部测试适用范围

本阶段的工程状态支持受控开发者 Dogfood，但不等于默认上线批准。内部测试应继续满足：

- 测试者明确知情并主动开启；
- `active_source` 只通过启动环境显式启用；
- `PROACTIVE_RECOMMENDATION_TUNING_MODE=off`；
- 保留进程内单向 `active_source → shadow` 回退；
- 继续监测隐私、过滤、归因、重复与实际打扰；
- G0 preview 不进入排序、PASS、投递或 tuning。

隐私违规、契约错误、无法关闭/回退或明显连续打扰，均应触发回退到 Shadow。

## 7. 本阶段未完成项

- 个性化兴趣对生产排序的实际消费；
- 持久 affinity 衰减与删除治理；
- 个性化 PASS/no-op；
- timing/fatigue 生产候选；
- semantic repeat、MMR、DPP 与单候选恢复；
- 自动调权、contextual bandit、OPE、Canary；
- 多用户统计证据与普通用户默认放量。

上述项目均需单独立项、在 Testbench 做固定输入的配对模拟，并通过当前规范规定的安全和质量门禁。本文不指定它们为默认下一步。

## 8. 阶段标记

> **Recommendation MVP engineering loop: COMPLETE**
> **Controlled developer Dogfood readiness: AVAILABLE UNDER EXISTING OPT-IN**
> **Personalized ranking consumption: NOT IMPLEMENTED / HOLD**
> **Automatic tuning and broad rollout: HOLD**

本标记只描述 2026-07-22 截点状态，不构成新的开发、调参或上线授权。
