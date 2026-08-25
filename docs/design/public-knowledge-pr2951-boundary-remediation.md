# PR #2951 公共知识边界收敛设计

> 状态：实施记录与待实施补充。第一、二轮已经实施；第三轮方案基于提交 `2381e79b8` 的全部未解决线程（含 outdated）和 review body 中的 outside-diff 评论整理，尚未实施的项目必须以代码、测试和 CI 通过为完成依据。评论数量是对应审查轮次的历史快照，不代表当前未解决线程数量。

## 目标与非目标

第一轮不逐条堆叠补丁，而是把评论收敛为 11 个拥有明确责任边界的修复单元。目标是：

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
- 通用 `/market/knowledge/{path}` 是本机管理面，只允许 loopback client、loopback Host 及同端口本地 Origin。远端 Market 即使通过一次性码换得共享 bridge token，也只能使用独立注册、明确允许的订阅路由，不能进入管理 catch-all。
- Main Server 的 `/api/public-knowledge/subscriptions/apply` 在 FastAPI multipart 解析之前执行精确路径 ASGI 守门：先拒绝超限 `Content-Length`，再把实际请求流写入至多 1 MiB 内存后转磁盘的 bounded spool；只有实际总量不超限才向下游重放。路由内的单制品限制继续作为第二层校验。

### 验收

- 有、无、伪造 `Content-Length` 三种情况下都不能超过上限。
- 超限请求不调用下游 client；边界值请求原样转发。
- 伪造较小或省略 `Content-Length` 的 multipart 仍按实际字节返回 413，且 FastAPI 不开始解析文件。
- 远端 Origin 携带有效配对 token 调用 `packs/remove` 等管理路径仍返回 403；本机同源管理调用和专用远端订阅调用保持可用。

## 修复单元 E：检索过滤正确性

涉及：禁用条目在候选截断后过滤的评论。

### 边界

- 在发出 FTS/LIKE 查询或执行向量 top-K 前读取 disabled 集合。
- 词法检索可按禁用条目数扩大候选窗口，因为一条词法候选对应一条 entry；向量检索不能使用这一近似，因为一个 entry 可拥有多个 chunk。向量路径必须先把 `(source_tag, title)` 映射为 entry rowid，从 eligible chunk mask 中排除全部禁用 rowid，再执行 `argpartition`。
- 排序、合并和最终 `limit` 仍只作用于启用条目；`include_disabled=True` 保持原行为和原预算。
- override 文件无效继续抛出领域错误，由管理 API 显示可诊断状态；自动会话路径按既有安全降级返回空结果。

### 验收

- 前 12 个强匹配均禁用、第 13 个启用时仍能返回第 13 个。
- 单个禁用 entry 拥有 65 个高分 chunk、启用 entry 位于原 top-64 之外时，仍返回启用 entry。
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

- `index_embedding_batch(load_model=True)` 的 `load_model` 同时表示“确保查询模型可用”。因此模型请求必须发生在 `no_work` 提前返回之前；加载必须经 `_KnowledgeInferenceCoordinator.ensure_loaded()` 串行化，并保留 `inference_busy`、`model_load_timeout`、`embedding_unavailable` 等稳定状态。`load_model=False` 绝不隐式加载模型。
- 当前模型发生变化时，`local` 与 `prebuilt_only` 两种策略下的旧模型 ready vectors 必须在同一 store transaction 中标为 stale；不兼容且不可搜索的 prebuilt cache 不得继续占用全局 ready-vector 预算。
- snapshot 缓存支持 `ready` 与 `rejected` 两种记录。拒绝记录至少绑定数据库 identity、chunk revision、模型 ID 和拒绝原因。
- 命中 rejected 记录时立即抛出相同的 `MemoryError`，不再加载向量行；数据库 identity/revision 或模型变化后自然失效并重试。
- 缓存只保存原因和键，不保存超限矩阵。

### 验收

- 只有预构建 ready chunks、没有本地 embedding work 时，`load_model=True` 仍请求一次模型；false 时不请求。
- 模型加载与 query/background inference 互斥；协调器繁忙或超时不会启动第二个 native inference。
- 旧模型的 local/prebuilt ready chunks 都从 ready 预算释放，当前模型向量不受影响。
- 同一 revision 的超限 snapshot 只执行一次大读取；revision 变化后重新评估。

## 修复单元 H：配置文件、数据库通知与安全降级

涉及：非法 UTF-8、硬编码数据库名、未捕获 `sqlite3.Error` 三条评论。

### 边界

- `load_disabled_entries()` 将 `UnicodeDecodeError` 与 JSON 语法错误统一转换为 `CatalogOverrideError`，不泄露原始文件内容。
- `catalog_overrides.set_entry_disabled()` 只负责 override 原子写入，不推断数据库路径、不发缓存通知。
- `KnowledgeService.set_entry_disabled()` 持有真实 database path，并负责一次 `notify_database_changed()` 与已实例化 routing state 刷新。
- `_safe_load_records()` 额外捕获 `sqlite3.Error`；只在自动路由快路径吞掉并返回空快照，管理与诊断路径仍保留错误可见性。
- `KnowledgeRetriever.search(include_disabled=False)` 捕获 `CatalogOverrideError` 并返回空候选，保证自动会话失败关闭；显式管理读取和 status 仍显示 `catalog_override_invalid`。`include_disabled=True` 不依赖 override，可用于诊断和恢复。

### 验收

- 非 UTF-8 override 返回 `catalog_override_invalid` 而非 500。
- 自定义数据库文件名只失效对应数据库缓存；通知恰好一次。
- 锁定或损坏数据库不阻断普通对话路由。

## 修复单元 I：新安装健康语义

涉及：数据库缺失即 degraded 的评论。

### 边界

- `database_exists=False`、override 不损坏且没有迁移失败证据时，定义为合法空状态：`integrity_ok=True`、entries/chunks 为 0、检索模式 BM25。
- “missing” 是观测字段，不是错误。`packs.json` 同样使用 `missing | ready | invalid` 三态：缺失表示尚未安装社区包，合法健康；存在但无法读取、JSON/结构/版本非法则必须 degraded，不能由 `list_installed_packs()` 的空列表降级掩盖。
- 只有数据库存在但 integrity check 失败、override 无效、pack registry 无效或迁移失败才 degraded。
- 状态读取不得为了证明健康而创建空数据库。

### 验收

- 全新目录 `/status` 返回 ready/available、0 entries，且磁盘上仍不产生数据库。
- 损坏数据库和损坏 override 仍返回 degraded。
- 损坏 `packs.json` 时 `pack_registry_state=invalid`、`integrity_ok=False`；缺失时为 `missing` 且保持健康。

## 修复单元 J：前端请求与轮询状态机

涉及：详情错误、导入错误混淆、轮询竞争、十分钟静默停止四条评论。

### 边界

- `openEntry()` 捕获 API 错误，仅在组件仍存活时显示 `loadFailed`，并保持原 drawer 状态。
- 导入分两阶段：文件读取/JSON 解析失败显示 `invalidPack`；API、网络或服务端拒绝显示 `operationFailed`。
- pending job 从 `Set` 改为以 job ID 为键、记录各自开始时间的 Map；新任务不会继承旧任务的十分钟预算。
- 任意时刻只有一个 in-flight poll。timer 只负责唤醒，poll 的 `finally` 统一安排下一次；并发调用复用当前 promise 或直接返回。
- 十分钟只停止该 job 的自动轮询，显示一次 `importStillProcessing`，不显示失败。后端 job 记录保留，刷新页面/概览仍能看到真实状态。
- 组件卸载清理 timer 和本地跟踪状态，不取消服务端任务。
- entry detail 使用独立 latest-request gate；只有最新请求可更新 drawer 或显示错误，组件卸载会使在途请求失效。
- overview refresh 链只允许“当前 promise 自己”在 `finally` 中清空全局引用，旧链结束不得把已排队的新链误标为空闲。
- job 截止时间检查位于 `packJobs()` 成功/失败分支之外；持续网络失败只影响退避次数，不能绕过十分钟本地等待上限。

### 验收

- 请求未完成时添加第二个 job 不会产生第二条轮询链。
- 两个 job 有独立截止时间；超时只提示一次，晚到的 active 仍可通过刷新观察。
- JSON 错误、HTTP 错误、active、failed、cancelled 和超时使用不同语义。
- 先点 A 再点 B 且 A 后返回时，drawer 保持 B；A 的晚到错误也不弹 toast。
- A refresh 结束时 B 已排队，则 C 仍排在 B 后；不能与 B 并行。
- `packJobs()` 持续拒绝时，job 到期后删除本地跟踪、只提示一次 `importStillProcessing`，且不再调度轮询。

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

## 第一轮实施顺序与提交边界

1. 文档提交：本文及设计索引。
2. 一致性与安全提交：A、C、D。
3. 事件循环与检索提交：B、E、F。
4. 索引与配置提交：G、H、I。
5. 前端与生命周期提交：J、K。
6. 回归修正提交：仅包含测试发现的必要修正。

第一轮已由 `5f4c87fb9`、`4493d4db5`、`9868b2243`、`7a23b65ef`、`d9b9c02ae` 实施。每个实现提交均由对应定向测试说明；只有代码和测试已经覆盖的审查线程才可解决。

## 第一轮评论覆盖矩阵

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
| 合计 | 23 | 第一轮审查时的全部未解决评论（历史快照） |

## 第二轮复审：13 项边界补充

第二轮包含 10 个 GitHub 行级未解决线程和 CodeRabbit review body 中 3 个 outside-diff-range 建议。Outside 类型不是 GitHub review thread，不能单独标记 resolved，但其技术内容仍按同一标准验证和实施。Greptile 的文件数量限制、CodeRabbit walkthrough/risk/Autofix 摘要不计为独立代码问题。

### 优先级与实际严重度

| # | 优先级 | 问题 | 复核结论 | 解决边界 |
| ---: | :---: | --- | --- | --- |
| 1 | P2 | 旧模型 prebuilt ready vectors 继续占预算 | 成立 | `local` 与 `prebuilt_only` 的不兼容模型向量同事务 stale |
| 2 | P2 | 禁用项在 semantic top-K 后过滤 | 成立 | disabled key 先映射 rowid，再从 chunk eligible mask 排除 |
| 3 | P2 | 非法 `packs.json` 被显示成空安装且健康 | 成立 | registry 明确 `missing/ready/invalid`，invalid 参与 integrity |
| 4 | P1 | 远端 Market 可借共享 token 进入管理 catch-all | 成立，影响最高 | 通用管理 bridge 强制本地同源，远端只保留专用订阅路由 |
| 5 | P2 | 删除已安装包后 staged replacement 可再次激活 | 成立 | pack-specific lock 线性化 stage/activate/remove，remove 同锁取消非终态 job |
| 6 | P2 | pack 清洗、分块预检和索引校验阻塞事件循环 | 成立 | canonicalize/parse/validate/preflight/prebuilt validation 整体 `to_thread` |
| 7 | P3 | entry detail 晚到响应覆盖新选择 | 成立，原评论 P2 偏高 | 独立 latest-request gate 同时保护成功与错误分支 |
| 8 | P3 | 设计文档仍写“实施中/当前 23 条未解决” | 成立 | 标为已实施，评论数量改为历史轮次快照 |
| 9 | P3 | `packJobs()` 持续失败时 job 永不超时 | 成立，原 Major 偏高 | 截止检查移到请求成功/失败之外 |
| 10 | P2 | 非法 catalog override 可中断自动检索 | 成立 | 自动 search 返回空候选，显式管理诊断保留错误 |
| 11 | P2 | 后台预热绕过 inference coordinator（outside） | 成立；评论所写文件名有误，实际在 `vector_index.py` | 经 `ensure_loaded` 串行并传播 busy/timeout/unavailable |
| 12 | P3 | 旧 overview promise 的 finally 清空新链（outside） | 成立 | promise identity guard，仅当前链可清引用 |
| 13 | P2 | Main Server multipart 在 parser 前无总量上限（outside） | 成立 | 精确路径 ASGI 预读、实际字节计数、bounded spool、验证后重放 |

这 13 项技术核心均成立，没有应当忽略的误报；但第 7、9 项属于局部 UI 生命周期，不应上调为会破坏数据或安全边界的高优先级。第 11 项建议的目标正确，但原评论把实现位置误写成 `knowledge/pack_jobs.py`。

### 并发与提交点设计

同一 `pack_id` 的三个写动作共享一个由 pack ID 哈希得到的进程内 RLock：

1. `stage_pack` 在锁内复核 pending/capacity 并原子创建 job；
2. `_activate_job` 在锁内再次读取 state，若已 cancelled 则不提交，否则以 `install_pack()` 为提交点；
3. `remove_pack` 在同一锁内先取消该 pack 全部非终态 job，再删除在线来源。

因此并发顺序只有两种合法结果：remove 先线性化时，旧 job 被取消且不能复活；activate 先线性化时，remove 随后删除刚提交的版本。无论调度顺序如何，remove 返回后都不会被调用前已存在的 staged replacement 重新安装。state-file lock 仍只保护单 job journal，registry lock 仍只保护 registry/database 原子更新；各锁职责不混用。

### 请求与资源预算设计

订阅总量上限是 `MAX_PACK_BYTES + MAX_PREBUILT_MANIFEST_BYTES + MAX_PREBUILT_VECTOR_BYTES + 256 KiB` multipart 开销。ASGI 守门针对精确路径工作：

1. 声明长度超限时不读取正文，直接 413；
2. 声明长度缺失、非法或偏小时，逐 ASGI message 累加实际字节；
3. 前 1 MiB 保存在内存，之后转临时文件，累计一旦超限即关闭 spool 并 413；
4. 通过后按 64 KiB 重放给 FastAPI parser，最终由各 `UploadFile` 的单制品限制再次校验。

这一区分“总 envelope 预算”和“单 artifact 预算”，既阻止 parser 前无界临时磁盘写入，也不把合法的大向量文件全部缓存在内存。

### 失败语义

- `pack_registry_state=missing` 和 `database_exists=False` 是合法空安装；`invalid` 才降级。
- 自动检索无法信任 override 时返回空，不猜测哪些条目可用；管理 status/写接口继续暴露可修复错误。
- inference coordinator 的 `inference_busy`、`model_load_timeout`、`embedding_unavailable` 原样成为 batch state；成功预热后若无索引工作仍返回 `no_work`。
- UI 十分钟到期表示停止自动等待，不把服务端 job 改成 failed；网络错误使用指数退避，但不能延长每个 job 的独立 deadline。

### 第二轮实现提交与验收

| 提交 | 覆盖项 | 关键验收 |
| --- | --- | --- |
| `62e029dd0` | #4、#13 | 远端管理调用 403；声明/实际超限 413；合法 multipart 字节级重放一致 |
| `28b10adc0` | #1、#2、#10、#11 | local/prebuilt 同步 stale；disabled 65 chunks 不挤出启用项；invalid override 自动返回空；预热经 coordinator |
| `9d4bca59b` | #3、#5、#6 | registry invalid 降级；remove 后 replacement 不激活；pack/prebuilt 校验在线程池执行 |
| `a5d1def7d` | #7、#9、#12 | late detail/旧 finally 不再改写新状态；API 连续失败仍按 deadline 停止 |
| 文档提交 | #8 | 本文和设计索引只把已实施内容列入 implemented records |

第二轮定向 Python 测试必须使用 `uv run pytest`；前端至少通过 `vue-tsc --build` 与 i18n 完整性检查。最终回归通过后，10 个行级线程可以逐项 resolved；3 个 outside 建议只能通过代码更新与 review 回复说明已处理。

## 第三轮复审：剩余边界的待实施设计

本轮以 `2381e79b8` 为代码快照，复核 GitHub 上全部 unresolved 行级线程（包括已经 outdated 但仍未手动解决的线程）及 review body 中的 outside-diff 评论。按“当前代码是否已经覆盖评论所述失败路径”重新归并后，结果为：

- 6 项原问题已经由当前代码解决；
- 1 项只完成了外围耗时校验的线程化，JSON 解码仍在事件循环中，属于半修复；
- 7 项问题在当前代码中仍成立；
- 另有 2 个不对应新 review thread、但会阻断 PR 合并的 Windows CI 回归。

这里的“已解决”只表示原评论描述的准确失败路径已经消失，不表示相邻实现没有继续优化空间。第三轮实现不得为了顺手优化而扩大 PR；下面明确列为“后续增强”的内容不作为本轮线程关闭条件。

### 复核分类

| 类别 | 问题 | 当前结论 | 第三轮动作 |
| --- | --- | --- | --- |
| 已解决 | 不兼容的 `prebuilt_only` 向量继续占 ready 预算 | 代码已同时 stale `local` 与 `prebuilt_only` | 增加精确策略回归测试后关闭线程 |
| 已解决 | disabled chunk 在 semantic top-K 后才过滤 | eligible mask 已在截断前排除 disabled rowid | 保留现有 65-chunk 测试；条目多样性另列后续增强 |
| 已解决 | 非法 `packs.json` 被当作健康空安装 | status 已暴露 `pack_registry_state=invalid` 并降级 integrity | 关闭线程 |
| 已解决 | 远端 Origin 可进入本地管理 bridge | catch-all 已先执行本地同源校验 | 关闭线程 |
| 已解决 | remove 返回后 staged replacement 再激活 | stage、activate、remove 已共享 pack lock | 增加真实并发时序测试后关闭线程 |
| 已解决 | entry detail 晚响应覆盖新选择 | latest-request gate 已同时保护成功、失败及卸载 | 关闭线程 |
| 半修复 | 本地 pack 导入在事件循环执行重 CPU 工作 | pack/prebuilt 校验已进线程；最大约 10 MiB JSON 仍同步解码 | 修复单元 L |
| 仍成立 | path-specific 413 文案误称“全局上限” | 稳定错误码和上限值正确，仅文案错误 | 修复单元 M |
| 仍成立 | unsubscribe 信任调用方 `pack_id` | 可误删本地导入包或其他订阅包 | 修复单元 N |
| 仍成立 | unsubscribe 不取消活动订阅 worker | 下载、安装和删除可并发交错 | 修复单元 O |
| 仍成立 | 损坏的 staged `state.json` 被静默忽略 | 作业、容量和同包冲突均会消失 | 修复单元 P |
| 仍成立 | takeover completion 泄漏 route owner | takeover 分支未消费 `_text_route_owners` | 修复单元 Q |
| 仍成立 | 未来数据库 Schema 被当前版本原地降级 | 版本检查发生在 DDL/修复写入之后，且最终覆盖为 7 | 修复单元 R |
| 仍成立 | 拉丁词直接匹配没有词边界 | `java` 会命中 `javascript` | 修复单元 S |
| CI 阻断 | 3 个 unit tests 构造了不完整 manager | 测试夹具缺少生产构造器已初始化的 owner map | 修复单元 T |
| CI 阻断 | Study Companion 纯注册导入加载 NumPy | route 聚合导入把 `knowledge.vector_index` 带入进程 | 修复单元 T |

### 第三轮新增不变量

8. 删除知识包的授权身份必须来自本地持久化订阅元数据或可信 Market descriptor；请求中的 `pack_id` 只能做一致性校验，不能作为删除授权。
9. 持久化作业一旦创建目录，就必须处于“可解析”或“显式隔离”状态；损坏、缺失或暂时不可读的状态不得从列表、容量和同包互斥中消失。
10. 高于当前 `SCHEMA_VERSION` 的数据库必须在任何 DDL、DML、journal mode 切换或修复动作之前被拒绝，旧程序不得尝试解释或降级新格式。
11. 取消内存 worker 后必须等待其真正结束，并把任务写成可观察的终态；取消期间不得允许同一 `package_id` 启动新订阅。
12. 拉丁词的自动直接匹配按拉丁字母/数字边界判断；CJK 相邻字符不是拉丁边界阻挡，因此 `Java开发` 可以命中 `Java`，`JavaScript` 不可以。

## 修复单元 L：有界 JSON 的事件循环边界

涉及：`main_routers/public_knowledge_router.py::_bounded_json_payload()` 的半修复评论。

### 边界

流式读取和字节累计继续由事件循环协调；UTF-8 解码、`json.loads()` 和根对象判定合并为纯同步 helper `_decode_json_object(raw: bytes | bytearray) -> dict`，统一通过 `await asyncio.to_thread(...)` 执行。把已累计的 `bytearray` 原样传给 `to_thread`，不能在参数求值时先调用 `bytes(raw)`，否则大对象复制仍发生在事件循环。helper 不访问 request、service 或全局状态。

返回契约保持 `(payload, too_large)`：只有实际或声明体积超限时 `too_large=True`；非法 UTF-8、非法 JSON 或非对象根仍返回 `({}, False)`，不借这次修复改变现有 API 错误语义。

### 验收

- 在线程标识测试中，`_decode_json_object` 不运行在事件循环线程；
- 超限输入在进入 decoder 前返回，不能分配第二份 JSON 字符串；
- 合法对象、数组根、非法 UTF-8、非法 JSON 和恰好等于上限的输入保持原响应；
- 解码接近上限的 JSON 时，一个并行 event-loop tick 能继续运行。

## 修复单元 M：请求体上限的稳定错误语义

涉及：`utils/asgi_body_limit.py::_reject()` 的 path-specific 文案评论。

### 边界

同一个 `_reject()` 同时服务全局上限和精确路径上限，错误文本不得声称是哪一类配置触发。统一改为“请求体超过允许的体积上限。”；`payload_too_large`、`knowledge_request_too_large` 和响应中的实际 `max_bytes` 保持不变。

这是后端机器可读错误契约，不新增前端文案，也不引入 i18n key。前端应继续依据 `error_code` 映射本地化提示，不能解析中文 `error`。

### 验收

- 全局 JSON 上限返回 `payload_too_large`、全局 `max_bytes` 和中性文案；
- 订阅 multipart 精确路径返回 `knowledge_request_too_large`、路径专用 `max_bytes` 和同一中性文案；
- 声明长度超限与实际流式累计超限的 payload 完全一致。

## 修复单元 N：订阅删除的可信所有权

涉及：`plugin/server/routes/knowledge_market.py::unsubscribe_knowledge_package()` 信任调用方 `pack_id` 的 P1 评论。

### 身份模型

为订阅元数据增加 `provider_package_id`。由于现有订阅 hand-off 是字符串字典，该值使用无前导零的正十进制字符串；新安装的 `plugin-market` 订阅必须写入该字段。`provider`、`provider_package_id` 和 `remote_id` 共同构成不可变提供方身份，`version` 与 `channel` 是可更新版本信息。

兼容规则如下：

- `validate_subscription()` 接受旧记录缺少 `provider_package_id`，但 Plugin Market 发起的新 apply 必须提供；
- 已有非空 `provider_package_id` 不得在 replacement 中改变；
- 旧记录从空值升级到非空值，只能发生在当前请求已经通过可信 descriptor 校验时；
- 本地导入包没有 subscription 元数据，永远不具备 Market unsubscribe 资格。

`provider_package_id` 是现有 subscription 子对象的向后兼容可选字段，`PACK_REGISTRY_SCHEMA_VERSION` 保持 4，`SUBSCRIPTION_PROTOCOL_VERSION` 保持 1：旧 registry 可以缺少它，新 Main Server 能读取旧记录；但新 Plugin Market apply 必须携带它。读取旧 registry 时不原地重写，等可信 replacement 时再持久化，避免一次列表操作产生写入。

### 所有权解析

新增 `_resolve_owned_subscription(package_id, claimed_pack_id)`，返回服务端确认的 `pack_id`，不直接删除：

1. 从 Main Server 的已安装 pack 列表中筛选 `subscription.provider == "plugin-market"` 且 `provider_package_id` 等于请求 `package_id` 的记录；
2. 找到唯一记录后，以该记录的真实 pack ID 为删除目标；调用方 `claimed_pack_id` 不一致时返回 `subscription_identity_mismatch`，绝不改用调用方值；
3. 没有新字段匹配时，只允许检查 caller 指向的旧订阅候选。候选必须是 `plugin-market` 订阅，并具有 version、channel 和 remote_id；随后按请求 `package_id` 获取可信 descriptor，且 descriptor 的 `pack_id`、`remote_id`、version、channel 全部一致才授权；
4. 旧记录无法联网验证、descriptor 不一致或元数据不足时返回 `subscription_ownership_unverifiable`，失败关闭；
5. 没有对应订阅时返回 `subscription_not_found`。本地包即使恰好同名也走该分支。

Main Server 删除接口只接收解析后的真实 pack ID。Market 上报使用原 `package_id`，但上报失败仍是 best-effort，不回滚已经完成的本地删除。

### 验收

- 用某个本地导入包的 pack ID 调用 unsubscribe 不会删除它；
- `package_id=A, pack_id=B` 不能删除 B，也不能删除 A 对应包；
- 新格式订阅在离线状态下仍能凭持久化身份安全删除；
- 旧格式订阅在线且 descriptor 完全一致时可删除，离线或不一致时失败关闭；
- replacement 可为旧记录补入 provider package ID，但不能改变既有非空值；
- 重复或冲突的 provider package ID 被诊断为 registry identity error，不任选第一条删除。

## 修复单元 O：unsubscribe 与活动任务的线性化取消

涉及：unsubscribe 忽略 `_active_package_tasks` / `_task_workers` 的 P2 评论，并与修复单元 N 共用可信身份。

### 并发顺序

新增 `_unsubscribing_package_ids: set[int]`。unsubscribe 在第一次 `await` 前完成冲突检查并登记 package ID；subscribe 也必须先检查该集合，命中时返回 409 `knowledge_subscription_conflict`。单事件循环内“检查并登记”之间没有让出点，因此不需要额外 asyncio lock。

登记后的顺序固定为：

1. 读取当前活动 task/worker 快照；
2. 若 worker 存在，调用 `cancel()` 并 `await asyncio.gather(worker, return_exceptions=True)`，确认下载、校验或等待 job 的协程已经退出；
3. cancellation handler 或 done callback 把任务写成 `status=stage="cancelled"`，设置 `completed_at`、`error_code="cancelled_by_unsubscribe"`，再清理 worker 和 active-package 映射；
4. 解析可信 pack 身份。worker 已经拿到 descriptor 时，必须在下一次 await 前把 `resolved_pack_id` 与 `resolved_remote_id` 写入 task，因此取消方可以直接使用；尚未解析 descriptor 的 worker 不可能已经提交 durable job，可按修复单元 N 的旧记录/可信 descriptor 流程继续；
5. 调用 Main Server 的 cancel-and-remove 语义：同一个 pack lock 内先取消该 pack 的全部非终态 durable jobs，再删除在线包；只取消到 staged job、尚无在线安装时也返回成功。结果显式返回 `removed_pack`、`removed_entries` 与 `cancelled_jobs`；只有 `removed_pack=False` 且 `cancelled_jobs=0` 才是 not found，不能把已删除的零条目空包误判为不存在；
6. 最后执行 Market best-effort 上报，并在 `finally` 清除 `_unsubscribing_package_ids`。

`asyncio.CancelledError` 必须单独处理，不能落入当前通用 Exception 分支并把用户取消误记成 internal failure。done callback 仍负责消费非取消异常，但不得覆盖已经写入的 cancelled 终态。

### 验收

- resolving、downloading、verifying 阶段取消后 worker 结束，task 保留 cancelled 终态；
- Main Server 已创建 durable job、Plugin worker 正在等待时，unsubscribe 能取消 job，且该包之后不会激活；
- 已激活后 unsubscribe 删除在线包；
- unsubscribe 登记后、删除返回前，同 package subscribe 始终返回 409；
- 两个并发 unsubscribe 只有一个执行删除，另一个在 reservation 存续时稳定返回 409 conflict；
- 取消到 staged-only 状态返回成功而不是表面 not found；
- `finally` 在网络、身份解析和 Main Server 失败时都释放 reservation，允许用户重试。

## 修复单元 P：损坏作业日志的隔离与容量守恒

涉及：`knowledge/pack_jobs.py::_read_json()` 把所有读取失败折叠为空字典的 P2 评论。

### 持久化结构

新 job 不直接在最终目录中边写边公开。持有 jobs-root mutation lock 时，先创建 `.creating-<uuid>` 临时目录，原子写入不可变的 `identity.json`，再写 pack/index artifacts，最后写初始 `state.json`；全部成功后在同一文件系统内把目录原子重命名为最终 `<job_id>`。这样并发列表不会把正常创建中的 job 误判成 missing state。identity 至少包含：`job_id`、`pack_id`、`created_at`、`entries_total`、`chunks_total`、`content_bytes`。processor 不修改 identity；mutable state 只记录阶段、进度、重试和结果。

JSON 读取改为判别结果而非空字典：`valid | missing | invalid | unreadable`。对共享/杀毒软件造成的临时 `OSError` 只做小次数、短间隔同步重试；仍失败后归类 unreadable，不能假装目录不存在。

### 隔离语义

- state 无效但 identity 有效：列表暴露 `state="degraded"`、`reason="invalid_job_state"` 及 identity 预算；容量统计和同 pack 互斥继续计入；processor 跳过，不自动清理；
- state 缺失但 identity 有效：同样 degraded，reason 为 `missing_job_state`；
- identity 与 state 都无法建立可信身份：暴露 orphan 诊断，并全局拒绝新 staging，错误码 `knowledge_job_registry_invalid`。此时无法安全判断它属于哪个 pack 或占用多少容量，不能按零处理；
- 启动时发现遗留 `.creating-*` 目录也按 orphan 暴露并失败关闭；它可能来自进程崩溃，不能在不知道另一个进程是否仍写入时自动删除；
- terminal auto-cleanup 不处理 degraded/orphan。管理端提供显式 discard 动作，按经过目录穿越校验的 job ID 删除隔离目录；不在后台自动修复或删除证据；
- 本轮不实现从损坏 state 自动恢复执行。若以后增加 repair，必须重新验证 pack artifact、容量和订阅身份后生成新 state。

为兼容旧 job：没有 identity 但 state 有效时，先按 state 正常展示和处理；只有在持有 job state lock、字段完整且 artifacts 可验证时才可补写 identity。不能仅凭目录名推断 pack ID。

### 验收

- `state.json` 为截断 JSON、数组、缺失或持续 unreadable 时，job 均出现在诊断中而不是消失；
- identity 有效的 degraded job 继续占 entries/chunks/content 容量，并阻止同 pack 重复 staging；
- identity 也无效时，任意新 staging 失败关闭，不创建第二个 job 目录；
- 列表与 staging 并发时，完整 job 只在原子 rename 后出现，不产生瞬时 degraded 记录；崩溃遗留的 `.creating-*` 明确成为 orphan；
- processor 重启不会执行或自动删除隔离 job；
- 显式 discard 只能删除选定的 `.staging/<job_id>`，成功后容量与冲突解除；
- 旧格式有效 job 可继续完成，兼容补写失败不破坏原 state。

## 修复单元 Q：takeover 完成路径的 route-owner 释放

涉及：`main_logic/core/turn.py::handle_response_complete()` takeover 分支的 P2 评论。

### 边界

在 takeover 分支的第一个 `await` 之前捕获 `active_request_id`，同步消费该 request 的 `_text_route_owners`，并清空只属于旧轮的 pending meta/text。`_active_text_request_id` 使用 compare-and-clear：仅当共享字段仍等于快照时置空。随后才 `await _clear_tts_pipeline()`。

这样既不发送旧轮 `turn_end`，也不在 TTS cleanup 的让出窗口删除新请求刚登记的 owner 或 request ID。不要用 `getattr(..., {})` 掩盖测试夹具缺字段；生产构造器已经保证 owner map 存在，CI 夹具应按修复单元 T 补齐。

### 验收

- takeover completion 后旧 request owner、pending meta 和旧文本均不存在；
- 在 `_clear_tts_pipeline()` await 期间注入新 request，新 owner 与新 active ID 保留；
- takeover 路径不发 `turn_end`，普通完成路径仍只消费一次 owner；
- TTS cleanup 抛错时也不能让旧 owner 永久残留，因此共享状态清理必须发生在 await 前。

## 修复单元 R：数据库 Schema 的向前兼容拒绝

涉及：`knowledge/store.py::_initialize()` 在确认版本前执行迁移和覆盖版本的 P1 评论。

### 版本探测

新增 `KnowledgeSchemaTooNewError(KnowledgeStoreError)` 和只读 `_read_schema_markers(connection)`。连接建立后只允许设置 row factory 与 busy timeout；在下列探测完成前，不得执行 `journal_mode=WAL`、CREATE、ALTER、DELETE、修复 source tag 或 metadata UPDATE：

1. 读取 `PRAGMA user_version`；0 表示旧版本尚未使用该标记；
2. 查询 sqlite schema 判断 metadata 表是否存在；存在时读取唯一 `schema_version` 值；
3. metadata 值存在但不是规范正整数时，抛 `KnowledgeStoreError`，不猜测；
4. 任一非零标记大于 `SCHEMA_VERSION` 时抛 `KnowledgeSchemaTooNewError`；
5. 两个非零标记不一致时失败关闭，不能选择较小值继续；
6. metadata 表或 schema row 缺失、且 user_version 为 0 时视为可迁移 legacy 数据库。

探测通过后才进入现有 migration/repair，并在成功提交时同时写 `metadata.schema_version` 与 `PRAGMA user_version=SCHEMA_VERSION`。当前数据库的 metadata=7、user_version=0 是合法过渡态，会在一次成功初始化后补齐 user_version。初始化失败不写 `_INITIALIZED_DATABASES`；连接由现有 `finally` 关闭。

status/管理 API 捕获 `KnowledgeSchemaTooNewError`，返回 degraded 及稳定诊断 `knowledge_schema_too_new`，包含 `supported_schema_version`，但不把数据库内容或异常堆栈暴露给前端。读、写、status 和后台 indexer 都必须经过同一 guard，不能只保护 mutation。

### 验收

- 构造 metadata 或 user_version 为 8 的数据库，分别调用读取、写入、status 和后台索引入口，均拒绝且错误稳定；
- 拒绝前后数据库文件 identity、schema、表内容、schema_version、user_version 和 journal mode 均不变；
- 非整数/负数 metadata marker 失败关闭；两个非零 marker 不一致失败关闭；
- 只有 metadata marker 的当前 v7 数据库可正常打开并补齐 user_version；
- 无 marker 的真实 legacy fixture 能迁移，空目录的健康语义不回归；
- 失败连接不会污染 initialized cache，替换为受支持数据库后可重新初始化。

## 修复单元 S：自动直接匹配的拉丁词边界

涉及：`knowledge/service.py::_normalized_direct_text()` 删除所有分隔符后执行 substring 的 P2 评论。

### 匹配模型

保留两套规范化，不能用一个“删除所有非字母数字”的字符串同时服务所有语言：

- compact normalization：NFKC + casefold + 仅保留字母数字，用于完整查询等价和含 CJK/非拉丁 term 的既有紧凑子串匹配；
- folded surface：NFKC + casefold + 规范空白，但保留标点，用于纯拉丁/数字 term 的字面查找和边界判断。

term 仅包含拉丁字母、数字、组合音标及允许标点时，嵌入式匹配的左右邻接字符不能是拉丁字母、组合音标或十进制数字。CJK 字符、空白和标点都构成合法边界，因此 `Java开发` 与 `学习Java` 可匹配，`JavaScript`、`myjava2` 不可匹配。不要使用 Python `\b`：Unicode 正则会把汉字也视为 word character，从而错误拒绝 `Java开发`。

含 CJK 的 term 保留 compact 子串路径。混合 term 因自身已有非拉丁区分度，也走 compact 路径。`_is_short_query_embedded_in_term()` 必须复用同一拉丁边界 helper，避免 direct 分支修好后 corpus semantic 辅助分支仍把 `java` 当作 `javascript` 的一部分。

长度规则保持现状：普通嵌入式 direct term 仍至少 4 个字母数字；短 query-in-term 仍只在 corpus + semantic 前提下使用。带语义标点的技术名词（如 `node.js`）按 folded surface 精确标点匹配；`C++`、`C#` 只允许完整查询等价，不因单字母 `c` 触发嵌入匹配。此处不引入可配置分词器。

### 验收

- `java` 不匹配 `javascript`、`myjava2`，匹配 `Java 开发`、`Java开发` 和标点包围的 Java；
- 中文 term 继续按紧凑子串匹配；NFKC、大小写折叠和带音标拉丁字母按同一边界工作；
- `node.js` 不因删除点号而命中 `nodejs`；`C++` 完整查询可命中，但普通 `c` 不可；
- direct 和 corpus short-query 两条路径使用同一组反例；
- 原有 2 字符最低阈值、4 字符嵌入阈值和 semantic score 阈值不改变。

## 修复单元 T：合并阻断的测试夹具与轻量导入

### Unit pytest

3 个失败测试通过 `object.__new__(LLMSessionManager)` 绕过生产构造器，夹具没有 `_text_route_owners`，而生产 `manager.py` 已在构造时初始化该字段。修复测试 `_make_manager()`，显式设为空字典；不得为了不完整测试对象在生产路径添加 defensive `getattr`。同时把 takeover 用例扩展为修复单元 Q 的 owner 清理和并发保留断言。

### Plugin pytest

`study_companion` 注册导入经过 `plugin.server.routes` package 后加载 `knowledge_market`；该模块从聚合层 `knowledge.api` 导入两个轻量订阅符号，聚合层继续加载 service、vector index 和 NumPy。将 `SUBSCRIPTION_PROTOCOL_VERSION` 与 `load_canonical_pack_artifact` 改为直接从 `knowledge.subscriptions` 导入；`MAX_PACK_BYTES` 继续来自 `knowledge.packs`。不为一个导入边界回归重构整个 routes package，也不添加运行时 sys.modules 清理。

### 验收

- 3 个当前失败的 manager unit tests 通过，且新 takeover 竞态测试通过；
- `test_study_plugin_registration_import_does_not_load_numpy` 通过；
- Plugin Market 的订阅 descriptor、canonical artifact 和下载上限行为不变；
- 在 fresh interpreter 中导入 Study Companion 注册模块后，`numpy` 与 `knowledge.vector_index` 均未出现在 `sys.modules`。

## 第三轮兼容、并发与失败矩阵

| 场景 | 可信事实 | 允许结果 | 禁止结果 |
| --- | --- | --- | --- |
| 新格式 Market unsubscribe | registry 的 provider package ID | 取消同包任务并删除解析出的真实 pack | 按 caller pack ID 删除 |
| 旧格式 unsubscribe，Market 离线 | 无法完成所有权证明 | 明确失败、保留数据 | 猜测 remote ID 或 pack ID 后删除 |
| unsubscribe 与 subscribe 并发 | 先登记的 package reservation | 旧任务完全结束后才允许重试 | 删除期间启动新 worker |
| staged state 损坏 | immutable identity 或 orphan 事实 | degraded/quarantine、计入预算、显式清理 | 静默跳过、自动删除、按零计费 |
| 数据库 Schema > 7 | 数据库自身版本标记 | 只读探测后 degraded | WAL/DDL/DML/版本覆盖 |
| takeover cleanup 期间新请求到达 | active request 快照与 owner map | 仅清旧 request | 清掉新 request 状态 |
| 拉丁 term 嵌在更长拉丁 token | 邻接拉丁字符/数字 | 不视为 direct match | `java` 自动命中 `javascript` |
| path-specific body 超限 | error code 与实际 limit | 中性人类文案 | 声称触发全局配置 |

## 第三轮实施顺序、提交边界与关闭条件

1. CI 与低风险边界提交：L、M、T。先恢复可用验证信号，不混入数据迁移。
2. 数据库兼容提交：R。独立提交便于审查“拒绝前零写入”的证据。
3. 作业持久化提交：P。包含旧 job 兼容、隔离展示、容量和显式 discard 的完整闭环。
4. 订阅身份与并发提交：N、O。二者共享身份和取消顺序，不能只实现“取消 worker”而仍信任 caller pack ID。
5. 会话与匹配提交：Q、S。它们互不共享状态，测试文件可清晰归属。
6. 回归修正提交：仅处理上述定向测试与全量 CI 暴露的必要问题，不顺带扩大检索或路由架构。

每个提交先运行对应定向测试，再运行受影响测试组。Python 使用项目 Python 3.11 的 `uv run pytest`；Plugin 测试在独立进程验证 import graph。只有满足以下全部条件后才可回复并 resolve 对应评论：实现已提交、评论所述反例有精确测试、相邻失败语义有至少一个负例、相关 CI 通过。

### 不属于第三轮关闭条件的后续增强

- semantic top-K 已在截断前排除 disabled chunks；“单个启用 entry 的大量 chunks 挤出其他启用 entry”属于结果多样性问题。若产品要求每个 entry 至少一个候选，应在 vector snapshot 中按 entry rowid 取最大分后再做 entry top-K，或渐进扩大 chunk 窗口直到得到足够不同 entry。该变化会影响相关性排序与性能，不与本轮 disabled 正确性评论绑定。
- stage/remove 已有共享 pack lock；本轮只补真实并发回归，不改为数据库级分布式锁。当前桌面单进程模型不需要扩大锁范围。
