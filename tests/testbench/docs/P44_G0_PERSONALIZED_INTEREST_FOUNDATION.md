# P44-G0 · MVP feedback-state preview 同步

状态：`complete`（v1 历史合同与 v2 只读合同均已同步）
MVP 所属分支：`feat/recommend-MVP`
Testbench 分支：`feat/recommend-testbench`
最近更新：2026-07-23

## 唯一语义来源

Testbench 直接使用 MVP production sanitizer，同时识别历史
`feedback_state_preview_v1` 与当前 `feedback_state_preview_v2`，不复制状态公式，
也不把 v1 迁移成 v2。

- 临时来源状态：2 小时 TTL；仅进程内，重启清除。
- 持久来源状态：只累计合格的显式证据；正负证据总数达到 3 后才形成
  `affinity_preview`。
- `reward_score_v2_preview_v2` 仍由 MVP 自己计算并仅作 Shadow/离线诊断；本
  Testbench 阶段不复制或重算其公式。状态 preview、排名和 tuning 的消费状态必须
  分别明确；当前 ranking/tuning 均为 `false` / HOLD。

## Testbench 同步合同

导入必须调用 MVP observation sanitizer。v1 继续保留原有 bounded aggregate；v2
只保留 `conversation_acceptance` 与 `source_affinity` 的 bounded aggregate。两版都
强制输出：

```json
{
  "preview_only": true,
  "ranking_consumed": false,
  "tuning_consumed": false
}
```

候选标题、回复正文、URL、token、cookie、payload、preview 内延迟明细及其他未知
字段必须被删除。原始 Feedback 白名单中的合法 `reply_latency_seconds` 继续保留。

Freeze、Golden 和 JSONL 均为只读来源。Testbench sanitizer 只生成新的内存视图或
新派生导出；未进入安全视图的字段仍留在原文件。既有 `review_context` 和 annotation
上下文继续保留，安全导出不得覆盖源文件或已有导出。

## 验收

`p52_feedback_state_preview_sync_smoke.py` 保留 v1 回归；
`p54_feedback_state_v2_readonly_sync_smoke.py` 验证：

1. v1/v2 可在同一输入中读取，且保持各自语义；
2. 边界外字段和隐私字段不进入派生视图，源对象和源文件不变；
3. review/annotation 上下文及合法 Feedback latency 保留；
4. prepare → write-new-export → read 可幂等 round-trip，且禁止覆盖；
5. `production_default` 排名不读取任一版本 preview，前后完全一致。

P52 已通过，说明 Testbench 已与 MVP preview 合同同步；不代表个性化推荐效果已被
证明，也不自动进入 P44-G1/G2/G3，更不允许修改生产权重或开启 tuning。
