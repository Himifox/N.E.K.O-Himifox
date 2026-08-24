# PR #2951 公共知识边界收敛设计

> 状态：实施中设计记录。本文针对 PR #2951 当前 23 条未解决审查评论，定义修复边界、失败语义与验收条件。实现完成后，代码和测试是最终事实来源。

## 目标与非目标

本轮不逐条堆叠补丁，而是把评论收敛为 11 个拥有明确责任边界的修复单元。目标是：

- 已提交的数据不能被后续辅助步骤改写成失败；
- 异步路由不直接执行可能阻塞的磁盘或 SQLite 操作；
- 后台任务、请求体、缓存和轮询都有固定上限及可观察终态；
- 检索过滤、模型预热和配置错误采用一致、可恢复的降级语义；
- 管理界面区分输入错误、服务错误、超时等待和真实任务失败；
- 维护脚本和请求生命周期显式释放资源。

非目标：改变知识包五字段 Schema、扩大当前容量预算、引入新的远端服务、改变 Memory Server 边界，或为修复评论重新设计整个公共知识 API。

## 全局不变量

1. `packs.json` 与在线 `knowledge.db` 中已激活来源共同表示安装事实；`state.json` 是可恢复作业日志，不得反向否定已经完成的安装。
2. 事件循环只负责协调。可能等待文件锁、SQLite busy timeout、迁移或大量反序列化的工作必须进入工作线程。
3. 所有 fire-and-forget 任务必须有强引用、并发上限、去重键、终态记录和清理路径。
4. 任意入站正文都必须在累积到内存前执行字节限制；`Content-Length` 只用于快速拒绝，不能替代流式计数。
5. 自动检索失败时可以降级为无知识或 BM25，但不能中断正常会话。
6. 超时表示“客户端停止自动等待”，不等同于“服务端任务失败”。
7. 兼容性版本覆盖确定性分块算法；改变 chunk 序列时必须同步处理预构建索引契约。

## 修复单元 A：知识包激活提交点

涉及：`knowledge/pack_jobs.py` 的激活状态分裂评论。

### 边界

`install_pack()` 成功返回是不可逆提交点。此前的异常可以把作业置为 `failed`；此后的路由刷新、状态日志和清理属于收尾步骤，不得再把作业写成 `failed`。

### 设计

- `_activate_job()` 返回结构化结果，至少包含 `committed`、`state`、`retrieval_mode` 和可选 `warning`。
- 提交前再次检查取消状态；提交开始后不再接受取消。
- `install_pack()` 成功后先尝试原子写入 `state=active`，再执行路由刷新和 payload 清理。
- 路由刷新失败只记录警告。后续查询可通过已有数据库变更通知或下一次刷新恢复。
- 若 active 状态写入失败，保留 staging payload，返回 `committed=True`；外层不得覆盖为 failed。下一轮通过注册表中的 `pack_id`、来源和订阅版本核对安装事实并补写 active 状态。
- 只有 `committed=False` 的异常路径可以写 `failed` 并清理正文 payload。

### 验收

- 注入 `refresh_routing_index()` 异常后，在线包存在，作业不是 failed。
- 注入 active 状态写入异常后，在线包存在、payload 保留；恢复轮次可收敛到 active。
- `install_pack()` 本身失败时仍保持旧在线版本并进入 failed。

## 修复单元 B：异步协调与阻塞 I/O

涉及：Router 初始化、pack job SQLite 查询两条评论。

### 边界

- 新增 `_service_async()`，使用 `asyncio.to_thread(_service)` 完成可能触发迁移的服务构造。
- 所有异步公共知识路由先 `await _service_async()`，再把服务方法放入 `to_thread`；禁止在 `to_thread(_service().method)` 的参数求值阶段同步构造服务。
- `process_pack_jobs()` 把 `chunk_status()`、被替换 ready chunk 统计等同步读取合并到一个工作线程 helper，确保同一轮容量决策来自一个同步快照。
- 文档中“异步协调路径不阻塞事件循环”的描述以该规则为准。

### 验收

- 用线程标识断言服务初始化、staging 状态读取和在线状态读取均不在事件循环线程。
- SQLite busy timeout 不阻塞一个并行的事件循环 tick。

## 修复单元 C：订阅任务所有权、去重与容量

涉及：`knowledge_market.py` 的任务强引用和无限并发两条评论。

### 边界

- `_task_workers: dict[str, asyncio.Task]` 保存强引用；任务终态仍保留在 `_tasks`，按既有 TTL 清理。
- 同一 `package_id` 同时最多一个任务。相同版本/频道的重试返回既有 `task_id`；不同版本与正在执行的同包任务冲突时返回 409。
- 全局最多 4 个活动订阅任务。达到上限返回 429，并携带稳定错误码 `knowledge_subscription_busy`；不建立隐藏排队队列。
- done callback 只移除仍指向自身的 worker，主动读取任务异常，避免“Task exception was never retrieved”。
- TTL 清理不得删除仍有活动 worker 的任务记录；服务关闭时取消并等待全部 worker。

### 验收

- 任务在 GC 后仍运行；完成后 worker 引用被移除、状态记录保留。
- 同包重复提交只有一次下载；不同版本冲突；第五个并发任务被拒绝。
- 一个任务失败不影响其他任务，也不会遗留未读取异常。

## 修复单元 D：Bridge 请求体预算

涉及：`market_bridge.py` 无界 `request.body()` 评论。

### 边界

- Bridge 不调用 `request.body()`。先检查合法 `Content-Length`，随后按 64 KiB 流式读取并累计。
- `packs/import` 上限为 `MAX_PACK_BYTES + 64 KiB`。
- `subscriptions/apply` 上限为正文、manifest、vectors 三项既有上限之和，再加 256 KiB multipart 开销。
- 其余 JSON mutation 统一上限 64 KiB。
- 超限在 Bridge 返回 413，稳定错误码 `knowledge_request_too_large`，且不向 Main Server 发起请求。
- 当前容量下可以在通过上限检查后一次性转发 bytes；关键安全边界是“先限流、后聚合”，无需引入不可重放的双向流。

### 验收

- 有、无、伪造 `Content-Length` 三种情况下都不能超过上限。
- 超限请求不调用下游 client；边界值请求原样转发。

## 修复单元 E：检索过滤正确性

涉及：禁用条目在候选截断后过滤的评论。

### 边界

- 在发出 FTS/LIKE 查询前读取 disabled 集合。
- 每条查询的候选预算为原预算加 disabled 数量，并受数据库总条目上限约束；这保证被禁用候选不能挤掉本应进入原候选窗口的启用条目。
- 排序、合并和最终 `limit` 仍只作用于启用条目；`include_disabled=True` 保持原行为和原预算。
- override 文件无效继续抛出领域错误，由管理 API 显示可诊断状态；自动会话路径按既有安全降级返回空结果。

### 验收

- 前 12 个强匹配均禁用、第 13 个启用时仍能返回第 13 个。
- `include_disabled=True` 可返回禁用项；来源过滤和 FTS/LIKE 去重不回归。

## 修复单元 F：注册表元数据与统计查询分层

涉及：自动会话每包查询、legacy migration 重复全表扫描两条评论。

### 边界

- `list_installed_packs()` 保留面向管理 UI 的富统计语义。
- 新增只读注册表快路径，只返回已校验的 `source_tag`、`auto_context` 和有效 material type，不打开 SQLite。自动会话只调用该快路径。
- KnowledgeStore 新增按来源一次聚合 entries/chunks/ready 的查询；legacy migration 每个数据库最多执行一次聚合，禁止每个 pack 调用 `list_active_entries()` 或 `source_chunk_status()`。
- 注册表损坏时快路径返回空社区来源，内置来源仍可用。

### 验收

- 自动会话来源选择不构造 KnowledgeStore、不执行逐包状态查询。
- 100 个 pack 的迁移统计仍只有常数次数据库查询，并保持原统计值。

## 修复单元 G：向量模型预热与超限负缓存

涉及：预构建索引不预热模型、超限 snapshot 反复加载两条评论。

### 边界

- `index_embedding_batch(load_model=True)` 的 `load_model` 同时表示“确保查询模型可用”。因此模型请求必须发生在 `no_work` 提前返回之前；`load_model=False` 绝不隐式加载模型。
- snapshot 缓存支持 `ready` 与 `rejected` 两种记录。拒绝记录至少绑定数据库 identity、chunk revision、模型 ID 和拒绝原因。
- 命中 rejected 记录时立即抛出相同的 `MemoryError`，不再加载向量行；数据库 identity/revision 或模型变化后自然失效并重试。
- 缓存只保存原因和键，不保存超限矩阵。

### 验收

- 只有预构建 ready chunks、没有本地 embedding work 时，`load_model=True` 仍请求一次模型；false 时不请求。
- 同一 revision 的超限 snapshot 只执行一次大读取；revision 变化后重新评估。

## 修复单元 H：配置文件、数据库通知与安全降级

涉及：非法 UTF-8、硬编码数据库名、未捕获 `sqlite3.Error` 三条评论。

### 边界

- `load_disabled_entries()` 将 `UnicodeDecodeError` 与 JSON 语法错误统一转换为 `CatalogOverrideError`，不泄露原始文件内容。
- `catalog_overrides.set_entry_disabled()` 只负责 override 原子写入，不推断数据库路径、不发缓存通知。
- `KnowledgeService.set_entry_disabled()` 持有真实 database path，并负责一次 `notify_database_changed()` 与已实例化 routing state 刷新。
- `_safe_load_records()` 额外捕获 `sqlite3.Error`；只在自动路由快路径吞掉并返回空快照，管理与诊断路径仍保留错误可见性。

### 验收

- 非 UTF-8 override 返回 `catalog_override_invalid` 而非 500。
- 自定义数据库文件名只失效对应数据库缓存；通知恰好一次。
- 锁定或损坏数据库不阻断普通对话路由。

## 修复单元 I：新安装健康语义

涉及：数据库缺失即 degraded 的评论。

### 边界

- `database_exists=False`、override 不损坏且没有迁移失败证据时，定义为合法空状态：`integrity_ok=True`、entries/chunks 为 0、检索模式 BM25。
- “missing” 是观测字段，不是错误。只有数据库存在但 integrity check 失败、override 无效或迁移失败才 degraded。
- 状态读取不得为了证明健康而创建空数据库。

### 验收

- 全新目录 `/status` 返回 ready/available、0 entries，且磁盘上仍不产生数据库。
- 损坏数据库和损坏 override 仍返回 degraded。

## 修复单元 J：前端请求与轮询状态机

涉及：详情错误、导入错误混淆、轮询竞争、十分钟静默停止四条评论。

### 边界

- `openEntry()` 捕获 API 错误，仅在组件仍存活时显示 `loadFailed`，并保持原 drawer 状态。
- 导入分两阶段：文件读取/JSON 解析失败显示 `invalidPack`；API、网络或服务端拒绝显示 `operationFailed`。
- pending job 从 `Set` 改为以 job ID 为键、记录各自开始时间的 Map；新任务不会继承旧任务的十分钟预算。
- 任意时刻只有一个 in-flight poll。timer 只负责唤醒，poll 的 `finally` 统一安排下一次；并发调用复用当前 promise 或直接返回。
- 十分钟只停止该 job 的自动轮询，显示一次 `importStillProcessing`，不显示失败。后端 job 记录保留，刷新页面/概览仍能看到真实状态。
- 组件卸载清理 timer 和本地跟踪状态，不取消服务端任务。

### 验收

- 请求未完成时添加第二个 job 不会产生第二条轮询链。
- 两个 job 有独立截止时间；超时只提示一次，晚到的 active 仍可通过刷新观察。
- JSON 错误、HTTP 错误、active、failed、cancelled 和超时使用不同语义。

## 修复单元 K：确定性分块与资源生命周期

涉及：短尾分块、route owner 清理、SQLite 连接关闭、异步测试未让出事件循环四条评论。

### 分块边界

长无断句正文采用均衡滑窗：先计算满足 `MAX_CHARS` 的最少窗口数，再在窗口间保持不超过 `OVERLAP_CHARS` 的重叠，使末窗不会只携带极少新字符。必须满足全文无丢失、顺序不变、窗口不超长、chunk 数不增加。

该算法会改变 chunk ID。由于 protocol v1 尚在本 PR 中首发，修复在合并前纳入 v1 基线，并同步更新确定性测试与预构建制品测试；若发现仓库外已经发布 v1 制品，则停止实施此项并改为提升 `CHUNKER_VERSION`，不能静默破坏已发布制品。

### 生命周期边界

- 新增请求级 route-owner 清理 helper；正常 turn end 消费 owner，终止且不重试的 discard 路径显式丢弃 owner。重试路径保留同一 request owner。
- 只读 SQLite helper 改为 `contextmanager`，在 `finally` 调用 `connection.close()`；调用方继续使用 `with`，但语义变为真正关闭。
- 调度后台任务的测试在断言前至少 `await asyncio.sleep(0)`，保证被测任务获得一次运行机会。

### 验收

- 1,201 字符无断句文本不会生成“1 个新字符 + 120 重叠字符”的尾块，且逐字符可重建原文。
- terminal discard 后 owner map 无残留；retry 时仍保留，正常结束只消费一次。
- 脚本正常返回和异常退出均关闭连接。
- 测试在后台 planner 真被调度后再断言。

## 实施顺序与提交边界

1. 文档提交：本文及设计索引。
2. 一致性与安全提交：A、C、D。
3. 事件循环与检索提交：B、E、F。
4. 索引与配置提交：G、H、I。
5. 前端与生命周期提交：J、K。
6. 回归修正提交：仅包含测试发现的必要修正。

每个实现提交都必须能由对应定向测试独立说明。全部实现完成后统一推送，并且只解决已经由代码和测试覆盖的审查线程；未实施或无法证明的线程继续保持 open。

## 评论覆盖矩阵

| 修复单元 | 评论主题数 | 覆盖主题 |
| --- | ---: | --- |
| A | 1 | 激活后状态分裂 |
| B | 2 | Router 初始化阻塞、pack job SQLite 阻塞 |
| C | 2 | Task 强引用、订阅无限并发 |
| D | 1 | Bridge 无界请求体 |
| E | 1 | 禁用项截断有效候选 |
| F | 2 | 自动会话逐包查询、迁移重复全表扫描 |
| G | 2 | 预构建模型不预热、超限 snapshot 重复加载 |
| H | 3 | 非 UTF-8、错误数据库通知、SQLite 路由降级 |
| I | 1 | 空安装误报 degraded |
| J | 4 | 详情错误、导入错误、轮询竞争、轮询超时 |
| K | 4 | 短尾分块、owner 泄漏、连接关闭、异步测试时序 |
| 合计 | 23 | 当前全部未解决评论 |
