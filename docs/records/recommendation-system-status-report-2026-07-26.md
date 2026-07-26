# 主动推荐系统状态报告与成熟化路线

> 报告日期:2026-07-26
> 审计范围:`feat/recommend-MVP`(实现分支,截点 `3c0626bf`)与 `feat/recommend-testbench`(测试台架分支,截点 `e0a3e474`)
> 审计方法:全量设计/证据文档核对 + 实现代码审计 + 两分支实测(单测与烟测实际运行)
> 本报告为状态快照,不构成任何实施授权;当前授权边界以两分支各自的
> `proactive-recommendation-current-scope.md` / `RECOMMENDATION_CURRENT_SCOPE.md` 为准。

---

## 1. 系统目的

N.E.K.O 的"主动搭话"功能会定时主动与用户交流,内容来自音乐、新闻、视频、meme、
屏幕观察、小游戏邀请等来源。推荐系统的目标:**在调度器决定"现在该搭话"之后,
从已通过硬约束的安全候选中挑出此刻最合适的一个**,并随使用逐渐学会用户偏好
(何时不打扰、更喜欢什么素材)。

设计上遵循 "Guarded Recommender" 路线的三条刻意边界:

1. **职责切分**:调度器管"什么时候",推荐器管"哪个候选",投递层与 LLM 管
   "最终说什么"——推荐器只前置 Phase1 话题顺序,Phase2 LLM 保留否决权;
2. **隐私优先**:观测数据全部白名单脱敏,不存对话原文、屏幕内容、URL、token;
   兴趣画像只存聚合值,不存逐条行为;
3. **影子优先**:任何新能力先以 preview/shadow 形式运行,拿到不可变冻结证据、
   通过门禁后才允许申请进入真实行为,且必须可单向回退。

## 2. 当前功能全景

### 2.1 实现组件(`feat/recommend-MVP`,约 5,900 行实现 + 4,500 行测试)

| 组件 | 文件 | 状态 |
|---|---|---|
| 排序核心 | `main_logic/proactive_recommendation.py` | 完整;确定性打分、来源权重、多样性护栏,读取真实 activity 状态 |
| 观察落盘 | `main_logic/proactive_recommendation_observer.py` | 完整;脱敏白名单、observation v3 契约、校准报表、review context |
| 反馈汇聚 | `main_logic/proactive_recommendation_feedback.py` | 完整;19 类事件统一 chokepoint、reward_score_v2 preview、turn_id 归因 |
| 兴趣画像 | `main_logic/proactive_recommendation_feedback_state.py` | 仅 preview;v2 双状态(搭话接受度 / 来源亲和度),`ranking_consumed=False` |
| 时机特征 | `main_logic/proactive_recommendation_timing.py` | 仅观测;timing v3 五字段快照,不参与任何决策 |
| 调参层 | `main_logic/proactive_recommendation_tuning.py` | 完整但默认 `off`;含步长/冷却/回滚/健康暂停 |
| 运行时开关 | `main_logic/proactive_recommendation_runtime.py` | 完整;仅支持单向 active→shadow 降级,无运行时激活路径 |
| API | `main_routers/proactive_router.py` | 完整;summary/runtime 只读端点 + 降级端点 |
| 前端反馈 | `static/jukebox/music_ui.js` 等 | 完整;音乐播放行为按实际播放时长分类采集 |

**生产默认行为**:纯影子——每轮真实运行排序并记录,但零落盘(日志开关默认 off)、
零画像更新、零调参、零投递影响。唯一改变行为的路径是开发者启动时 env 显式开启
`active_source`,效果被压到最小(仅前置 top1 素材通道,LLM 仍可不采纳)。

### 2.2 测试台架(`feat/recommend-testbench`,约 20,900 行)

五环节离线影子研发流水线:27 场景金标回归门禁 → 影子数据脱敏导入与 readiness
门禁 → 盲标注/裁决/金标建设 → 离线策略分析(阈值/timing/四臂/接受度/个性化模拟)
→ 群组冻结与安全导出。全链路只读、确定性、带 SHA-256 溯源,不写生产状态。
自 2026-07-26 起,导入 chokepoint 直接采用生产 sanitizer 输出,配漂移拒绝门
(`production_timing_sanitizer_drift`),生产语义漂移会被显式拒绝而非静默修补。

### 2.3 阶段完成度台账

| 阶段 | 内容 | 状态 | 关键结论 |
|---|---|---|---|
| P0 / P1 | 环境恢复、隐私修复、Shadow 采集、schema v2 | done | News -0.02 候选三轮评估 NO-GO |
| P44-A~D | 结构审计、安全 review_context、导出校验、重采 | done | 形成 annotation-ready cohort |
| P44-E / E2 | 正式 freeze + 人工主审/盲二审/裁决 | done | Golden 有效样本 128 |
| P44-F1 | 统一 PASS 阈值搜索 | done | `no_universal_threshold_candidate` |
| P44-F2 / F2-B | timing v3 只读观测 + 疲劳关联分析 | done(结项) | 105/30 baseline;缺人工标签,`no_candidate` |
| P44-F2-R0 | timing 盲标重启预检 | closed: HOLD | 可行性通过(上限 101 条),不开新标注轮 |
| 四臂评估(R1) | baseline vs 3 候选 | done | 三候选触发护栏,`baseline_retained` |
| MVP 收口(R2) | active_source 开发者 opt-in + 单向回退 | done | "工程闭环 COMPLETE、受控 Dogfood 可用" |
| P44-G0-A~D | reward / 回复速度 / 临时·持久画像 preview | done | 全部 shadow-only,无消费方 |
| P44-G1 第一部分 | feedback preview v2 语义拆分 + 接受度报告 | done | 260/161,`descriptive_only` |
| P44-G1-R1 | 有界个性化模拟(±0.03) | done | `impact_only`,Top-1 翻转 0 |
| P44-G1-R2 | 个性化响应曲线 | done | `gradual_12` 仅过机械门禁,`hold_for_negative_evidence` |
| P44-G2 / G3 | 重复惩罚 / 来源多样性 | HOLD | 无实验,需独立立项 |

**贯穿事实**:所有候选评估结论均为 `no_candidate` / `baseline_retained` / `hold`,
至今零候选晋升 Shadow;生产权重、调度、投递、tuning 从未被修改。这是流程纪律的
结果("证据不够就不动"),不是烂尾。

## 3. 关键指标现状

| 指标 | 数值 | 解读 |
|---|---|---|
| 决策准确率(该不该搭话) | 57.81% | **最大短板**,接近随机 |
| 误打扰率 | 50.98% | 约一半主动搭话是打扰 |
| Hit@1 / MRR / nDCG@3 | 57.41% / 86.11% / 85.92% | 正例排序已具备一定能力 |
| Golden 有效样本 | 128 条 | 支持离线评估,不足以证明生产改善 |
| timing baseline | 105 观测 / 30 显式反馈 / 0 契约错误 | 无同批人工标签 |
| 来源亲和度证据 | Music 正向 11 / 负向 0 | 负例为零,阻塞一切个性化验收 |
| 数据来源 | 单一开发者 | 不能外推多用户效果 |
| 测试(2026-07-26 实测) | 单测 118/118、烟测 14/14 全绿 | 两分支均已验证 |

## 4. 成熟度评估

| 维度 | 程度 | 说明 |
|---|---|---|
| 工程基建 | ★★★★★ | 全链路落地,防御性编程与脱敏质量高,TODO/FIXME 为零 |
| 测试生态 | ★★★★★ | 确定性、可复现、SHA 溯源、含漂移防护 |
| 排序能力 | ★★★☆☆ | 可用,但仅在单人小样本上验证 |
| 决策能力 | ★☆☆☆☆ | 接近抛硬币,且统一阈值路线已被否定 |
| 个性化 | ★☆☆☆☆ | 只有 preview,零消费方;模拟显示 ±0.03 上限下影响极小 |
| 数据 | ★☆☆☆☆ | 规模小、全正向、单人、activity 覆盖不全 |
| 产品化 | 刻意 10% | 影子默认 + 开发者 opt-in;`production_release_approved` 硬编码 False |

**一句话**:骨骼(工程/测试)已是成熟水准,肌肉(算法)刚开始长,神经(个性化闭环)
尚未接通。缺的不是代码,是带标签的真实数据和按既定门禁走完的晋升。

## 5. 2026-07-26 审计后的修复记录

| 提交 | 分支 | 内容 |
|---|---|---|
| `7f25c86a` | feat/recommend-testbench | 生产代码副本追平 MVP 截点(消除约 753 行分叉;修复 `feedback_joined_count` 红测;补齐 runtime/timing 模块与 activity 接线) |
| `92a0e8a1` | feat/recommend-testbench | 台账收口(F2-R0 口径统一为 HOLD;补 R1/R2 台账条目) |
| `46f29dbf` | feat/recommend-testbench | observation v3 adapter 正式同步,v2 兼容桥接层退役为漂移拒绝门 |
| `2ebd81dc` | feat/recommend-MVP | 权威边界文档推进到 2026-07-26 现状 |
| `08ab18d9` | feat/recommend-MVP | 记录 adapter 同步完成 |

修复后两分支代码、测试、文档完全自洽;两份权威文档中声明的全部可执行工作项已关闭。

## 6. 成熟化路线(按依赖顺序)

### 阶段 A:补证据地基(当前最优先)

- **定向补充真实负向来源证据**——R1/R2 明确写出的共同阻塞项;可考虑立项设计
  低摩擦负反馈入口("不感兴趣"),而非只依赖用户主动关闭;
- 决策 timing 标签问题:为 105 条 cohort 补人工 `should_recommend` 标签,或重新
  采集带标签数据(重启门禁:delivered/pass × should 四格各 ≥8、两侧各 ≥20);
- 扩大反馈规模(放量前置条件约 ≥500 条可归因反馈,当前差一个量级)与 activity
  覆盖(focused_work 目前零样本)。

### 阶段 B:第一次真正闭环(preview → 行为)

- 负向证据到位后重跑 `gradual_12` 类有界个性化评估——当前离晋升最近的候选;
- 通过后按权威文档 §7 既定流程:冻结公式 → 同一 Golden 配对验证 → feature flag
  + Shadow 先行 → 开发者 opt-in 复核。**第一个走完晋升管道的候选,验证的是整条
  管道本身**;
- 配套完成持久画像的衰减与删除治理(隐私承诺的一部分)。

### 阶段 C:攻决策门(最难、最值钱)

- 排序已可用,"该不该说话"才是桌宠体验生死线;统一阈值已被否定,方向只能是
  **分场景时机模型**(activity × timing 特征 × 个人基线),依赖阶段 A 的标签;
- 建议单独立项并定义量化验收线(例:误打扰率 <25% 且错失机会不恶化)。

### 阶段 D:多用户化与放量

- propensity 日志 → 离线策略评估(OPE)→ 多用户 Shadow(文档模板:200 决策 /
  50 反馈 / 7 天)→ Canary;
- 设计目前刻意不存在的运行时激活路径(现仅 env 启动配置、API 只降不升),连同
  `production_release_approved` 的翻转——这是一次正式的产品 + 隐私治理决策,
  不是普通代码改动。

### 贯穿性工程卫生

- 保持两分支同步纪律:cherry-pick 模式曾产生 753 行分叉与一次红测,漂移门现可
  自动报警,但建议改为定期合流;
- 将 14 个推荐烟测与推荐单测挂入 CI,而非仅本机执行;
- testbench 前端 `static/` 择机同步 main(当前 3 个静态契约用例因此暂未引入)。

## 7. 参考索引

- 权威边界(MVP):`docs/design/proactive-recommendation-current-scope.md`
- 权威边界(台架):`tests/testbench/docs/RECOMMENDATION_CURRENT_SCOPE.md`
- 证据文档:`tests/testbench/docs/P44_*.md`(F2-R0 / G0 / G1 / G1-R1 / G1-R2)
- 历史计划:`docs/design/proactive-recommendation-mvp-p0-p1-plan.md`
- 远期研究路线(未授权实施):`docs/design/proactive-recommendation-academic-technical-route.md`
- 台账:`tests/testbench/docs/PROGRESS.md`、`tests/testbench/docs/CHANGELOG.md`
