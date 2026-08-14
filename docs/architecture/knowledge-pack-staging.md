# 知识包暂存与激活

用户导入的知识包不会直接写入在线知识库。Main Server 先把经过校验的五字段原始内容放入
`<knowledge-root>/.staging/<job-id>/`，后台完成 FTS、分块和可用的向量索引后，再替换在线来源。
更新期间旧版本继续服务，暂存内容不参与 BM25、语义检索、自动上下文或素材路由。

## 固定容量边界

- 规范 JSON 最大 10 MiB。
- 单包最多 5,000 个条目。
- 单包最多派生 5,000 个 chunk。
- 全部社区知识累计最多 10,000 个条目、10,000 个 chunk 和 64 MiB 正文。
- 在线 ready 向量总预算为 10,000 个 chunk。
- 暂存前至少保留 512 MiB，或预计工作空间两倍的可用磁盘容量，取较大值。

超过单包或社区累计限制时拒绝导入。同一知识包更新会先扣除在线旧版本再计算容量，不会重复
计费。超过在线向量预算时不拒绝知识内容，而是以 BM25 模式激活，避免 CPU 和内存随用户知识量
无限增长。HTTP 接口在流式读取过程中执行大小检查，不会先把超大请求完整载入内存。

## 状态与宽容降级

```text
queued → building_fts → embedding → active
                         ├─ hybrid
                         ├─ mixed
                         └─ bm25
```

Embedding 被禁用、不可恢复失败或超过向量预算时，任务仍可完成为 `bm25` 或 `mixed`，不会永久
卡住，也不会使聊天失败。取消和致命失败只保留不含正文的 `state.json`，暂存正文与数据库会被
清理。应用重启后，非终态任务由知识索引协调器继续处理。

管理接口：

```text
GET  /api/public-knowledge/packs/jobs
POST /api/public-knowledge/packs/jobs/cancel
```

维护脚本的默认 `--status` 会只读显示任务；`--preflight-pack PATH` 可在导入前检查容量，
`--cancel-job JOB_ID` 可取消暂存任务。
