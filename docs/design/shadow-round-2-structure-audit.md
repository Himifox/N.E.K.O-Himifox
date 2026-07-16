# Shadow Round 2 结构审计报告

## 审计范围

- 数据冻结：`shadow-round-2-freeze.json`
- SHA-256：`40A8889333139B9F47F1747A8622DD4A26D09670B81180C0E99ADC562F1A40B0`
- 标注模板：`shadow-round-2-annotation-template.json`
- SHA-256：`A1F1BFDA9A7457F0EF372AE09A6BE159573315B7E3DF3970E6DB8150F34F7B9F`
- 冻结名称：`production-shadow-2026-07-15`
- 冻结时间：`2026-07-15T06:45:06.940800+00:00`

本报告只做结构、关联和分布审计，不对候选语义质量做 relevance 判断。

## 结构结论

Observation schema v2 结构健康：

- observation 共 85 条，85 条均为 `0.8.3:proactive-recommendation-observation-v2`。
- algorithm version 未混用。
- 无无效 observation index。
- turn ID 无重复。
- feedback event 共 21 条，无无效 feedback index。
- tuning 保持只读 preview，本报告未应用任何权重修改。

## Activity 覆盖

| Activity | 数量 | 占比 |
|---|---:|---:|
| idle | 64 | 75.29% |
| unknown | 11 | 12.94% |
| focused_work | 8 | 9.41% |
| chatting | 2 | 2.35% |

本轮已经覆盖 `idle`、`focused_work` 和 `chatting`。`away`、`gaming` 尚无真实 Shadow 样本，可继续由 builtin 场景提供契约覆盖，等待后续真实采集补齐。

## 推荐来源分布

| 来源 | 数量 | 占比 |
|---|---:|---:|
| vision | 34 | 40.00% |
| meme | 29 | 34.12% |
| news | 13 | 15.29% |
| music | 8 | 9.41% |
| video | 1 | 1.18% |

来源覆盖满足结构分析需要，但 `vision + meme` 占 74.12%，后续语义复核样本应主动检查这两类是否存在场景偏置。

## Feedback 关联

| 项目 | 数量 |
|---|---:|
| observation | 85 |
| feedback event | 21 |
| 显式关联 observation | 15 |
| 显式关联率（15 / 85） | 17.65% |
| inferred ignored | 23 |
| scored（显式 + inferred） | 38 |
| 未评分 observation | 47 |
| orphan feedback turn ID | 2 |

显式 join 与 inferred ignored 已按 schema v2 拆分。人工证据门槛只能使用显式关联数 15，不能把 23 条 inferred ignored 计为真实反馈。

## Tuning preview

- `suggested_weight_adjustments`：空。
- meme、news、vision 均只出现 `weak_ignored_pressure`，置信度为 low。
- 三者 `suggested_delta` 均为 `0.0`，`write_mode` 为 `manual_review_only`。
- 当前 `active_ready_by_feedback=false`。
- 原因包括显式反馈不足、high score bucket 缺失、Top-1 正向率不足和负向率过高。

结论：当前数据没有形成可执行的 tuning 建议，必须继续保持 tuning off。

## 人工 relevance 标注阻断结论

当前 observation 的 `top_candidates` 只有：

- candidate ID
- source type
- family
- rank / score

当前数据不包含候选安全标题、候选短摘要或实际投递文本摘录。仅凭 ID、来源和分数，审核者无法判断“是否该推荐”“Top-1 是否合理”或候选 relevance 0–3。

因此：

- 当前 freeze 和 annotation template 只能作为结构审计证据。
- 当前 85 条 observation 不得进入人工语义 relevance 标注。
- 不得用基于该模板的空白或猜测性评分形成 Golden 数据。
- 只有通过 P44-B/C 安全导出并包含有效 `review_context` 的新 observation，才允许进入 P44-E。

## 阶段结论

P44-A/B/C 已完成，Shadow round 2 在结构层收口，安全 `review_context` 契约与 annotation-ready 门禁已经落地。下一步由用户开启 review 模式并重新采集 20–30 条小样本；在新样本通过安全校验前继续停止人工 relevance 标注。
