# 主动搭话推荐系统 MVP：P0/P1/P44 历史执行记录

状态：**历史记录（已归档，不作为当前实施计划）**
适用分支：`feat/recommend-MVP`
当前实施范围：[`proactive-recommendation-current-scope.md`](./proactive-recommendation-current-scope.md)

> 本文保留各阶段当时的目标、门禁和实验结果，便于审计；后文出现的“待执行”“下一步”和旧 GO/HOLD 条件均按其原始时间点理解。若与当前范围文档冲突，以当前范围文档为准。

当前收口状态（2026-07-21）：

- P44-E2 人工裁决已完成，有效指标样本 128；
- P44-F1 已完成，结论为 `no_universal_threshold_candidate`；
- timing v3 首个 baseline 已冻结为 105 observation / 30 个显式关联 feedback turn，契约错误为 0；
- P44-F2-B 已完成，因同 cohort 缺少人工 `should_recommend` 标签而结论为 `no_candidate`；没有生成疲劳公式或模拟；
- 当前没有自动获准的下一阶段；生产排序、调度、权重和 tuning 保持不变。

## 目标

P0/P1 的目标不是建设测试平台，而是让当前推荐实现具备两个能力：

1. **P0：可以安全地打开 Shadow 数据采集。**
2. **P1：用主程序真实运行数据判断是否值得进入 `active_source` 灰度。**

## 明确边界

本计划不建设新的 Testbench，不增加独立测试 UI，不复制主动搭话管线，不模拟完整用户系统，也不开发通用实验平台。

只复用当前已经存在的能力：

- 推荐单元测试。
- 主程序主动搭话运行时。
- `shadow` 模式。
- observation/feedback JSONL。
- `/api/proactive/recommendation/summary` 汇总接口。

## P0：让现有 Shadow 可安全运行

### P0.1 恢复测试环境并确认当前基线

执行：

1. 使用项目规定的 Python 3.11 和 `uv` 重建失效环境。
2. 运行本分支新增的 6 组推荐测试。
3. 运行已有主动搭话相关单元测试，确认没有回归。
4. 记录失败项；只修复会阻塞 Shadow 采集或破坏原行为的问题。

验收：

- 推荐测试全部通过。
- 主动搭话相关测试无本分支新增失败。
- `PROACTIVE_RECOMMENDATION_MODE=off` 时保持原行为。
- `shadow` 模式不改变 Phase 1 顺序和最终投递。

### P0.2 修复日志隐私阻断项

当前 observation 白名单保留 `top_candidates[].topic`，而 topic 可能来自个人动态、窗口或视觉上下文。

执行：

1. observation 落盘默认不保存候选 topic 原文；只保留候选 ID、来源、family、rank 和 score。
2. 保持 feedback 禁止保存聊天正文、prompt、截图、source links 和原始 payload。
3. 增加针对个人动态、窗口标题、URL 参数和聊天原文的脱敏测试。
4. 确认日志异常只降级为“不记录”，不得阻断主动搭话。

验收：

- JSONL 中不存在用户聊天原文、个人动态正文、窗口标题、截图、完整 URL 或 prompt。
- 非法字段注入测试通过。
- 日志路径不可用或写入失败时，主动搭话仍可完成。

### P0.3 完成现有契约冒烟

不另建验证平台，使用现有 Recommendation Testbench 契约 smoke 完成最小检查：

1. P41 后端契约通过。
2. P42 UI 契约通过。
3. 覆盖 off/shadow、隐私过滤、来源过滤和硬约束。
4. 场景执行无错误，且不改变生产配置。
5. observation/feedback/summary 的文件与 API 契约由现有单元测试覆盖。

验收：

- 后端与 UI 契约 smoke 通过。
- `shadow` 不改变实际来源选择。
- observation 和 feedback 能通过 `turn_id` 关联。
- summary 不返回敏感正文。
- 执行错误和硬约束违规均为 0。

### P0 完成定义

只有以下条件全部满足才进入 P1：

- 测试环境恢复，相关测试通过。
- observation topic 原文隐私问题解决。
- 真实主程序完成一轮 Shadow 闭环。
- Shadow 对现有主动搭话行为零干预。

## P1：直接采集真实 Shadow 数据（历史阶段）

### P1.1 运行配置

在开发者自用角色或明确知情的测试角色上启用：

```text
PROACTIVE_RECOMMENDATION_MODE=shadow
PROACTIVE_RECOMMENDATION_OBSERVATION_LOG=jsonl
PROACTIVE_RECOMMENDATION_FEEDBACK_LOG=jsonl
PROACTIVE_RECOMMENDATION_TUNING_MODE=off
```

要求：

- 不开启 `active_source`。
- 不开启自动调参。
- 不为了凑样本手工构造虚假反馈。
- 推荐算法或反馈分值发生修改时重新开始一个数据批次。

### P1.2 最低样本目标

- observation 不少于 100 条。
- 成功投递不少于 60 条。
- 可关联反馈不少于 30 轮。
- 每个主要来源至少 10 条；不足的来源只报告，不调整权重。

以下不算有效偏好反馈：

- 投递失败或被抢占。
- `turn_id` 缺失、重复或无法关联。
- 音乐加载错误、自动播放被浏览器阻止等技术事件。
- 超出反馈窗口的行为。

### P1.3 只看必要指标

直接使用现有 summary API，关注：

1. Shadow Top-1 与实际来源一致率。
2. 实际材料在推荐列表中的平均排名。
3. 高分推荐最终 pass 的比例。
4. 有效反馈覆盖率与关联率。
5. 高、中、低分桶的平均反馈是否基本单调。
6. 各来源曝光率、平均反馈和负反馈率。
7. 单一来源过量、连续重复来源和重复候选情况。

所有比例同时保留分子和分母，避免小样本百分比误导。

### P1.4 采集期间停止条件

出现以下情况立即关闭 JSONL，退回 P0：

- 日志出现敏感原文。
- Shadow 改变了实际投递行为。
- observation 与 feedback 大量错配。
- 日志写入影响主动搭话稳定性或延迟。
- 单一来源因明显算法异常长期垄断推荐。

### P1 完成与决策

> 以下是 P1 当时的采集决策标准，已被 P44 annotation-ready Golden、裁决和候选准入流程取代。达到这些数量只表示可进入离线候选分析，不再直接表示可制定 `active_source` 灰度。

达到样本目标后只做一次阶段决策：

- **GO**：数据质量可靠，分数与反馈基本正相关，可以制定 `active_source` 小流量灰度。
- **HOLD**：数据可靠但样本或效果不足，继续 Shadow。
- **NO-GO**：评分失真、隐私或稳定性存在问题，先修复再采集。

GO 的最低条件：

- observation ≥ 100、成功投递 ≥ 60、有效反馈 ≥ 30。
- 日志解析和敏感字段合规率 100%。
- 反馈关联率 ≥ 95%。
- 高分桶反馈不低于中分桶，中分桶不低于低分桶；样本不足则 HOLD。
- 没有隐私、投递稳定性或明显延迟回归。

## 执行顺序

| 顺序 | 工作项 | 状态 | 完成证据 |
|---|---|---|---|
| 1 | 重建 Python 3.11/uv 环境 | 已完成 | 项目 `.venv` 已恢复为 Python 3.11.9 |
| 2 | 跑推荐与主动搭话测试 | 已完成 | 推荐修改前 72/72、修改后 73/73；完整相关回归 424/424 |
| 3 | 修复 observation topic 隐私项 | 已完成 | topic 原文移除；隐私与完整回归通过 |
| 4 | Recommendation Testbench 契约冒烟 | 已完成 | P41/P42 通过；45 场景；0 错误；0 硬约束违规 |
| 5 | P0 sign-off | 已完成 | 运行、隐私、过滤和 UI/后端契约通过 |
| 6 | 开启隔离 Shadow 采集 | 已完成 | P44-E 正式 freeze |
| 7 | 达到样本目标并复核质量 | 已完成 | P44-E2 有效样本 128；timing v3 baseline 105/30 |
| 8 | GO/HOLD/NO-GO | 已完成 | 仅可进入离线候选分析；生产 `active_source`/tuning 维持 HOLD |

## 执行记录

### P0 基线（2026-07-15）

- 环境：项目 `.venv` 已恢复为 Python 3.11.9；临时安装环境验证后已清理。
- pytest 自动插件加载会导致本机收集阶段卡住；基线命令禁用自动加载，仅显式启用 `pytest_asyncio.plugin`。
- 推荐系统 6 组测试：修改前 72 passed；新增隐私测试后 73 passed。
- 全部 `test_proactive_*.py` 加音乐反馈回归：修改前 423 passed，隐私修改后 424 passed。
- 仅有 FastAPI、websockets 和 plugin config 既有弃用警告，无测试失败。

### P0 Recommendation Testbench 签核（2026-07-15）

- Run ID：`bf42aa31dd3d4c8d9acac1382f5f6527`。
- P41 后端契约 smoke：通过。
- P42 UI 契约 smoke：通过。
- 场景数：45；执行错误：0；硬约束违规：0。
- 推荐系统运行、隐私和过滤契约通过，P0 据此签核。
- 第 45 个场景为本地用户副本 `activity_07_copy`，不是内置场景。该轮基线与候选使用同一场景集，因此候选淘汰结论有效；若需要形成严格可复现的标准归档，应另跑一次仅含 44 个 builtin 场景的 canonical run。

### P1 前置候选实验：News -0.02（拒绝）

| 指标 | 生产默认基线 | News -0.02 | 结论 |
|---|---:|---:|---|
| 可接受 Top-1 | 70.45% | 63.64% | 下降 6.81 个百分点 |
| 最大来源曝光 | 79.49% | 71.79% | 集中度改善 |
| 来源 HHI | 0.6568 | 0.5502 | 集中度改善 |
| 候选重复率 | 10.26% | 15.38% | 上升 5.12 个百分点，越过退化门槛 |
| 硬约束违规 | 0 | 0 | 持平 |
| 执行错误 | 0 | 0 | 持平 |

决策：

- 本候选总状态为 `regressed`，不得应用到生产。
- 保持生产默认权重，不开启该候选调参，也不进入 `active_source` 灰度。
- 此结论只针对 `News -0.02`，不代表推荐系统契约失败。
- Hit@1 和 nDCG@3 为 `null`，原因是场景没有候选 relevance 黄金标注；45 个 ties 不能解释为候选质量相同。
- 本轮属于 P1 前置离线准入，不计入“真实 Shadow observation ≥ 100、有效反馈 ≥ 30”的样本门槛。

### P1 最新离线复测（2026-07-15，结论不变）

- Run ID：`8cbb704ad4b04fd1b9c133bdf53c1b0d`。
- 报告状态：`passed_with_warnings`；49 场景；0 执行错误；0 硬约束违规。
- Input hash：`dce4c76122ee2747d798a9c58b3b1ad5a7cb7cb5405209ab54e3fae917094b48`。
- `News -0.02` 在本报告中是相对生产默认分数的额外 `-0.02`：所有 news 分数均从基线下降 0.020，并非把生产总权重改成 `-0.02`。

| 指标 | 生产默认基线 | News -0.02 | 差异 |
|---|---:|---:|---:|
| 最大来源曝光 | 78.05% | 70.73% | -7.32 个百分点 |
| 候选重复率 | 14.63% | 19.51% | +4.88 个百分点 |
| 硬约束违规 | 0 | 0 | 0 |
| 执行错误 | 0 | 0 | 0 |

场景集审计：

- 内置场景为 44 个，本轮另外包含 5 个本地用户副本：`activity_07_copy`、`activity_07_copy_copy`、`activity_10_copy`、`competition_17_copy`、`quality_34_copy`。
- 用户副本改变了来源分布和重复率分母，使重复率退化从上一轮 5.12 个百分点变为 4.88 个百分点，刚好低于 5% 硬退化门槛。这解释了状态从 `regressed` 变为 `passed_with_warnings`，不能解释为候选质量改善。
- 生产默认与候选均为 0 wins / 0 losses / 49 ties。Hit@1 和 nDCG@3 仍为 `null`，因此本轮没有可计算的排序质量证据。
- 候选主要可观察变化是 `privacy_04`–`privacy_06` 的 Top-1 从 news 切换为 vision，以及整体 news 曝光下降；这只能证明权重生效和集中度变化，不能证明结果更相关。

复测决策：

- `passed_with_warnings` 不升级为生产准入通过。
- `News -0.02` 继续维持 **NO-GO**；生产默认权重保持不变。
- 历史规则：后续离线准入当时要求固定 44 个 builtin 场景、排除用户副本；该规则已被 `recommendation_builtin_v2` 的 27 场景 manifest（v2.0.0）取代。
- 在 relevance 黄金标注补齐前，离线报告只能验证契约、稳定性、硬约束和分布变化，不能用于宣称排序质量提升。

### P1 黄金标注子集复测（2026-07-15，候选确认淘汰）

- Run ID：`49d83326384249cda2428314bf7bbe9a`。
- 报告状态：`regressed`；25 个场景，0 执行错误，0 硬约束违规。
- Input hash：`db3e07349b40dba64b086919928bd792e3b5e9f650b3bf42775587d11c6aef94`。
- 本轮已产生可计算的排序质量指标，因而可作为比前两轮更直接的候选准入证据。

| 指标 | 生产默认基线 | News -0.02 | 差异 |
|---|---:|---:|---:|
| Hit@1 | 0.8750 | 0.8125 | -0.0625 |
| nDCG@3 | 0.9661 | 0.9480 | -0.0181 |
| 最大来源曝光 | 68.42% | 63.16% | -5.26 个百分点 |
| 候选重复率 | 10.53% | 10.53% | 持平 |
| 硬约束违规 | 0 | 0 | 0 |
| 执行错误 | 0 | 0 | 0 |

配对结果为 0 wins / 1 loss / 24 ties。`News -0.02` 虽然继续降低最大来源曝光，并且本轮没有恶化重复率，但同时令 Hit@1 下降 6.25 个百分点、nDCG@3 下降 0.0181；分布收益不能抵消有黄金标注的排序质量损失。

本轮仍不是 44 个 builtin 场景的 canonical run：25 个场景中包含 `activity_07_copy`、`activity_07_copy_copy`、`activity_10_copy`、`competition_17_copy`、`quality_29_copy`、`quality_34_copy` 等用户副本。因此它不替代标准归档，但候选与基线在同一输入集上配对，且已经出现明确 loss，足以维持淘汰决定。

复测决策：

- `News -0.02` 维持 **NO-GO**，不应用到生产，不进入 `active_source` 灰度。
- 保持生产默认权重；后续不再重复验证该候选，除非候选逻辑或黄金标注发生实质变化。
- 下一轮离线实验应提出新的候选，固定场景版本，并同时设置排序质量与来源集中度护栏；不能仅以曝光分布改善作为通过依据。

### P1 Shadow observation schema v2（2026-07-15，代码完成）

- 所有 observation 出口现在保证非空 turn ID；缺失时生成 UUID，并同步写回响应体供后续反馈关联。
- JSONL writer 拒绝缺少 turn ID 或 algorithm version 的新记录。
- observation 新增 `activity_state`、`activity_propensity`、`algorithm_version` 与可选 `git_revision`；当前契约版本为 `0.8.3:proactive-recommendation-observation-v2`。
- `feedback_joined_count` 只统计通过有效 turn ID 关联的显式反馈；新增 `feedback_inferred_count` 和 `feedback_scored_count`，每条 joined row 标记 `feedback_inferred`。
- inferred ignored 不再为缺少 turn ID 的 observation 生成；active-ready 的 30 条门槛只使用显式关联数。
- 受影响测试 60 passed；全部主动推荐与音乐反馈回归 433 passed。仅有既有弃用警告。

历史部署指令（已完成且不再适用）：当时要求将首轮 22 条标为 `diagnostic-round-1 / observation-schema-v1`，再以 schema v2 采集核对。旧的轮转指令已经作废；现行日志分批规则见 [`proactive-recommendation-current-scope.md`](./proactive-recommendation-current-scope.md)。

### P44 Shadow review 收口与正式采集门禁（2026-07-16）

P44-A/B/C 已完成：结构审计、默认关闭的安全 `review_context`、安全导出与 Testbench 校验已经形成可复核标注数据。旧的“直接补 relevance 标注”路线作废；只有带合规 review context 的 observation 才允许进入人工标注。

更新后的 `shadow-review-round-1-analysis.md` 已生成：

- 可复核场景 21，候选 51。
- Hit@1 0.7143、MRR 0.7778、nDCG@3 0.9117、应推荐率 0.619。
- 权重压力只用于诊断；`Ready=false`，没有修改生产配置，也没有生成可应用权重。
- freeze 快照为 85 observations、15 条显式关联 feedback；`inferred ignored` 不计入反馈门槛。

该阶段当时仅保留两个正式门禁（后被 annotation-ready Golden 门禁取代）：

1. observation ≥ 100。
2. 通过有效 turn ID 关联的显式 feedback ≥ 30。

执行状态转入 P44-D 正常采集。达到两个门槛前保持 `PROACTIVE_RECOMMENDATION_TUNING_MODE=off`，不应用权重候选；继续监控 schema、隐私、orphan feedback 和来源/activity 覆盖，但这些诊断项不替代上述两个样本门槛。

### P44-E 正式 freeze 与 Golden cohort 门禁（2026-07-16，历史阶段已完成）

- 正式 freeze：`shadow-p44e-freeze-20260716-123009.json`。
- SHA-256：`68FC12864472E9540C6EE32A0A3909CE261EF7F3A884483819ECDDD1CD4B9E5D`，已复核一致。
- 全日志层为 169 observations、36 个显式关联 feedback turn，旧门槛已通过。
- 4 个 orphan feedback 已审计为独立小游戏邀请链路：`mini_game_decline` 3、`mini_game_accept` 1；不是数据损坏，也不计入 Recommendation feedback 门槛。
- Testbench 已修复混合 `git_revision` 与 `algorithm_version` 导致的 mixed-version 误报；正式 freeze 为单一 observation schema v2，169/169。

Golden cohort 只接受通过安全校验并带 `review_context` 的 observation。正式 freeze 中 annotation-ready observation 为 84，相关显式 joined feedback turn 为 21；85 条旧 observation 不做追溯式语义标注。

该阶段当时的新正式门禁（后续均已达成）：

1. annotation-ready observation ≥ 100（当时尚差 16）。
2. 与 annotation-ready observation 通过有效 turn ID 显式关联的 feedback turn ≥ 30（当时尚差 9）。
3. 达标后冻结只含 annotation-ready cohort，完成标注并至少复核 20%，才允许设计第一组离线权重候选；这只表示可进入离线分析，不授权写生产权重。

当时的采集覆盖重点：ready cohort activity 为 idle 83、chatting 1，后续优先覆盖 `focused_work`。当时已将 `gaming` 明确排除在本轮覆盖要求外；`away`、`busy` 只做机会性记录，没有覆盖也不阻塞 Golden cohort 的 100/30 门禁。生产 tuning 始终保持关闭。

### P44 可复核上下文优先（修订计划）

人工 relevance 标注后移。现有 Shadow round 2 虽然 schema v2 健康，但 observation 不含候选标题、摘要或投递文本，审核者没有足够事实依据。执行顺序改为：

1. **P44-A 结构审计（已完成）**：冻结 85 条 observation、21 条 feedback；显式 join 15，orphan feedback turn ID 2 个；报告见 `docs/design/shadow-round-2-structure-audit.md`。当前数据明确禁止进入语义 relevance 标注。
2. **P44-B 安全 review_context（已完成）**：增加 schema v1 的候选安全标签、activity、投递短摘录和 redaction notes；默认关闭，仅允许 `shadow_review` 或显式 `testbench` 模式。
3. **P44-C 安全导出与校验（已完成）**：observer 使用白名单、URL/secret 清除和长度上限；vision/personal 原文不导出；候选 ID/来源必须与 top candidates 对齐；没有 review_context 时 `annotation_ready=false`。
4. **P44-D 小样本重采（已完成）**：使用 `PROACTIVE_RECOMMENDATION_REVIEW_CONTEXT_MODE=shadow_review` 形成合规的 annotation-ready cohort；`gaming` 不属于本轮覆盖目标，`away`/`busy` 只自然记录。
5. **P44-E/P44-E2 人工标注、盲二审与裁决（已完成）**：137/137 主审、28/28 盲二审和轻量裁决完成；排除 9 个弃权后有效指标样本为 128，Validator/readiness 通过。

P44-B/C 不改变排序、来源选择、投递或 tuning；`PROACTIVE_RECOMMENDATION_TUNING_MODE` 继续保持 `off`。

### P44-F2 MVP timing observation schema v3（2026-07-20，代码完成）

P44-F1 已证明不存在可直接采用的统一分数阈值，因此 MVP 先补时间/疲劳观测，不实现新的 PASS/no-op 规则。observation 合约升级为 `proactive-recommendation-observation-v3`，新增白名单字段：

```json
{
  "decision_context": {
    "timing": {
      "configured_interval_seconds": 60,
      "elapsed_since_last_delivery_seconds": 540,
      "recent_delivery_count_30m": 2,
      "recent_delivery_count_2h": 5,
      "consecutive_unanswered_deliveries": 1
    }
  }
}
```

- `configured_interval_seconds` 来自本轮 `/proactive_chat` 请求中的用户基础间隔。
- 投递间隔与窗口计数来自进程内真实主动搭话投递历史，覆盖普通搭话、休息提醒和小游戏邀请等实际投递；因此它们表示**全局主动搭话打扰负载**，不是 Recommendation 来源自身的曝光计数。
- 连续未回复数只统计 Recommendation feedback pending 窗口内、尚未收到显式用户回复的推荐投递。
- 快照在本轮可能发生投递前冻结；当前投递不会反向污染自己的 timing context。
- sanitizer 仅允许上述低基数数值字段，丢弃其他嵌套数据。
- 新字段只写 observation；排序、投递、生产权重和 tuning 均不读取它们，现有行为不变。

首个 timing v3 baseline 已冻结：

- 文件：`shadow-p44f2-timing-v3-baseline-20260721-103709.json`；
- 截点：2026-07-21 10:33:52（Asia/Shanghai）；
- 样本：105 observation / 30 个显式关联 feedback turn；
- SHA-256：`E79E2B3258E55A29109525CDBB00E511EE7B4142E0A204EC40DF8E2961A88BD7`；
- 绝对时间桶只作描述，不作为 readiness 门禁；不再新增 scheduler 回退阶段或 timing v4 字段。

后续 Testbench P44-F2-B 使用该 freeze 做连续变量关联分析：

- 同 cohort 人工 `should_recommend` 标签为 0，误打扰与错失机会不可计算；
- `recent_delivery_count_30m` 与显式反馈分数存在稳定相关，但不足以形成生产或 Shadow 候选；
- 正式结论为 `no_candidate`，没有运行真实 cohort fatigue simulation；
- P44-F2 至此结项，不自动转入重复、来源多样性或其他研究阶段。
