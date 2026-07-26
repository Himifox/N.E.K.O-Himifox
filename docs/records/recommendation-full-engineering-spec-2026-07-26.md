# 推荐系统完整工程规格书(全量细节版)

> 日期:2026-07-26
> 性质:**工程规格提案**——每个 Part 进入实现前仍需按项目规矩单独立项;本文把可以在拿到数据之前确定的全部细节一次写清,凡必须由实验产出的值以 **⟨待数据:实验名⟩** 标注(见 Part 11 总表),不编造。
> 文中所有既有符号(函数名、常量、事件名、端点、文件)均已逐一从 `feat/recommend-MVP` 截点 `3c0626bf` 与 `feat/recommend-testbench` 截点 `3e21f5e4` 的代码核对;新增符号一律标 **(新增)**。
> 配套文档:[状态报告](./recommendation-system-status-report-2026-07-26.md) · [成熟化计划](./recommendation-maturity-plan-2026-07-26.md) · [深度技术路线](./recommendation-deep-technical-route-2026-07-26.md) · 学术路线 `docs/design/proactive-recommendation-academic-technical-route.md`(公式依据库,下称 §4.x/§5.x 均指该文)。

---

## Part 0 文档约定

- **MUST/禁止** = 规范性要求;**SHOULD/建议** = 默认做法,偏离需在立项蓝图说明;**⟨待数据⟩** = 实验输出,不得预填。
- 所有新增打分项 MUST 进入 `score_breakdown`(§4.4 纪律);所有新增行为 MUST 挂独立 feature flag 且默认最保守档;所有异常路径 MUST fail-open(推荐系统故障时主动搭话行为退回现状,绝不新增沉默或崩溃)。
- 版本号沿用深度路线:T0(数据层)、T1(个性化)、T2(决策门)、T3(产品化)。

---

## Part 1 现状精确基线(实测核对,写规格前的事实地板)

### 1.1 排序器打分链(`main_logic/proactive_recommendation.py`)

现有单候选分数由 `_score_candidate`(:742)组装,组件函数与 breakdown 键一一对应:

| 组件函数 | 语义 |
|---|---|
| `_context_match`(:793) | 活动状态与候选的上下文匹配 |
| `_user_interest_match`(:805) | 用户来源开关意向 |
| `_novelty`(:815) | 新颖度 |
| `_interaction_value`(:823) | 交互价值 |
| `_interruption_cost`(:837) | 打断成本(减项) |
| `_risk_penalty`(:847) | 风险惩罚(减项) |
| `_source_type_score_adjustment`(:863) | 来源静态权重(含 news −0.05 影子校准) |
| `_tuning_score_adjustment`(:867) | 调参层注入,钳制 ±0.15,默认断开 |
| `_diversity_penalty`(:878) | 来源/候选/streak 多样性惩罚 |

关键入口:`build_shadow_recommendation_decision`(:315)与 `build_phase1_material_shadow_decision`(:323)产出影子决策;`build_active_source_bias`(:358)+`reorder_phase1_topics_for_bias`(:415)是 active_source 唯一的行为出口(仅重排 Phase1 话题顺序,门槛 `ACTIVE_MIN_SCORE_GAP=0.05`);`resolve_recommendation_activity_state`(:92)读真实活动状态。

### 1.2 反馈事件全表(`proactive_recommendation_feedback.py` `_FEEDBACK_EVENT_SCORES`,:39)

| 事件 | 组 | 分值 | 置信度 |
|---|---|---|---|
| `user_reply_fast` | generic_engagement | +0.25 | medium |
| `user_reply` | generic_engagement | +0.15 | medium |
| `user_continue` | generic_engagement | +0.35 | medium |
| `ignored` | generic_engagement | −0.05 | low |
| `proactive_disabled_after` | settings | −0.70 | high |
| `source_disabled_after` | settings | −0.35 | medium |
| `music_played_through` | music | +0.90 | high |
| `music_high_completion` | music | +0.65 | high |
| `music_mid_completion` | music | +0.25 | medium |
| `music_normal_close` | music | +0.05 | low |
| `music_early_close` | music | −0.35 | medium |
| `music_hard_skip` | music | −0.70 | high |
| `music_not_started` / `music_error` / `autoplay_blocked` | music | 0.00(技术零分) | low |
| `mini_game_accept` | mini_game | +0.90 | high |
| `mini_game_later` | mini_game | +0.20 | medium |
| `mini_game_decline` | mini_game | −0.35 | high |
| `mini_game_ignored` | mini_game | −0.05 | low |

### 1.3 兴趣画像现状(`proactive_recommendation_feedback_state.py`)

常量:`TEMPORARY_INTEREST_TTL_SECONDS = 7200`、`PERSISTENT_INTEREST_MIN_EVIDENCE = 3`、`PERSISTENT_AFFINITY_MAX = 0.20`;双 scope `conversation_acceptance` / `source_affinity`;状态文件 `proactive_recommendation_feedback_state_preview_v2.json`(v1 只读);公开写入口 `update_conversation_acceptance_preview`(:36)、`update_source_affinity_preview`(:54),读入口 `get_feedback_state_preview`(:123)。**现状问题(T1.2 要解决的)**:`_persistent_score`(:316)在证据 ≥3 后输出阶跃到 ±0.20 的固定值,无衰减、无删除入口。

### 1.4 API 与前端接点

`main_routers/proactive_router.py`:`POST /recommendation/feedback`(:542)、`GET /recommendation/summary`(:378)、`GET /recommendation/runtime`(:501)、`POST /recommendation/runtime/rollback`(:510)、tuning 四端点(:599-:678)。前端上报:`static/jukebox/music_ui.js:1690` fetch `/api/proactive/recommendation/feedback`;`static/app/app-proactive.js` 中 `dispatchMusicPlay` 已携带 `turnId`/`sourceType`。

`proactive_chat_flow.py` 的 skip/pass 机制:`reason_code` 常量族(已核对存在 `PROACTIVE_REASON_PASS_PRIVACY`、`PROACTIVE_REASON_DELIVERY_PREEMPTED`、`PROACTIVE_REASON_CHAT_DELIVERED`)+ `_ensure_proactive_reason_code`(:81 导入)。T2 的 gate 建议 MUST 复用该机制。

---

## Part 2 T0a 显式负反馈入口 —— 完整规格

### 2.1 目标 / 非目标

- 目标:为用户提供一键"不感兴趣",产生**高置信、可归因、带来源方向**的负例。
- 非目标:不改变排序/PASS/投递/tuning;不引入"现在不方便"语义(那是时机问题,属 T2);不做撤销栈(见 2.6 交互裁决)。

### 2.2 事件契约(新增事件:`user_not_interested`)

请求(沿用 `POST /api/proactive/recommendation/feedback` 既有 body 结构):

```json
{
  "lanlan_name": "小天",
  "turn_id": "8f2c…",                  // MUST 来自被反馈的主动搭话响应体
  "event_type": "user_not_interested",
  "source_type": "music",              // 卡片可确定来源时 MUST 携带
  "candidate_id": "music:xxxx",        // 可验证时 MUST 携带;无则省略
  "ui_generation": "not_interested_button_v1"   // (新增字段) 分层分析用
}
```

校验规则(全部沿用既有 chokepoint 逻辑,新增项标注):
1. `turn_id` MUST 命中 pending feedback 窗口内的已投递 observation,否则按既有孤儿规则拒绝、不计任何状态;
2. `candidate_id`/`source_type` MUST 与该 observation 的 top candidates 对齐(既有校验),不对齐则降级为"无来源负反馈";
3. **(新增)** 同一 `turn_id` 的 `user_not_interested` 幂等:重复提交返回 200 但不重复累计(实现:沿用"同 turn 同组事件不重复累计持久证据"的既有规则,组名 `explicit_negative`);
4. **(新增)** `ui_generation` 仅允许白名单枚举值,长度 ≤64,进 observation 白名单(observer 同步)。

### 2.3 事件语义定义(与既有事件的边界)

| 对比事件 | 语义差 | 规格决定 |
|---|---|---|
| `music_early_close`(−0.35) | 行为推断,可能只是"现在不想听" | `user_not_interested` 是**素材维度**的显式否定,置信度 high |
| `source_disabled_after`(−0.35) | 关整个来源,设置行为 | 不感兴趣只针对本次素材/来源倾向,不动设置 |
| `ignored`(−0.05, low) | 沉默,弱信号 | 点了按钮 = 强信号,两者不可互推 |

分值提案:`"user_not_interested": ("explicit_negative", -0.35, "high")` —— 绝对值对齐 `music_early_close`/`mini_game_decline` 的 −0.35 档,置信度取 high(显式动作)。**是否用 −0.5 档 ⟨待数据:上线 2 周后按负反馈率与关闭率的相关性复核⟩**,首版从保守档起。

### 2.4 逐文件改动清单

**`main_logic/proactive_recommendation_feedback.py`**
1. `_FEEDBACK_EVENT_SCORES` 增一行(2.3 的三元组);
2. reward v2 组件映射表增 `("not_interested", -0.35)`(独立 component,不与 close 系列合并,保证可重算);
3. `_CONVERSATION_ACCEPTANCE_EVENT_TYPES` **不加入** `user_not_interested` 的正向路径——它进入**负向**接受度路由:新增集合 `_CONVERSATION_REJECTION_EVENT_TYPES = {"user_not_interested"}`(新增),仅当无有效 `candidate_id` 时更新会话接受度负向;
4. 来源路由:有有效 `candidate_id` + `source_type` 时调用 `update_source_affinity_preview(..., positive=False)`(该函数已支持负向证据参数,核对其签名后如不支持则扩展参数,兼容既有调用);
5. 事件落盘:沿用既有 JSONL sink(`FEEDBACK_LOG_FILENAME = "proactive_recommendation_feedback.jsonl"`),无新文件。

**`main_logic/proactive_recommendation_feedback_state.py`**
6. 负证据计入:临时桶(TTL 7200s 不变)与持久桶的 `negative` 计数字段(桶结构 `_empty_evidence_bucket`(:306)已含正负计数位,核对后缺则补);
7. `_persistent_score`(:316):负证据参与方向判定——现版仅正向阶跃,改为 `sign(pos−neg)·PERSISTENT_AFFINITY_MAX`(证据合计 ≥3 时);**此改动是 T1.2 平滑公式的前置垫层,行为变化仅体现在 preview 数值,排序仍不读取**;
8. 同 turn 去重:`explicit_negative` 组进既有去重账本。

**`main_logic/proactive_recommendation_observer.py`**
9. observation/feedback 白名单增 `ui_generation`(字符串,枚举白名单,越界即丢弃字段不丢事件)。

**`main_routers/proactive_router.py`**
10. `POST /recommendation/feedback`(:542)事件类型白名单增 `user_not_interested`;
11. `GET /recommendation/summary`(:378)的 reward preview 输出中 `not_interested` component 可见(自动获得,验证即可)。

**前端 `static/app/app-proactive.js`**
12. 主动搭话卡片渲染处增加操作区按钮(结构提案):

```html
<button class="proactive-not-interested" data-turn-id="…" data-source-type="…"
        data-candidate-id="…" hidden>🚫 不感兴趣</button>
```

交互规格:
- 桌面端 hover 卡片 300ms 后显示(移动端长按 500ms);
- 点击 → 立即 `fetch POST`(body 见 2.2)→ 按钮置灰 + 文案换 `已记录`,2s 后淡出;
- **不做确认弹窗、不做撤销**:裁决理由——负反馈本身就是低成本信号,撤销栈引入的状态复杂度(pending 撤销窗口内的画像回滚)超过误触代价;误触率由分层字段 `ui_generation` 事后量化,若 ⟨待数据:误触率>15%⟩ 再立项加 5s 撤销;
- 防抖:同按钮 5s 内重复点击忽略(幂等由后端兜底);
- 失败静默:网络错误只 console.warn,不打扰用户(反馈丢失可接受,打扰不可接受)。

13. i18n key(新增):`proactive.feedback.not_interested`(zh: 不感兴趣 / en: Not interested / ja: 興味なし)、`proactive.feedback.recorded`(已记录);落点跟随项目现有 i18n 文件结构(`static/` 内既有多语言表,实现时按现结构放置)。

**`static/jukebox/music_ui.js`**
14. 音乐卡片同样挂按钮,复用 :1690 的上报函数;与既有关闭分类逻辑互不干扰(点不感兴趣≠关闭播放器,两事件可共存于同 turn,分属不同组各自去重)。

### 2.5 隐私自查表

| 项 | 结论 |
|---|---|
| 新增字段含用户内容? | 否——`ui_generation` 为固定枚举 |
| 事件可反推屏幕/对话内容? | 否——仅 turn_id + 来源 + candidate ID(既有白名单元素) |
| 需要新增留存策略? | 否——进既有 feedback JSONL 滚动 |
| 进入画像的粒度? | 仅聚合计数(桶结构不变) |

### 2.6 测试矩阵(单测,文件:`tests/unit/test_proactive_recommendation_feedback.py` 扩展 + `test_proactive_recommendation_summary_router.py` 扩展 + 新前端 `tests/frontend/proactive_not_interested.test.cjs`)

| # | 用例 | 断言 |
|---|---|---|
| 1 | 带有效 candidate_id 的 not_interested | 来源负证据 +1,会话接受度不变 |
| 2 | 无 candidate_id | 会话接受度负向更新,来源证据不变 |
| 3 | candidate_id 与 observation 不对齐 | 降级为无来源负反馈 + 校验计数 |
| 4 | 孤儿 turn_id | 拒绝,零状态更新 |
| 5 | 同 turn 重复提交 ×3 | 幂等,证据只 +1 |
| 6 | 与 music_early_close 同 turn 共存 | 两组各自入账,不互相吞并 |
| 7 | reward v2 preview | 出现 not_interested 负 component,clip [−1,1] 生效 |
| 8 | 技术零分事件之后提交 | 不受污染,正常入账 |
| 9 | `_persistent_score` 方向 | pos=1,neg=3 ⇒ −0.20;pos=2,neg=1(合计3) ⇒ +0.20;合计<3 ⇒ 0 |
| 10 | 排序静态断言 | `proactive_recommendation.py` 不 import 新状态,分数逐位不变 |
| 11 | observer 白名单 | ui_generation 合法保留/非法丢弃 |
| 12 | summary 端点 | 新事件计入 feedback_calibration 的显式计数 |
| 13 | 前端 hover/长按显示 | jsdom 断言 hidden 切换 |
| 14 | 前端点击 → 请求体 | turn_id/source_type/candidate_id/ui_generation 齐备 |
| 15 | 前端失败静默 | fetch reject 不抛 UI 异常 |

testbench 侧:`p52`/`p54` 契约烟测增断言(新事件经生产 sanitizer 往返、preview_only 标志不变);`_run_all` 全量回归。

### 2.7 回滚与发布

回滚 = 从事件白名单移除(1 行)+ 前端按钮 feature 注释;历史 JSONL 保留(重放时未知事件按既有规则跳过)。发布顺序:后端先行(白名单打开但前端未发布,零流量)→ 前端跟进。

### 2.8 验收 gate 与预期效果

验收:测试矩阵 15/15 + 烟测无回归 + 影子运行 48h 无异常日志。
效果:负例开始以真实用户动作的速率积累;`affinity_preview` 首次可能出现负方向;**排序行为零变化**;2–4 周后 Part 5(T1.1)的验收前置(方向校准)具备数据。

---

## Part 3 T0b timing 标注轮 —— 操作与数据规格

### 3.1 流程(工具全部现成,列执行序)

```
prepare_timing_annotation_manifest.py   → manifest(101 条, 盲字段已剥离)
prefill_timing_annotation_assistant_draft.py → 助手草稿(0-3 分制,不计入人审)
人工主审(101 条)→ 主审文件
merge_recommendation_blind_review.py / normalize_recommendation_blind_review.py → 盲二审(21 条预选)
prepare_recommendation_adjudication.py → 分歧清单
finalize_recommendation_adjudication.py → 终审标签集
analyze_recommendation_timing.py → 门禁计算 + F2 重跑
```

### 3.2 标注记录 schema(与 R0 协议一致)

```json
{
  "turn_id": "…",
  "should_recommend": true,
  "confidence": "high|medium|low",
  "reason": "一句话,≤120 字,不引用结果字段",
  "annotator": "primary|blind_second|adjudicated",
  "manifest_sha256": "…"          // 绑定不可变 freeze
}
```

盲性约束(既有 `FORBIDDEN_BLIND_KEYS` 机制强制):标注者不可见 production score/rank、delivered 结果、feedback、inferred ignored、timing 五字段。

### 3.3 门禁精确计算

设标签集 L,四格 `cell(d,s) = |{x∈L : delivered(x)=d ∧ should(x)=s}|`:
`ready_for_f2_rerun ⟺ min(四格) ≥ 8 ∧ |delivered=true| ≥ 20 ∧ |delivered=false| ≥ 20`。
不达标 ⇒ 状态回 `hold`,缺格清单写入证据文档,标签需求转 Part 4 的多用户 cohort;禁止为凑格降门槛或二次挑选样本。

### 3.4 产出与效果

产出:终审标签集(不可变,SHA 绑定)+ 带标签 F2 重跑报告。效果:误打扰率/错失机会率首次可计算;Part 8(T2)的训练/验证数据地板成立。

---

## Part 4 T0c 多用户 cohort —— 数据规格

操作手册已在 testbench 分支:`tests/testbench/docs/RECOMMENDATION_BETA_COHORT_GUIDE.md`(同意书/env 配置/回收流程,不重复)。此处补数据规格:

- 导入包 = `export_recommendation_shadow.py` 产物(observations + feedback + `audit_shadow_dataset` 审计,≤1000 条/包);
- dataset 命名:`beta-<代号>-<日期>`;**参与者代号 MUST 不含真实姓名**;
- 合并 freeze:各 dataset 审计通过后合并冻结,记录 per-user 条数分布;单人占比 >40% 时 ⟨待数据⟩ 在分析中做 leave-one-user-out 稳健性检查(LOSO 的 user 版);
- 标签扩充:从多用户 freeze 抽样进入 3.1 同款标注流程(每用户等比抽样,盲于用户代号)。

---

## Part 5 T1.1 有界个性化消费 —— 完整规格

### 5.1 公式(精确定义)

对候选 c(来源 s = c.source_type),读取**决策前快照**(observation 内嵌的 `feedback_state_preview`,即产出该轮决策时已写入的那份,天然 point-in-time):

```
evidence(s)  = persistent.positive(s) + persistent.negative(s)
direction(s) = sign(persistent.positive(s) − persistent.negative(s))   // 0 则 delta=0
confidence(s)= min(1, evidence(s) / E_SAT)          // E_SAT = 12 (gradual_12, R2 已验证)
delta(c)     = clip(direction(s) · confidence(s) · DELTA_CAP, −DELTA_CAP, +DELTA_CAP)
               其中 DELTA_CAP = 0.03;evidence(s) < PERSISTENT_INTEREST_MIN_EVIDENCE(=3) 时 delta = 0
```

约束:`conversation_acceptance` MUST NOT 参与 delta(只描述"愿不愿意聊",不是素材偏好——G1 语义拆分的核心结论);mini_game / vision 首版不参与(证据通道未验证),参与来源白名单 = {music} 起步,扩展需单来源证据 ⟨待数据:各来源正负证据 ≥3/≥1⟩。

### 5.2 开关语义(新增 `PROACTIVE_RECOMMENDATION_PERSONALIZATION_MODE`)

| 档 | `_score_candidate` 行为 | observation 记录 | 用户可感知 |
|---|---|---|---|
| `off`(默认) | 代码路径短路,分数与现状逐位一致 | 无新字段 | 无 |
| `shadow_compare` | 计算 delta 但**不加进分数**;并行算出 personalized 排序 | `personalization_preview`: {delta per candidate, baseline_top1, personalized_top1, flipped: bool} | 无 |
| `active` | delta 计入分数,进 `score_breakdown["personalization"]` | 同上 + `applied: true` | 近分差场景的候选偏好倾斜 |

读取方式:`_read_str_env`,allowed 三值,启动时决定(与 MODE 同款);运行时只允许单向降档(复用 `proactive_recommendation_runtime.py` 模式,新增对应 runtime 字段与 `/recommendation/runtime` 输出)。

### 5.3 改动清单

1. `config/proactive_settings.py` + `config/__init__.py`:新开关(默认 `off`)+ 常量 `PERSONALIZATION_DELTA_CAP=0.03`、`PERSONALIZATION_EVIDENCE_SATURATION=12`;
2. `main_logic/proactive_recommendation.py`:`_score_candidate` 尾部新增 `_personalization_adjustment(ctx, candidate)`(新增函数),ctx 携带决策前快照(由 `proactive_chat_flow.py` 的既有快照读取处传入,**不得**在打分内即时读状态文件——保证同轮内一致性与可重放);
3. `main_logic/proactive_recommendation_observer.py`:`personalization_preview` 字段白名单(数值有界 ±0.03、来源枚举);
4. `main_routers/system_router/proactive_chat_flow.py`:两处决策构造点(:2005 区域 / :2372 区域)把快照传入 ctx;
5. `main_logic/proactive_recommendation_runtime.py` + `proactive_router.py`:降档端点 `POST /recommendation/personalization/rollback`(新增,语义同 runtime rollback:进程内单向、不写配置)。

### 5.4 验收前置(R3 离线验收,testbench)

在含负例的新 freeze 上跑 `recommendation_bounded_personalization.py` 扩展版,门禁(先定后跑):
- 方向校准:每个负证据主导的来源,delta ≤ 0(硬断言);
- 护栏:HHI 与最大来源曝光变化 ≤ ±0.005(与 R1 同精度);
- 排序:Golden 正例上 Hit@1/nDCG@3 不劣于 baseline(配对比较,确定性 bootstrap B=2000, 固定种子, 95% CI 下界 ≥ −0.01);
- 隐私/执行/硬约束错误 = 0。

### 5.5 测试矩阵(节选核心 10 条)

off 短路逐位一致(黄金输出快照对比)/ shadow_compare 不改分 / active 改分且 breakdown 可见 / evidence<3 零 delta / 方向翻转 / clip 上限 / 白名单外来源零 delta / conversation_acceptance 不参与 / 快照缺失 fail-open(delta=0)/ 降档端点单向性。

### 5.6 预期效果与已知局限

效果:消费链路打通,每条投递的个性化贡献可解释可审计;opt-in 用户在近分差场景获得温和偏好倾斜。
**诚实局限**:R2 实测非 Top-1 分差全部 >0.034 > DELTA_CAP,**本版对 Top-1 的翻转预期 ≈ 0**;它的价值是链路与治理,不是行为改变。行为改变的主力是 Part 8(决策门)与后续证据积累。

---

## Part 6 T1.2 Beta 先验 + 半衰期衰减 + 删除治理 —— 完整规格

### 6.1 公式

```
// 证据衰减(读/写时惰性执行)
pos'(s) = pos(s) · 2^(−Δt_days / H_s);  neg' 同理;Δt = now − last_update
// Beta 后验均值 → 方向分
affinity01(s) = (α0 + pos'(s)) / (α0 + β0 + pos'(s) + neg'(s))
signed(s)     = 2·affinity01(s) − 1                    ∈ (−1, +1)
// 与 T1.1 组合(替换其 direction·confidence 两项)
delta(c) = clip( signed(s) · min(1, (pos'+neg')/E_SAT) · DELTA_CAP, ±DELTA_CAP )
```

参数与选择方法:
- α0 = β0 = 1(Laplace 先验)起评,网格 {0.5, 1, 2};
- 半衰期 H:music 起评 14d、web 类 30d(对齐现有遗忘注释 music≈4.5d/web≈13d 的量级后统一评审——现注释值偏激进,以 ⟨待数据:离线重放中方向稳定性 vs 响应速度的帕累托前沿⟩ 定夺;评估指标 = ①方向月翻转次数(过敏感度)②新证据到方向更新的中位天数(迟钝度)③与人工标签方向一致率);
- 衰减实现:惰性(lazy)——`_persistent_bucket` 读写时按 `last_update` 补算,新增字段 `decay_version: 1`;禁止后台定时任务(无守护进程原则)。

### 6.2 迁移方案(关键:不做 in-place migration)

v2 preview 文件的证据计数**可从原始 feedback JSONL 完整重放**(项目既有承诺:preview 可重算)。因此:
1. 新算法上线时不改写旧文件;新文件 `…_state_preview_v3.json`(新增),从 JSONL 重放构建(重放器 = 既有归因链 + 新公式);
2. v2 文件转只读历史(同 v1 待遇);
3. observation 快照字段带 `preview_version` 区分;
4. 回滚 = 切回读 v2 文件,零数据损失。

### 6.3 删除治理

`POST /api/proactive/recommendation/state/reset`(新增,proactive_router):
- body `{ "lanlan_name": "…", "scope": "source_affinity" | "conversation_acceptance" | "all" }`;
- 行为:对应持久桶清零 + 临时桶清空 + 写一条审计事件(`state_reset`,进 feedback JSONL,不含被删内容);
- **原始 JSONL 不删**(immutable 纪律)——但重放器 MUST 尊重 reset 水位:重放时跳过 reset 时间戳之前的证据(实现:审计事件即水位标记);
- 前端:设置页"清除推荐偏好"按钮 + 二次确认。

### 6.4 测试矩阵(节选)

衰减半衰期数值断言(14d 后 pos 减半 ±1e-9)/ 惰性衰减幂等(连续读不重复衰减)/ 方向连续性(pos=neg ⇒ signed=0)/ 重放确定性(同 JSONL 两次重放逐位一致)/ reset 水位生效 / v2 文件未被写 / preview_version 传播 / 先验极限(零证据 ⇒ signed=0, delta=0)。

### 6.5 预期效果

阶跃(±0.20 定格)→ 连续曲线;停止互动的来源偏好按半衰期自然遗忘;负反馈立即压方向;用户一键清除;**放量所需的画像治理完整性成立**。

---

## Part 7 T1.3 会话内信号 · 速度 bonus · 重复/恢复 · MMR-lite(四个独立立项)

> 纪律(§4.4):四项 MUST 单变量逐项评估与上线,禁止一次叠加。每项结构:公式 → 参数 → 评估。

**7a 会话内跟随 `b_ephemeral`**:临时桶(TTL 7200s 既有)命中来源 ⇒ `+min(0.02, 0.01·临时正证据数)`;负临时证据 ⇒ 对称减。评估:重放对照会话内二次推荐接受率 ⟨待数据⟩。
**7b 个人回复速度 `b_speed`**:`0.05·sigmoid(z_speed)`,z_speed 由既有 G0-B 基线(`log(1+latency)` 中位数+MAD,≥5 次激活)——**实现已存在,只差把 preview 接进 `_score_candidate`**;约束:慢于本人基线不减分(sigmoid 下界处理:z<0 时 bonus→0 而非负)。
**7c 重复惩罚强化与单候选恢复**(§4.8):semantic repeat(同 family+近似 safe_title)软惩罚 −0.05;第三次同素材 cooldown 硬过滤 ⟨待数据:cooldown 时长⟩;唯一安全候选且被软约束抑制 ≥3 次 ⇒ recovery bonus +0.03(上限一次,投递后清零 suppressed 计数;`music_error` 类技术候选禁止触发)。
**7d MMR-lite**(§4.9):`MMR(c)=λ·s(c)−(1−λ)·max_sim(c,已选)`,sim = 5 布尔加权(同 candidate 1.0 / 同规范化标题 0.8 / 同 family 0.5 / 同 source 0.3 / 连续出现 0.2),λ 起评 0.85 网格 {0.75,0.85,0.95};仅作用于 Phase1 话题列表排序,无向量库。

---

## Part 8 T2 决策门 —— 完整规格(本文最重部分)

### 8.1 问题形式化

对每次"调度器已产生的搭话机会"x,学习 `P(should_recommend=1 | x)`,输出三态建议:`deliver` / `soft_skip`;PASS 语义(§4.3)不新增统一阈值,而是**分场景阈值化的概率模型**。基线对照 = 现状"永远 deliver"(误打扰 50.98%)。

### 8.2 数据集规格(新增 `tests/testbench/pipeline/recommendation_gate_dataset.py`)

输出 JSONL,每行:

```json
{
  "turn_id": "…",
  "label": {"should_recommend": true, "confidence": "high", "source": "adjudicated"},
  "features": { …见 8.3, 全部原始值… },
  "activity_scene": "idle",
  "cohort": "timing-101 | beta-<代号>",
  "manifest_sha256": "…",
  "feature_schema_version": 1
}
```

point-in-time 规则(逐条硬断言,违反即拒绝该行):特征只可来自该 observation 自身及其 `decision_context`/内嵌快照;禁止 join 未来 feedback、未来兴趣状态、未来曝光;禁止使用 delivered 结果本身作特征(标签泄漏)。

### 8.3 特征工程规格(逐特征)

| # | 特征 | 原始来源 | 变换 | 缺失处理 | 裁剪 |
|---|---|---|---|---|---|
| f1 | activity 场景 | `activity_snapshot.state` | 场景内建模,不进 x(见 8.4) | `unknown` ⇒ 不建模,恒 deliver | — |
| f2 | configured_interval_seconds | timing v3 | log(1+·) | null ⇒ 场景中位数(冻结进工件) | [0, 86400] |
| f3 | elapsed_since_last_delivery_seconds | timing v3 | log(1+·) | null ⇒ log(1+86400)("很久没投") | [0, 31536000] |
| f4 | interval_ratio = f3原始/f2原始 | 派生 | log(1+·) | 任一 null ⇒ 1.0 中性 | [0, 100] |
| f5 | recent_delivery_count_30m | timing v3 | 原值 | 必有(契约) | [0, 20] |
| f6 | recent_delivery_count_2h | timing v3 | 原值 | 必有 | [0, 50] |
| f7 | consecutive_unanswered_deliveries | timing v3 | 原值 | 必有 | [0, 20] |
| f8 | 个人回复速度 z | G0-B 基线快照 | 原值 | 未激活(<5 回复)⇒ 0 | [−5, 5] |
| f9 | 会话接受度方向 | v2 快照 conversation_acceptance | signed ∈ [−1,1] | 无状态 ⇒ 0 | — |
| f10 | 可用安全候选数 | 决策快照 | log(1+·) | 必有 | [0, 32] |
| f11 | 最高候选分 | 决策快照 top score | 原值 | 必有 | [0, 2] |

标准化:每特征 robust scale `(v − median)/max(IQR, ε)`,median/IQR 在**训练集**上计算并**冻结进模型工件**(推理期不得重算);ε=1e-6。
类不平衡:class weight `w_c = n/(2·n_c)`(按场景内标签分布)。
共线性:f3/f4 保留其一 ⟨待数据:场景内 |ρ|>0.9 则弃 f4⟩。

### 8.4 模型与拟合(新增 `main_logic/proactive_recommendation_gate.py`)

- 形式:每场景 a ∈ {idle, chatting}(首版仅此二场景,样本不足场景不建模)独立 `P = σ(w_a·x + b_a)`;
- 拟合:IRLS(Newton-Raphson on weighted least squares),规格:`max_iter=50`、收敛 `‖Δw‖∞ < 1e-8`、初始 `w=0, b=logit(先验阳性率)`、L2 λ 网格 `{0.01, 0.1, 1.0, 10.0}`(不惩罚截距)、全 float64、无随机源 ⇒ 完全确定性可复现;
- 数值防护:σ 输入 clip ±30;Hessian 加 λI 后 Cholesky,失败 ⇒ λ×10 重试,连续失败 ⇒ 该场景放弃建模(fail-open);
- 单调性检查(不做约束优化,做**事后校验**):`w(f7) ≤ 0`、`w(f5) ≤ 0` SHOULD 成立(连续未回应/半小时高频 ⇒ 更不该打扰);违反 ⇒ 报告并人工评审,不自动上线;
- 模型工件 schema(JSON,进 testbench exports,SHA 固化):

```json
{
  "gate_model_version": 1,
  "scene": "idle",
  "weights": {"f2": -0.12, "…": 0},  "bias": 0.35,
  "scaler": {"f2": {"median": 4.1, "iqr": 1.3}, "…": {}},
  "lambda": 0.1,
  "threshold": 0.0,                  // ⟨待数据:8.5 选定⟩
  "train_manifest_sha256": "…",
  "feature_schema_version": 1
}
```

拟合代码放 testbench(`recommendation_gate_eval.py` 新增);`proactive_recommendation_gate.py` 只含**推理**(点积+σ+阈值,纯函数)与工件加载校验——生产侧无训练代码。

### 8.5 评估协议(先定后跑)

混淆矩阵定义(场景内,gate 决策 g ∈ {deliver, skip},标签 s):
- 误打扰率 `FI = |g=deliver ∧ s=false| / |g=deliver|`;
- 漏报率 `MISS = |g=skip ∧ s=true| / |s=true|`(全体正例中被压掉的比例——**不用** "skip 中的正例占比",因为基线永远 deliver 时该量无定义);
- 基线:永远 deliver ⇒ `FI₀ ≈ 0.51`(E2 实测),`MISS₀ = 0`。

验收线:**存在阈值 θ_a 使 FI ≤ 0.25 且 MISS ≤ 0.20**,且:
- 时间切分(按 ts 前 70/后 30)与全量重拟合结论一致;
- LOSO(leave-one-source-out)各折 FI 均 ≤ 0.30(稳健性);
- 多用户数据可用时追加 leave-one-user-out;
- 确定性 bootstrap(B=2000,固定种子)FI 的 95% CI 上界 ≤ 0.30;
- 校准:可靠性曲线 10 等频 bin,`max|预测−实际| ≤ 0.15`(可解释性要求:P 值要"像概率");
- 阈值 θ_a 在验证集的 FI–MISS 帕累托前沿上选,倾向低 MISS 端(桌宠宁可偶尔多说,不可明显变冷淡)⟨待数据:θ 值⟩。

### 8.6 生产集成(新增 `PROACTIVE_RECOMMENDATION_GATE_MODE = off/shadow/active`)

| 档 | 行为 |
|---|---|
| `off`(默认) | gate 代码不加载,零开销 |
| `shadow` | 每次机会算 `gate_score` + 假想动作,写 observation 新字段 `gate_preview: {score, scene, suggested_action, model_version}`;行为零变化 |
| `active`(opt-in) | `suggested_action=soft_skip` ⇒ `proactive_chat_flow.py` 走既有 pass 出口,`reason_code = PROACTIVE_REASON_GATE_SOFT_SKIP`(新增常量,归入既有 `_ensure_proactive_reason_code` 体系) |

安全机制(active 档强制):
- **fail-open**:工件加载失败/特征缺失/任何异常 ⇒ deliver + debug 日志(沉默是事故);
- **防饿死熔断**:同 lanlan_name 连续 `GATE_MAX_CONSECUTIVE_SKIPS = 3` 次 soft_skip 后强制放行一次并记 `gate_forced_release`;
- **每日预算**:soft_skip 次数 ≤ `max(3, ⌈0.5 × 近 7 日日均机会数⌉)`(进程内计数,跨天重置),超出 ⇒ 全放行;
- **单向降档**:runtime 端点 `POST /recommendation/gate/rollback`(active→shadow,进程内,不写配置);
- 未建模场景(`unknown`/样本不足)恒 deliver。

### 8.7 监控指标(summary 端点扩展,精确定义)

`gate_skip_rate_24h = skips/opportunities`(同进程窗口);`gate_forced_release_count`;`gate_score_p50/p90`;shadow 档专属 `hypothetical_skip_rate`(假想拦截率,用于上线前回放论证);校准漂移 `calib_drift = |近7日实际回复率 − 对应 bin 预测均值|` ⟨待数据:告警阈值⟩。

### 8.8 测试矩阵(节选 12 条)

推理纯函数确定性 / 工件 SHA 校验失败 fail-open / 特征缺失路径全枚举 / unknown 场景恒放行 / 熔断第 4 次强制放行 / 预算越界全放行 / reason_code 进 observation 且 summary 可见 / off 档零 import(静态断言)/ shadow 档行为零变化(黄金对照)/ 降档单向性 / IRLS 黄金拟合(固定小数据集权重逐位复现)/ 单调性校验器本身的单测。

### 8.9 预期效果

shadow 期:回放可量化"假想少打扰多少次"(预期 `hypothetical_skip_rate ≈ 0.3–0.5`,⟨待数据⟩);active(opt-in)后:**误打扰 51% → ≤25%(验收线)**,主观体验 = focused/高频疲劳时段安静、idle 正常;每次 skip 可解释(记分卡权重×特征值明细)。这是全路线用户价值最大的一步。

---

## Part 9 远期(T1.4 bandit / T2.4 疲劳序列)——为什么现在只能写到这

**T1.4 source-level bandit**(§4.10):arms={music,news,video,meme,vision},context=8.3 特征子集+affinity,reward=`reward_score_v2`,候选算法序 LinTS→LinUCB→context-free TS 对照。**不可提前确定的硬依赖**:①非退化 propensity——需安全随机化立项获批(§4.11:确定性策略 propensity≡1.0 使 IPS/DR 无定义);②≥500 可归因反馈;③OPE 与真实 opt-in 对照的一致性验证。在此之前写任何参数都是编造。
**T2.4 疲劳/序列**:指数恢复公式保留为研究假设(§4.2),进入条件 = T2.2 残差分析显示 timing 特征外仍有序列结构 ⟨待数据⟩。

## Part 10 T3 产品化规格

### 10.1 开关组合语义矩阵(逐组合)

三新旧开关(MODE / PERSONALIZATION_MODE / GATE_MODE)共 3×3×3,有效组合按约束坍缩:`PERSONALIZATION=active` MUST 先经 `shadow_compare ≥ 2 周`;`GATE=active` MUST 先经 `shadow ≥ 2 周`;`MODE=off` 时其余强制视为 off。启动时校验非法组合 ⇒ 全部降为 off + 警告日志(fail-open 到最保守)。

### 10.2 自动降级守护(通用化 tuning 的健康暂停)

守护指标(进程内滑窗)→ 动作表:归因失败率 >20% ⇒ PERSONALIZATION 降档;HHI/最大曝光越护栏 ⇒ 同左;gate 预算连续 3 日打满 ⇒ GATE 降 shadow;任何模块异常率 >1% ⇒ 对应模块降档。全部单向、记 observation、恢复仅人工。阈值均 ⟨待数据:shadow 期分布的 P99⟩ 校准后冻结。

### 10.3 CI(新增 `.github/workflows/recommendation-ci.yml` 骨架)

```yaml
on: [pull_request]
jobs:
  recommendation:
    steps:
      - run: python -m venv .venv && .venv/bin/pip install <轻量依赖清单,见审计记录>
      - run: .venv/bin/python -m pytest tests/unit/test_proactive_recommendation*.py -q
      - run: for s in tests/testbench/smoke/p4*_*.py tests/testbench/smoke/p5*_*.py; do .venv/bin/python "$s"; done
```

### 10.4 Canary 规格(§5.8)

指标:显式负反馈率、来源关闭率、搭话回复率;样本量(双比例检验,α=0.05,power=0.8):
`n/组 = (z₀.₉₇₅+z₀.₈)²·[p₀(1−p₀)+p₁(1−p₁)]/(p₁−p₀)²`,p₀=基线负反馈率 ⟨待数据:多用户 shadow 期实测⟩,MDE 建议绝对 5pp;自动回滚线:负反馈率超基线 +5pp(单侧 95%)即回滚。达标 → `production_release_approved` 评审会(治理文档另立)。

## Part 11 ⟨待数据⟩ 占位符总表(本文全部未定值及其产出实验)

| 占位 | 产出于 | 位置 |
|---|---|---|
| not_interested 分值升档与撤销功能 | T0a 上线 2 周复核 | 2.3/2.4 |
| 单人占比稳健性处理 | 多用户 freeze 分布 | 4 |
| 个性化来源白名单扩展 | 各来源证据积累 | 5.1 |
| 半衰期 H 与 α0 终值 | T1.2 离线帕累托 | 6.1 |
| 7c cooldown / 7a 接受率 | 各自单变量评估 | 7 |
| f4 取舍、θ_a、告警阈值 | T2.2 训练与 shadow | 8.3/8.5/8.7 |
| hypothetical_skip_rate 实测 | T2 shadow 期 | 8.9 |
| 降级守护阈值 | shadow 期 P99 | 10.2 |
| Canary p₀ 与样本量 | 多用户 shadow | 10.4 |
| bandit 全部参数 | 随机化获批后 | 9 |

## Part 12 版本-效果-验收终表

| 版本 | 系统行为变化(用户视角) | 量化验收 | 回滚方式 |
|---|---|---|---|
| T0a | 卡片多一个"不感兴趣"按钮 | 测试 15/15;影子 48h 干净 | 白名单摘除 |
| T0b/T0c | 无 | 四格门禁 / freeze 落档 | — |
| T1.1 | opt-in 后近分差偏好倾斜(预期翻转≈0,链路价值) | R3 四门禁 | flag off |
| T1.2 | 偏好会遗忘、可删除 | 重放确定性 + 半衰期断言 | 切回 v2 文件 |
| T1.3(×4) | 会话跟随/慢用户公平/重复减少/来源不饿死 | 各自单变量门禁 | 各自 flag |
| T2 shadow | 无(积累假想拦截证据) | 校准+FI/MISS 达线 | flag off |
| T2 active | **打扰显著减少(51%→≤25%)** | 8.5 全套 + shadow 一致 | 单向降档 |
| T3 Canary | 面向小流量真实用户 | 10.4 样本量与回滚线 | 自动回滚 |

> 实施序与立项对应关系见[成熟化计划](./recommendation-maturity-plan-2026-07-26.md)决策点表;每 Part 开工前以本文对应章节为蓝图底稿,补该 Part 的 ⟨待数据⟩ 项后过设计评审。
