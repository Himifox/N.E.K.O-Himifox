# P44-F2-R0：Timing Evidence Restart Design

> 状态：**已实施预检，待人工盲标**  
> 分支：`feat/recommend-testbench`  
> 范围：仅 Testbench、不可变 freeze 与人工评审工作流  
> 不改变：MVP、scheduler、router、权重、interval、tuning、observation schema

## 1. 目的与前提

P44-F2-B 的 `no_candidate` 不是“没有时间/疲劳效应”，而是当前
105 条 timing-v3 freeze 缺少同 cohort 的人工 `should_recommend` 标签，
所以无法计算误打扰和错失机会。

R0 的第一动作不是采新数据，而是检查现有 freeze 能否成为一个新的
盲标 cohort。若现有安全 `review_context` 足以支持盲标，就只新增一个
外部 annotation manifest；freeze 本身、其 hash 和观察日志永远不改写。
只有 manifest 的结构预检整体不合格，或人工评审后有效单元不足时，
才可以另行决定是否采集新 cohort。

正式输入：

- Freeze：`shadow-p44f2-timing-v3-baseline-20260721-103709.json`
- File SHA-256：`e79e2b3258e55a29109525cdbb00e511ee7b4142e0a204ec40df8e2961a88bd7`
- 截点：2026-07-21 10:33:52 Asia/Shanghai

## 2. R0 预检结果

本次预检从原 freeze 构建独立 manifest，结果为：

- 105/105 条都具有可结构化检查的 `review_context`；
- 105/105 条具备至少一个带稳定 candidate ID、来源和安全标题/摘要的候选；
- 4 条技术退出（3 `DELIVERY_PREEMPTED`、1 `PASS_GENERATION_EMPTY`）不进入
  后续 F2 指标分母；因此分析可用上限为 101；
- manifest 预先选出 21 条独立盲二审样本：已投递侧 11、未投递侧 10。
  该分层信息只保留在管理员预检逻辑中，reviewer bundle 不暴露它。

这只证明“可以发起盲标”，不证明每一条在语义上都足够。评审者可以、
也应当，在证据不足时选择 `abstain`，并填写
`insufficient_review_context`。弃权不会被强行补标签。

## 3. Immutable manifest 与盲性

manifest 以 `turn_id + freeze SHA-256` 绑定来源，包含：

- `activity_state`；
- `candidate id / source_type / safe_title / safe_summary`；
- `redaction_notes`；
- primary review、预分配的 blind second review 和 adjudication 空槽。

manifest 明确排除：

- production score、rank、selected source；
- `delivered`、reason、delivered excerpt 和下游生成文本；
- feedback、inferred ignored；
- 五个 timing 字段及整个 `decision_context.timing`；
- privacy/token/cookie/URL 等任何非白名单字段。

`validate_timing_annotation_manifest()` 递归拒绝这些泄漏字段。不能把
管理员的 readiness 报告与 reviewer manifest 混成同一文件。

## 4. 人工标注协议

每个 primary reviewer 只填写：

```json
{
  "status": "completed",
  "reviewer_id": "reviewer-a",
  "reviewed_at": "2026-07-21T13:00:00+08:00",
  "should_recommend": true,
  "confidence": "medium",
  "reason_code": "candidate_appropriate",
  "comment": "可选简短理由"
}
```

允许值：

- `should_recommend`：`true` / `false`；看不出时用 `status=abstained`，不填布尔值；
- `confidence`：`low` / `medium` / `high`；
- `reason_code`：`candidate_appropriate`、`candidate_irrelevant`、
  `activity_unsuitable`、`repeat_or_fatigue`、`privacy_or_safety`、
  `insufficient_review_context`、`other`；
- 弃权理由：`insufficient_review_context`、`privacy_redaction`、
  `ambiguous_candidate_context`、`other`。

二审人必须不同于主审人，且同样看不到 outcome、feedback 和 timing。
主/二审都完成但 `should_recommend` 不一致时，进入 adjudication；裁决写入
独立字段，不覆盖两份原始评审。

## 5. 预冻结指标与 readiness

只有同时满足以下条件，状态才从 `hold` 变为 `ready_for_f2_rerun`：

1. 所有分析可用样本完成主审或明确弃权；预分配的二审全部完成或弃权；
   所有 completed/completed 分歧已裁决。
2. 合格、非弃权且已投递样本至少 20；合格、非弃权且未投递样本至少 20。
3. 四个交叉单元各至少 8：
   `delivered × should=true`、`delivered × should=false`、
   `pass × should=true`、`pass × should=false`。

指标定义固定为：

| 指标 | 分子 | 分母 |
|---|---|---|
| 误打扰 | 合格已投递且人工 `should_recommend=false` | 全部合格已投递且非弃权样本 |
| 错失机会 | 合格未投递且人工 `should_recommend=true` | 全部合格未投递且非弃权样本 |
| 显式反馈覆盖 | 有有效 `turn_id` 显式关联 feedback 的合格已投递样本 | 全部合格已投递且非弃权样本 |

隐私硬拦截、技术失败、弃权与 inferred ignored 均不进入以上分母。`PASS_DUPLICATE`
不是技术失败，保留为“未投递”一侧的候选观测；其含义由盲标后的人工标签决定。

即便总数达到 105，只要任意交叉单元不足，结论仍为 `hold`。这避免用总样本
掩盖“没有足够的误打扰或错失机会反例”。

## 6. 执行与停止边界

生成命令：

```powershell
uv run python tests/testbench/tools/prepare_timing_annotation_manifest.py `
  tests/testbench_data/recommendation/exports/shadow-p44f2-timing-v3-baseline-20260721-103709.json `
  --manifest-output tests/testbench_data/recommendation/exports/shadow-p44f2-timing-v3-baseline-20260721-103709-timing-annotation-manifest.json `
  --preflight-output tests/testbench_data/recommendation/exports/shadow-p44f2-timing-v3-baseline-20260721-103709-timing-annotation-preflight.md
```

R0 结束后：

- 若 readiness 为 `hold`：停止，报告缺少的单元；不默认重采，也不设计公式。
- 若 readiness 为 `ready_for_f2_rerun`：仅重新运行原 P44-F2 关联分析。
- 只有结果同时支持稳定的反馈和误打扰关系，才可以另立任务设计一个
  Testbench-only fatigue candidate；它仍不构成 MVP 改动授权。

P49 smoke 覆盖：manifest 脱敏/盲性、技术失败排除、二审与裁决门禁、四格
样本门槛，以及 synthetic positive-control 的 `ready_for_f2_rerun` 路径。

## 7. 助手预标注与人工确认

为了减少第一次人工浏览负担，可以从 manifest 生成单独的助手草稿。它不写入
`primary_review`，不会影响 readiness，也不能替代主审：

```powershell
uv run python tests/testbench/tools/prefill_timing_annotation_assistant_draft.py `
  tests/testbench_data/recommendation/exports/shadow-p44f2-timing-v3-baseline-20260721-103709-timing-annotation-manifest.json `
  --output tests/testbench_data/recommendation/exports/shadow-p44f2-timing-v3-baseline-20260721-103709-timing-annotation-assistant-draft.json
```

若已回填因果受限的对话上下文，草稿仅在没有该上下文时弃权。所有候选都以
同一 0–3 分制竞争；是否可打扰不是另设的助手硬拦截，而是 vision 的最终竞争
分依据。人工确认时必须把自己的结论、身份和带时区时间写入 `primary_review`，
而不是把助手字段直接计为已审。

助手草稿还按 freeze 顺序维护**所有来源**的候选重复账本：同一稳定 candidate
ID（缺失时用来源 + 规范化标题/摘要）第二次出现即降分，第三次及以后为 0 分；
meme 的第二次出现采用更严格的最高 1 分。清单会显示 occurrence 和 penalty。
该规则只影响助手候选相关性草稿，不替代生产重复惩罚，也不读取投递或 feedback。

`vision` 例外：它来自屏幕分享或窗口 title，因此语义相关性固定为 3；
`screen_context` 是脱敏占位而非视觉内容身份，不能因标题相同推断重复。它与
music/news/meme/video 仍在同一 0–3 分制中竞争，但最终竞争分按可打扰性调整：

| 可打扰性 | 依据 | vision 最终竞争分 |
|---|---|---:|
| `open_visual_focus` | `idle` 且用户明确提及屏幕/窗口/界面等视觉对象 | 3 |
| `open` | 普通 `idle` | 2 |
| `uncertain` | `unknown` | 1 |
| `restricted` | `busy`、`gaming`、`focused_work`、`chatting` 或追问/纠错语气 | 0 |
| `unavailable` | `away` | 0 |

因此 vision 在可打扰时具有高权重；不宜打扰时不会被硬删除，而是以低分与其它
资源继续比较。生产现有的隐私、技术失败、显式关闭等硬约束仍然优先。

News 与 video 的有效新鲜候选有共同基础分 1；明确主题匹配才升至 2，不能因最后
一句用户话没有精确关键词就直接归零。相同内容的重复惩罚仍单独生效。后续离线

对于重复出现的、且仅有相同脱敏 review context 的 vision 行，助手草稿只作
**标注去重**：首条保留为候选性预标注，后续条目写为 `abstained` 并要求人工复核。
这不是 episode fatigue、调度回退或来源权重公式，不能进入生产排序，也不得用于
生成新的 P44-F2 候选。

若本地测试对话允许用于复核，可使用 `recover_timing_annotation_context.py`
从 `time_indexed.db` 只读回填不晚于 observation 的最多 10 条消息。生成的是
新的 derived manifest，绝不改 freeze；不带消息时间戳、投递、反馈、分数或
timing。P50 smoke 固化该因果边界。

## 8. 收口决定（2026-07-21）

本轮不启动新的人工主审。已有 P44-E2 的 128 条人工 Golden 与 timing-v3 的
105 条 freeze 属于不同 cohort，不能混用；要求用户为 timing cohort 重复进行
第五轮审查不在当前授权内。R0 产物仅保留原始 freeze、结构审计与重复上下文
发现，状态为 `HOLD / no_candidate`。不得据此新增 episode fatigue、来源机会
补偿、调度回退、生产权重或 tuning 候选。
