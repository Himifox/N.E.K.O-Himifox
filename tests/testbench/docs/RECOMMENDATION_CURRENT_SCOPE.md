# Recommendation Testbench：当前边界与停止点

> 状态：**Testbench 当前操作规范**
> 所在分支：`feat/recommend-testbench`
> 产品/MVP 边界归属：`feat/recommend-MVP`
> 最近更新：2026-07-21
> 当前阶段：P44-F2 已以 `no_candidate` 结项；没有自动获准的下一阶段

本文是 Recommendation Testbench 的当前操作说明。产品组件边界仍由 MVP 的 [`proactive-recommendation-current-scope.md`](../../../docs/design/proactive-recommendation-current-scope.md) 决定；`PROGRESS.md`、`CHANGELOG.md` 和 `tests/testbench_data/recommendation/exports/` 中的文件分别是历史记录、版本记录和不可变运行产物，不能覆盖本文的当前结论。

## 1. 当前事实

- P44-E2 人工裁决 Golden Candidate：有效样本 128，排除 9 个弃权。
- P44-F1：`no_universal_threshold_candidate`。
- P44-F2 timing-v3 freeze：105 observation / 30 个显式 joined feedback turn / 0 个 timing 契约错误。
- P44-F2-B：`no_candidate`。
- 生产权重、阈值、interval、scheduler、routing、投递与 tuning 均未修改。
- `active_source` 与所有自动 tuning 继续保持 `HOLD/off`。

正式 freeze：

- 文件：`shadow-p44f2-timing-v3-baseline-20260721-103709.json`
- SHA-256：`E79E2B3258E55A29109525CDBB00E511EE7B4142E0A204EC40DF8E2961A88BD7`
- Cutoff：2026-07-21 10:33:52（Asia/Shanghai）
- 审计：`shadow-p44f2-timing-v3-baseline-20260721-103709-audit.md`
- 分析：`shadow-p44f2-timing-v3-baseline-20260721-103709-analysis.md`

上述 export 位于本地 `tests/testbench_data/recommendation/exports/`，属于不可变证据，不得为同步文档而重写。

## 2. 跨分支组件边界

| 组件 | 当前职责 | Testbench 不得替它做什么 |
|---|---|---|
| 前端 scheduler / 主动搭话 route | 产生搭话机会，执行曲线回退、抖动、固定模式及 privacy/activity 硬约束 | 不在 Testbench 发明或回写第二套调度状态机 |
| Production recommender | 对已经安全的候选做确定性、可解释排序 | 不把 Testbench 模拟自动变成生产 PASS gate、权重或阈值 |
| Delivery | 最终内容生成、文本相似度/BM25 去重和投递退出路径 | 不把技术失败解释为用户偏好 |
| Observation / Feedback | 输出脱敏 point-in-time 数据，并通过有效 `turn_id` 关联显式反馈 | 不补造未来字段、人工标签或 inferred ignored |
| Recommendation Testbench | 审计、冻结、重放、离线反事实模拟与指标报告 | 不联网取代生产候选源，不写生产配置，不自动晋升候选 |
| Tuning | 当前为 `off` | 不开启 `manual`/`auto_safe`，不自动调权或发布 |

`should_recommend`/`PASS` 目前只是 Testbench 的 Gate 评价标签，不是新增生产动作。timing v3 只是只读观测，不进入生产排序，也不包含 scheduler mode、backoff level/tier 或 scheduled delay。

## 3. P44-F2 的准确结论

P44-F2-B 已完成以下工作：

1. 将 `5/10/30` 分钟绝对 elapsed 桶降为描述统计，不再作为 readiness gate。
2. 对五个 timing v3 字段做连续变量关联、确定性 bootstrap、时间切分和 leave-one-source-out 检查。
3. 保留显式 feedback 作为辅助证据，不把缺失反馈当负例。
4. 在 synthetic positive control 上验证 `candidate_for_shadow` 路径，在真实无标签 cohort 上验证 `no_candidate` 路径。

真实 cohort 的限制：

- 同 cohort 人工 `should_recommend` 标签：0；
- false interruption：不可计算；
- missed opportunity：不可计算；
- `recent_delivery_count_30m` 与显式反馈分数存在稳定相关，但这是观察性相关，不是因果证据或候选准入依据。

因此没有 fatigue 公式、没有真实 cohort 候选模拟，也没有 Shadow/生产候选。

## 4. Activity 与数据适用范围

- timing cohort：idle 76 / chatting 10 / unknown 19。
- `unknown` 不算有意义的个性化 activity 覆盖。
- `focused_work` 缺失只报告限制，不阻塞 P44-F2 的 `no_candidate` 结项。
- `gaming` 不属于本轮真实覆盖目标。
- `away`、`busy` 仅在自然出现时记录，缺失不构成产品门禁。
- P47 的“三种 activity”属于 Testbench 通用 strategy-scan eligibility；它不是 MVP、Golden 或生产 gate，也不得被解释为要求覆盖 `gaming`。

## 5. 当前允许与禁止

允许：

- 复现 P47/P48 smoke 和现有冻结报告；
- 修复确定性的契约、隐私、哈希或报告生成 bug；
- 维护 canonical suite、Validator 和文档链接，但必须保持 run artifact 不变。

禁止：

- 因 P44-F2 得出 `no_candidate` 就自动转入重复惩罚或来源多样性；
- 用 delivered、feedback join、feedback score 或 inferred ignored 替代人工 `should_recommend`；
- 新增 timing v4、scheduler/backoff 字段、持久画像或新的生产 PASS gate；
- 写入 MVP 权重、interval、生产配置或 tuning；
- 为了重新分批而删除、归档或轮转 observation/feedback 日志；只能使用 immutable freeze/cutoff。

## 6. 研究 Backlog（全部 `HOLD`）

- 为 timing cohort 补充合规人工决策标签或重新采集带标签 cohort；
- 个体回复时延基线；
- 临时/持久兴趣与 `reward_score_v2`；
- 新的重复惩罚、来源多样性、semantic repeat、MMR、单候选恢复；
- propensity、contextual bandit、OPE、Canary 和自动 tuning。

每一项都需要独立目标、证据、分支归属与验收门禁。当前没有默认下一项。

## 7. 文档角色

- 本文：Recommendation Testbench 当前边界、证据适用范围和停止点。
- `testbench_ARCHITECTURE_OVERVIEW.md`：长期架构与模块关系。
- `PROGRESS.md`：阶段交付历史；旧“下一步”不能覆盖本文。
- `CHANGELOG.md`：面向测试用户的版本记录。
- `tests/testbench_data/recommendation/exports/*`：不可变运行、冻结、审计与人工评审 lineage。
