# 推荐系统开工技术路线(详细版)

> 日期:2026-07-26 | 性质:**技术路线提案**——各步骤开工前仍需按项目规矩逐项立项授权
> 配套文档:[成熟化计划](./recommendation-maturity-plan-2026-07-26.md)(阶段/门禁/决策点)、[状态报告](./recommendation-system-status-report-2026-07-26.md)(基线)
> 本文体例:每一步 = 任务分解 + 涉及文件/模块(全部实名核对) + **完成后推荐系统的预期效果**
> 文中"新建"模块名为提案命名,以实际立项文档为准。

## 基线(Step 0,已完成 2026-07-26)

两分支代码/文档自洽:testbench 生产副本 = MVP 截点 `3c0626bf`,v3 timing 桥接层已退役为漂移拒绝门。
**当前系统效果**:纯影子——每轮主动搭话运行两次确定性排序并(在开启日志时)落盘观测,零行为影响;开发者可 env opt-in `active_source` 获得最小干预的基线排序。

---

## Step 1(M1-A)timing 标注轮 —— 给决策门造地面真值

**前置**:翻转 F2-R0 HOLD 的授权。**纯 testbench 分支操作,零生产代码改动。**

| # | 任务 | 涉及文件/模块 | 执行者 |
|---|---|---|---|
| 1.1 | 从 105 条冻结生成盲标注 manifest(自动排除 4 条技术退出,可用 101 条) | 工具 `tests/testbench/tools/prepare_timing_annotation_manifest.py` → pipeline `recommendation_timing_annotation.py`;输入 freeze `shadow-p44f2-timing-v3-baseline-20260721-103709.json`(SHA-256 `E79E2B32…`) | Claude |
| 1.2 | 生成助手保守预标注草稿(不计入人审) | `tests/testbench/tools/prefill_timing_annotation_assistant_draft.py` → pipeline `recommendation_timing_annotation_draft.py` | Claude |
| 1.3 | 人工主审 101 条:`should_recommend` + 置信度 + 理由 | testbench UI「校准」子页(`tests/testbench/static/ui/recommendation/page_calibration.js`)或直接编辑标注 JSON | **用户,1–2 小时** |
| 1.4 | 盲二审 21 条预选样本(隔天,保证独立;盲字段由 `FORBIDDEN_BLIND_KEYS` 机制屏蔽) | `merge_recommendation_blind_review.py`、`normalize_recommendation_blind_review.py`;分歧走 `prepare_recommendation_adjudication.py` + `finalize_recommendation_adjudication.py` | 用户 + Claude |
| 1.5 | 四格 readiness 门禁(delivered/pass × should 各 ≥8、两侧各 ≥20)→ `ready_for_f2_rerun` → 重跑 F2 关联分析 | pipeline `recommendation_timing_analysis.py` + 工具 `analyze_recommendation_timing.py`;回归保护 `smoke/p48_recommendation_timing_analysis_smoke.py`、`p49_timing_annotation_restart_smoke.py` | Claude |
| 1.6 | 台账回写 | `tests/testbench/docs/P44_F2_R0_TIMING_EVIDENCE_RESTART.md`、`PROGRESS.md`、`RECOMMENDATION_CURRENT_SCOPE.md` | Claude |

**完成后的预期效果**:
- 推荐系统运行行为**零变化**(仍 shadow);
- 首次拥有人工 `should_recommend` 标签集(≈101 条)——**误打扰率与错失机会率从"不可计算"变为可计算**;
- F2 关联分析首次带标签重跑:timing 五字段(如 `recent_delivery_count_30m`)与"该不该搭话"的关系从观察性相关升级为可验证结论;
- Step 7(决策门建模)的数据前置条件解锁。
- **失败路径**:某格 <8 → 按 R0 规则回 HOLD,标签需求转由 Step 3 新采数据满足。

## Step 2(M1-B)显式负反馈入口 —— 结构性解决负例为零

**前置**:立项授权(新反馈 UI,G1 第一部分明确排除过)。**影子先行:只增证据,不碰排序。**

| # | 任务 | 涉及文件/模块 |
|---|---|---|
| 2.1 | 新事件类型 `user_not_interested`:加入 `_FEEDBACK_EVENT_SCORES`(负值 component,如 `("explicit_negative", -0.35, "high")`)与 `_CONVERSATION_ACCEPTANCE_EVENT_TYPES` 路由;带可验证 candidate ID 时写**来源负向证据**,不带时只更新会话接受度 | `main_logic/proactive_recommendation_feedback.py`(事件表约 :39-:110 区域及 v2 状态路由) |
| 2.2 | v2 状态负向证据落账:临时/持久桶的 negative evidence 计数与 `affinity_preview` 方向(现有结构已支持正负计数,补事件源) | `main_logic/proactive_recommendation_feedback_state.py` |
| 2.3 | 端点白名单:`POST /api/proactive/recommendation/feedback`(`proactive_router.py:542`)接受新事件类型;summary 端点的 reward preview 显示负向 component | `main_routers/proactive_router.py` |
| 2.4 | 前端一键入口:主动搭话卡片悬停显示「不感兴趣」,复用 `music_ui.js:1690` 的上报模式(`lanlan_name` + `turn_id` + 事件类型 + candidate ID) | `static/app/app-proactive.js`(通用卡片)、`static/jukebox/music_ui.js`(音乐卡片) |
| 2.5 | 数据标记:事件带 `ui_generation` 字段,供分析分层(按钮引入的观察偏差可隔离) | 同 2.1;observation/feedback 白名单同步 `proactive_recommendation_observer.py` |
| 2.6 | 测试:事件归因/负向路由/孤儿拒绝单测;前端 .test.cjs;testbench 契约同步 | `tests/unit/test_proactive_recommendation_feedback.py`、`test_proactive_recommendation_summary_router.py`;`tests/frontend/`(新增);`tests/testbench/smoke/p52`、`p54`(契约断言扩展) |
| 2.7 | 文档:MVP 权威文档新增结论条目与 §5.7;testbench 双台账 | `docs/design/proactive-recommendation-current-scope.md` 等 |

**完成后的预期效果**:
- 用户在主动搭话卡片上获得一个新的一键操作(唯一可见变化);点击后该 turn 记为显式负反馈;
- **负例从结构性为零变为自然积累**:每条带 candidate ID 的「不感兴趣」进入来源负向证据,`affinity_preview` 首次可能出现负值方向;
- reward preview 出现负 component,`/api/proactive/recommendation/summary` 可审计;
- 排序、PASS、投递、tuning **仍完全不读取**这些状态(`ranking_consumed=false` 不变)——行为零变化,只有证据变了;
- 积累 2–4 周后,Step 4 的验收条件(正负双向证据)成立。

## Step 3(M1-C)多用户知情内测 —— 消除单人偏差

**手册已就绪**:`tests/testbench/docs/RECOMMENDATION_BETA_COHORT_GUIDE.md`(testbench 分支,提交 `3e21f5e4`)。

| # | 任务 | 涉及文件/模块 |
|---|---|---|
| 3.1 | 招募 5–10 名知情用户(同意书模板在手册 §2) | **用户** |
| 3.2 | 参与者按手册 §3 配置 env(shadow + 两个 jsonl 日志 + shadow_review),7 天正常使用 | `config/proactive_settings.py` 定义的 4 个环境变量 |
| 3.3 | 回收脱敏包并导入:参与者本机跑 `export_recommendation_shadow.py` → 操作者经 testbench `POST /datasets/import`(`routers/recommendation_router.py`,内置生产 sanitizer + 漂移门)导入,按代号分 dataset | `tests/testbench/tools/export_recommendation_shadow.py`、`tests/testbench/routers/recommendation_router.py` |
| 3.4 | 冻结多用户 cohort(SHA-256 固化)+ readiness 审计 + activity 覆盖报告 | `freeze_recommendation_v2_cohort.py`、pipeline `recommendation_shadow.py`(`audit_shadow_dataset`) |

**完成后的预期效果**:
- 推荐系统运行行为零变化;
- 证据基础从"单人"变为"≥5 人、合计 ≥200 决策/≥50 显式反馈"——**所有后续结论首次可以谈外推性**;
- activity 覆盖预期扩展(可能首次出现 `focused_work` 有效样本,解除该场景"只能报告限制"的状态);
- Step 4 与 Step 7 的共同数据基础就绪。

## Step 4(M2-B1)个性化效果验收重跑 —— 第一次有资格说"有效/无效"

**前置**:Step 2 产生的负例 + (建议)Step 3 的多用户 freeze。纯 testbench 分析,零生产改动。

| # | 任务 | 涉及文件/模块 |
|---|---|---|
| 4.1 | 在含正负证据的新 freeze 上重跑有界个性化(`current_v1` 对照 + `gradual_12`) | pipeline `recommendation_bounded_personalization.py`、`recommendation_personalization_response_curve.py`;工具 `analyze_recommendation_bounded_personalization.py`、`analyze_recommendation_personalization_response_curves.py`;回归 `p55`、`p56` smoke |
| 4.2 | 新增方向校准检查:负向证据必须把对应来源 delta 拉向负值(先定义门禁再跑) | `recommendation_bounded_personalization.py` 小幅扩展 + p55 新断言 |
| 4.3 | 判定:方向校准正确 + HHI/最大曝光护栏不退化 + 排序指标不劣于基线 + 隐私/执行错误为 0 → 首个 `candidate_for_shadow=true`;否则诚实 HOLD | 证据文档新增 `P44_G1_R3_*.md` |

**完成后的预期效果**:
- 若通过:项目历史上**第一个**拿到 `candidate_for_shadow=true` 的候选诞生——个性化从"仅证明无害"升级为"证明方向正确";
- 若 HOLD:得到明确的缺口清单(通常是某来源证据不足),回 Step 2/3 补数据——管道本身已验证。

## Step 5(M2-B2)个性化晋升 —— preview 第一次影响排序(Shadow → opt-in)

**前置**:Step 4 通过 + 晋升判定授权(决策点 4)。**首次修改生产排序代码,全程 feature flag。**

| # | 任务 | 涉及文件/模块 |
|---|---|---|
| 5.1 | 新开关 `PROACTIVE_RECOMMENDATION_PERSONALIZATION_MODE`(`off`/`shadow_compare`/`active`,默认 `off`) | `config/proactive_settings.py`、`config/__init__.py` |
| 5.2 | 排序消费:`score_breakdown` 新增 personalization component(delta = 冻结公式,硬裁剪 ±0.03;`off` 时代码路径完全短路) | `main_logic/proactive_recommendation.py` |
| 5.3 | 状态读取接口:决策前快照读取(沿用现有 point-in-time 机制),`ranking_consumed` 标记按模式如实翻转 | `main_logic/proactive_recommendation_feedback_state.py` |
| 5.4 | 观测:observation 记录 applied delta 与 baseline/personalized 双排序对照(`shadow_compare` 模式只记不用) | `main_logic/proactive_recommendation_observer.py`、`main_routers/system_router/proactive_chat_flow.py` |
| 5.5 | 单测 + testbench 对照(shadow_compare 数据回放验证与离线模拟一致) | `tests/unit/test_proactive_recommendation.py` 等;testbench `p55` 扩展 |

**完成后的预期效果**:
- `shadow_compare` 阶段:日志里首次出现「基线排序 vs 个性化排序」的实时对照,可量化差异频率——用户仍无感;
- 开发者 opt-in `active` 后:**推荐系统的实际投递首次带个性化**——常听完音乐的用户会更常收到音乐类搭话(幅度 ≤0.03,主要影响近分差场景),点过「不感兴趣」的来源被温和压低;
- 任何异常可 flag 一键回退,`off` 下行为与今天完全一致。

## Step 6(M2-B3)画像治理 —— 兑现隐私承诺

| # | 任务 | 涉及文件/模块 |
|---|---|---|
| 6.1 | 持久 affinity 时间衰减(无新证据时逐步回零,公式冻结进证据文档) | `main_logic/proactive_recommendation_feedback_state.py` |
| 6.2 | 用户删除入口:`POST /api/proactive/recommendation/state/reset`(清空 v2 状态文件 + 审计日志) | `main_routers/proactive_router.py` + 前端设置页入口 |
| 6.3 | 单测 + 文档 | `tests/unit/test_proactive_recommendation_feedback.py`、权威文档 §5.4 更新 |

**完成后的预期效果**:旧偏好自动淡出(不会"一次爱好定终身");用户可一键抹掉画像——个性化获得放量所需的治理完整性。

## Step 7(M3)决策门 —— 把误打扰率从 ~51% 压到 <25%

**前置**:Step 1 的标签(和/或 Step 3 的多用户标签)。分四小步,前三步零生产影响。

| # | 任务 | 涉及文件/模块 |
|---|---|---|
| 7.1 | 数据集组装:标签 × 特征(activity 状态、timing v3 五字段、个人回复延迟基线、会话接受度快照) | 新建 `tests/testbench/pipeline/recommendation_gate_dataset.py` |
| 7.2 | 分场景记分卡模型(idle/chatting 优先;确定性、可解释、纯 Python、无新依赖;F1 结论约束:**不做统一阈值**) | 新建 `main_logic/proactive_recommendation_gate.py`(先只被 testbench 引用)+ `tests/testbench/pipeline/recommendation_gate_eval.py` + 新烟测 `p57_recommendation_gate_smoke.py` |
| 7.3 | 离线验收(先定线再跑):留出集误打扰 <25% 且错失机会不劣于现状;LOSO 稳定;不足场景只报限制 | `recommendation_gate_eval.py` 报告 + 证据文档 `P44_H0_GATE_*.md` |
| 7.4 | 消费(需单独晋升授权):新开关 `PROACTIVE_RECOMMENDATION_GATE_MODE`(`off`/`shadow`/`active`);shadow 时 observation 记 gate 建议;active 时 gate 建议 skip → 走 `proactive_chat_flow.py` **现有** skip/pass 机制(不新增第二套调度) | `config/proactive_settings.py`、`main_routers/system_router/proactive_chat_flow.py`、`main_logic/proactive_recommendation_gate.py` |

**完成后的预期效果**:
- 7.1–7.3:observation 中出现 gate 打分与人工标签的对照报告——决策能力首次有可信的量化改进证据;
- 7.4 shadow:每轮记录"gate 本会建议跳过吗",可回放统计假想打扰减少量;
- 7.4 active(opt-in):**用户可感知的核心变化——不合时宜的主动搭话显著减少**(验收线:误打扰 <25%),桌宠"更懂什么时候闭嘴";这是全路线中对产品体验影响最大的一步。

## Step 8(M4)放量准备 —— 从 opt-in 到讨论默认开启

| # | 任务 | 涉及文件/模块 |
|---|---|---|
| 8.1 | propensity 日志(决策概率入 observation,需 schema 演进评审) | `main_logic/proactive_recommendation_observer.py` + 契约文档 |
| 8.2 | 离线策略评估 | 新建 `tests/testbench/pipeline/recommendation_ope.py` + 烟测 |
| 8.3 | Canary:样本量计算、指标(关闭率/负反馈率/留存代理)、自动回滚条件 | 设计文档 + 监控接线 |
| 8.4 | 运行时激活治理:`proactive_recommendation_runtime.py` 扩展带审批字段与审计日志的激活路径;`production_release_approved` 正式评审 | `main_logic/proactive_recommendation_runtime.py`、`main_routers/proactive_router.py` |

**完成后的预期效果**:推荐系统具备"安全放量"的全部基础设施;Canary 达标后才进入"普通用户默认开启"评审——届时新用户开箱即获得会挑时机、懂偏好的主动搭话,且随时可关。

## 贯穿工程卫生(随时可做,无需授权)

| 任务 | 涉及文件 | 效果 |
|---|---|---|
| CI 挂烟测与单测 | 新建 `.github/workflows/`(复用 `tests/testbench/smoke/_run_all.py`) | 分叉/回归在 PR 阶段被拦截 |
| 双分支每周合流 | — | 漂移门从常态防线退回兜底 |
| testbench 前端同步 main | `static/`、补入 `tests/unit/test_music_playback_static.py` | 静态契约测试恢复完整 |

## 依赖关系与建议顺序

```
Step 1(标注)──────┐
Step 2(负反馈)────┼→ Step 4(验收)→ Step 5(晋升)→ Step 6(治理)┐
Step 3(内测)──────┘         └────→ Step 7(决策门)─────────────┼→ Step 8(放量)
                                                                  ┘
```

Step 1/2/3 互不阻塞、可并行启动;Step 7 只依赖标签(1 或 3),可与 4–6 并行。
**建议第一批开工:Step 1(你出 1–2 小时)+ Step 2(Claude 1–2 天)**,Step 3 招募同步进行。
