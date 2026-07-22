# P44-G0 · MVP feedback-state preview 同步

状态：`complete`（Testbench 合同同步已验收）
MVP 所属分支：`feat/recommend-MVP`
Testbench 分支：`feat/recommend-testbench`
生效日期：2026-07-22

## 唯一语义来源

G0 使用 MVP 已提交的 `feedback_state_preview_v1`，不新建第二套兴趣模型。

- 临时来源状态：2 小时 TTL；仅进程内，重启清除。
- 持久来源状态：只累计合格的显式证据；正负证据总数达到 3 后才形成
  `affinity_preview`。
- `reward_score_v2_preview_v2` 仍由 MVP 自己计算并仅作 Shadow/离线诊断；本
  Testbench 阶段不复制或重算其公式。状态 preview、排名和 tuning 的消费状态必须
  分别明确；当前 ranking/tuning 均为 `false` / HOLD。

## Testbench 同步合同

导入必须调用 MVP observation sanitizer，并保留唯一允许的 bounded aggregate：

```json
{
  "feedback_state_preview": {
    "version": "feedback_state_preview_v1",
    "preview_only": true,
    "ranking_consumed": false,
    "tuning_consumed": false,
    "temporary": {"ttl_seconds": 7200, "sources": {}},
    "persistent": {"min_explicit_evidence": 3, "sources": {}}
  }
}
```

候选标题、回复正文、URL、token、cookie、payload、延迟明细及其他未知字段必须被
删除。导入后的 JSON 持久化再读取必须保持完全相同的安全 preview。

## 验收

`p52_feedback_state_preview_sync_smoke.py` 验证：

1. MVP 的 2 小时 TTL、3 条显式证据门槛、preview-only 标志均被保留；
2. 边界外字段和隐私字段不进入 Testbench 数据；
3. Testbench import preparation → 原子 JSON 写盘 → 读取的 round-trip 无漂移；
4. `production_default` 排名不读取 preview，前后完全一致。

P52 已通过，说明 Testbench 已与 MVP preview 合同同步；不代表个性化推荐效果已被
证明，也不自动进入 P44-G1/G2/G3，更不允许修改生产权重或开启 tuning。
