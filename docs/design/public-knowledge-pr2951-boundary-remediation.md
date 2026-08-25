# PR #2951 公共知识边界收敛设计

> 状态：持续修复记录。第一至第五轮均已实施。第三轮方案基于提交 `2381e79b8` 的全部未解决线程（含 outdated）和 review body 中的 outside-diff 评论整理，并由 `7b972d227` 至 `f4a9aaf31` 的五个提交完成；第四轮及其 review-body 补充由 `d33a80b25` 至 `6e4a3e131` 的六个实现提交完成；第五轮及其 review-body 补充由 `43c138ce4`、`2a114dd23`、`5557d1760` 与 `4b75b24b4` 完成。评论数量是对应审查轮次的历史快照，不代表当前未解决线程数量；代码、测试和 CI 是最终事实来源。

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

## 第三轮复审：剩余边界的实施设计

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

## 第三轮实施结果

| 提交 | 覆盖单元 | 关键结果 |
| --- | --- | --- |
| `7b972d227` | L、M、T | JSON 解码离开事件循环；413 文案统一；manager 测试夹具完整；知识轻量导入不再加载 NumPy/vector index |
| `425d118b1` | R | 未来 Schema 在 WAL/DDL/DML 前拒绝，status 暴露稳定 degraded 诊断 |
| `d56d2c778` | P | job 原子发布；损坏状态隔离且计入容量；orphan 失败关闭；显式 discard |
| `682f2cd40` | N、O | provider package identity 持久化；unsubscribe reservation、worker 终态、durable job 取消及 Main Server 二次所有权校验 |
| `f4a9aaf31` | Q、S | takeover 同步释放旧 owner 并保护新请求；拉丁词按脚本边界匹配 |

本地合并前回归覆盖知识库、公共知识路由、请求体守门、Plugin Market、Study Companion 轻量导入及核心 takeover 生命周期，共 354 项测试通过。本次改动文件 Ruff 检查通过。全仓 Ruff 仍有一个既存、范围外的 `ASYNC220`（CosyVoice server 在 async 函数中调用 `subprocess.Popen`）以及旧 `noqa` 格式警告，不在本 PR 中顺带修改。GitHub CI 结果仍以对应提交上的远端检查为准。

## 第四轮：迁移、资源与并发边界

第四轮来自第三轮实现后重新收集的 12 个有效未解决线程。两个关于 `package_id` 字符串/整数直接比较和 subscribe/unsubscribe 检查窗口的评论经实际控制流复核不成立：持久化身份比较前显式执行 `str(package_id)`；subscribe 从 reservation 检查到 worker 映射登记之间没有 `await`，单事件循环不能在该同步区间插入 unsubscribe。两条线程已回复依据并关闭，不计入本轮。

### 修复单元 U：legacy 迁移输入必须全部可证实

涉及：旧数据库读取失败被折叠为空集合、旧 `packs.json` 无效时被静默跳过两条 P1 评论。

迁移采用“先验证全部输入、再创建候选结果、最后原子发布”三阶段。任何一个已发现的 legacy 数据库或注册表无法读取，都不能用其余输入生成权威目标库。

- 为迁移增加严格读取入口；`list_active_entries()` 等面向自动检索的安全降级 API 不得用于迁移。严格入口传播 `KnowledgeStoreError`、`sqlite3.Error` 和锁超时，并在读词条、来源策略、向量前执行只读完整性与 Schema 兼容检查。
- legacy 数据库缺失与“路径存在但不是可读 SQLite 数据库”是不同状态。后者中止迁移并保留恢复副本；不得发布空 `knowledge.db`，也不得写入表示迁移完成的目标文件。
- legacy `packs.json` 缺失表示该来源没有注册表，可以继续；路径存在但读取失败、JSON 非对象、Schema 不支持或 `packs` 非对象时中止迁移。复用 pack registry 的严格解析契约，不能在 migration 中维护第二套宽松 parser。
- 全部输入验证成功后才创建 stage。stage 内的数据库、registry 和 override 完成 `quick_check`、计数与引用一致性检查后，才 `os.replace()` 发布。失败只清理未发布 stage，不修改 legacy 输入和既有目标。
- 服务状态将迁移失败映射为稳定的 `knowledge_legacy_migration_failed`，只暴露输入类别和安全原因码，不暴露本地路径、SQL 或原始 JSON。

验收：损坏、锁定和临时不可读数据库分别使迁移失败且不生成目标；缺失 registry 可迁移，存在但损坏/不可读/未来版本 registry 必须失败；修复输入后重试可成功；失败前后恢复副本内容不变。

### 修复单元 V：前端导入体积的第一道防线

涉及：`file.text()` 前未检查 `File.size` 的 P2 评论。

- Plugin Manager 在任何 `await file.text()` 或 `JSON.parse()` 前比较文件字节数与知识包制品上限。空文件仍交给格式错误路径；超限文件不读取、不解析、不发送 Bridge 请求。
- 前端常量明确表示“知识包文件上限”，与后端 `MAX_PACK_BYTES` 保持 10 MiB。增加契约测试防止两端数值漂移；Bridge envelope 的额外预算不混入文件上限。
- 增加 `knowledge.importTooLarge` i18n key，并同步所有现有语言。不能把体积错误伪装成 JSON 格式错误。
- 后端流式限制仍是权威安全边界；客户端检查只改善页面可用性，不能替代 Bridge/Main Server 校验。

验收：上限加一字节的 File 不调用 `text()` 和 API；恰好上限仍进入解析；提示走本地化体积错误；伪造前端仍被后端拒绝。

### 修复单元 W：LIKE 查询必须按字面量解释

涉及：规范化查询中的 `_`、`%` 被 SQLite 当作通配符的 P2 评论。

- 新增单一 `_escape_like_pattern()`，按“先转义 escape 字符，再转义 `%`、`_`”的顺序处理用户片段，再在两端添加 `%` 作为系统控制的 contains 通配符。
- 所有五个 LIKE 分支使用同一参数和显式 `ESCAPE '\\'`；继续参数化查询，不能字符串拼接 SQL。
- FTS 失败后进入 LIKE fallback 时同样使用字面语义。普通字母、CJK、空格和连字符规范化行为不变。

验收：仅含 `_`、`%`、反斜杠及其组合的查询只命中字面包含者；普通 contains 查询不回归；恶意引号不改变 SQL 结构；来源过滤仍生效。

### 修复单元 X：degraded 必须是可终止、可诊断状态

涉及：Market 轮询忽略 degraded job、status 只捕获 future Schema 两条 P2 评论。

- `_wait_for_pack_job()` 将 `degraded` 与 `cancelled/failed` 一样视为终态失败，抛稳定 `job_degraded`；保留服务返回的安全 `reason` 供日志诊断，但不把路径或异常正文传给远端 Market。worker 立即结束并释放四个订阅槽之一，隔离 job 仍只能由显式 discard 删除。
- `KnowledgeService.get_status()` 将数据库兼容探测包在统一错误边界。`KnowledgeSchemaTooNewError` 保持专用 `knowledge_schema_too_new`；其余 `KnowledgeStoreError`/SQLite 打开、锁定、损坏、非法或冲突 marker 返回 `integrity_ok=False`、`schema_state="invalid_or_unavailable"`、`error_code="knowledge_database_unavailable"`。
- 抽取零数据 degraded payload builder，保证不同失败路径都含前端依赖的计数、registry/job 状态和向量预算字段，避免修复 500 时制造响应 shape 分叉。
- status 不尝试修复、迁移或覆盖错误数据库；后台索引仍失败关闭。暂时锁定与持久损坏使用同一外部错误码，详细类别只进入本地日志。

验收：degraded job 一次轮询即结束任务并释放 active 映射；显式 discard 仍可用；损坏、锁定、非法 marker、marker 冲突和未来 Schema 的 status 均返回结构化 degraded，其中未来 Schema 保持专用字段；新安装空目录仍健康。

### 修复单元 Y：provider package ID 的唯一规范形式

涉及：`isdecimal()` 接受 Unicode 十进制数字的 Minor 评论。

- 公共契约为 ASCII 正十进制字符串：`[1-9][0-9]{0,18}`。导出一个规范化/校验 helper，由 subscription 解析、Main Server 删除边界和需要持久化身份的路径共同使用。
- 不使用 `isdecimal()`、`isdigit()` 或先 `int()` 再判断原字符串；全角数字、阿拉伯-印度数字、符号、空白、前导零和 20 位以上值全部拒绝。
- Market 请求模型与 descriptor 的整数上限同步为 19 位最大值。合法整数持久化时只通过 `str(value)` 生成规范 ASCII。
- 既存非 ASCII 值不可能由受支持写路径产生，按损坏身份失败关闭，不做猜测式转换。

验收：`1`、19 位最大值通过；`0`、前导零、20 位、全角和阿拉伯-印度数字在 subscription 与 remove 两处得到相同拒绝；新安装、replacement 和离线 unsubscribe 的合法身份不回归。

### 修复单元 Z：TTS 清理按 speech 所有权执行

涉及：takeover 旧请求在 `_clear_tts_pipeline()` 等待后清空新请求 pending chunks 的 Major 评论。

- `_clear_tts_pipeline()` 接收调用入口在首个 `await` 前捕获的 `expected_speech_id`。清理只拥有该 speech 的 pending text、done 记账、回放状态和可识别响应；不得在等待后无条件清空共享新世代状态。
- `tts_pending_chunks` 已携带 speech ID，清理时过滤旧 ID 而非 `clear()`。等待 worker 处理 interrupt 后，在 `tts_cache_lock` 内比较 `current_speech_id`；若已切换，只删除旧 ID 项并保留新 ID chunks 与新轮 done flags。
- 对不能按 speech ID 区分的 worker interrupt/响应队列，定义一个单调递增 TTS generation。interrupt 记录目标 generation，后续请求入队携带新 generation；迟到的旧响应由 handler 丢弃，不能通过“等待后清空整个响应队列”保护正确性。
- 所有清理调用点必须显式传入旧 speech 快照。确实要关闭整个 worker 的 lifecycle shutdown 使用单独 `clear_all=True`，避免普通 takeover 借用全局销毁语义。

验收：旧 cleanup 的等待窗口内启动新请求，新 `tts_pending_chunks`、done 状态和音频均保留；旧 speech 的 pending/迟到响应被丢弃；连续两次 takeover、worker 未 ready、worker 已 ready 和 shutdown 全清理路径均有测试。

### 修复单元 AA：终态作业历史有界保留

涉及：成功、失败、取消的 staging 目录永久增长的 P2 评论。

- terminal job 保留短期诊断价值，但采用双界限：默认保留 7 天且每类知识根最多保留最近 100 个 terminal 目录。超过任一界限的旧目录可删除；非终态和 degraded/orphan 永不自动删除。
- prune 在持有 jobs-root 跨进程 mutation lock 时执行；先按可信 `updated_at/created_at`、再按目录 mtime 排序。identity/state 无法可信读取的目录已经属于 degraded，不得作为过期 terminal 清理。
- 删除目标必须是 `_jobs_root` 的直接子目录且名称等于已验证 job ID；不跟随 symlink，不接受路径穿越。失败记录日志并留待下轮，不阻断本轮索引。
- `/packs/jobs` 仅返回保留窗口中的 terminal history；当前 active、pending、degraded 始终完整返回。

验收：第 101 个 terminal job 删除最旧项；超过 TTL 的 terminal 被删；新 terminal、非终态、degraded、orphan、symlink 均不被误删；并发 list/process/prune 不产生半删除状态。

### 修复单元 AB：mutation lock 的跨进程线性化

涉及：维护 CLI 与 Main Server 共享路径时 `threading.RLock` 无法互斥的 P2 评论。

- 保留当前按规范化路径共享的进程内 `RLock`，在最外层进入时再获取同路径 sidecar lock file 的 OS advisory exclusive lock；最外层退出时释放。嵌套同线程调用只增加深度，不能对同一文件锁二次阻塞。
- 使用项目已有的 `portalocker` 提供跨平台 advisory exclusive lock（底层分别采用操作系统文件锁）。锁文件位于目标同目录并使用稳定、无用户输入的派生名称；锁文件持久存在是正常状态，不以删除 lock file 表示释放。
- 获取顺序固定为进程内锁后文件锁；多路径操作继续使用既有上层 pack-operation 锁顺序，禁止在持有具体 state/registry 锁后反向获取 pack root 锁。
- 维护 CLI 的所有 mutation 复用同一 helper。只读诊断不取独占锁；会基于读结果写回的 read-modify-write 必须把读取和提交放在同一锁区间。
- 文件锁获取失败或超时应明确终止维护操作，不退化为无锁执行。服务路径继续在工作线程等待，不能阻塞事件循环。

验收：两个独立 Python 3.11 进程对同一路径互斥；不同路径可并行；同线程嵌套不死锁；cancel 与 activate、policy 与 registry 更新的受控竞态最终状态一致；异常退出由 OS 自动释放锁。

### 修复单元 AC：bounded spool 的磁盘 I/O 不占事件循环

涉及：`SpooledTemporaryFile` rollover 后同步读写的 P2 评论。

- 网络 `receive()` 仍在事件循环协调；spool 的 `write`、`seek`、`read`、`close` 全部通过 `asyncio.to_thread` 执行。为避免每个小 chunk 都产生线程切换，可在内存阶段累计到固定 64 KiB 块后批量写，但内存累计仍受总上限控制。
- replay receive 改为 async 读取 helper，一次读取并判定 `more_body`，不使用同步“多读一个字节再 seek 回退”。spool 访问保持单消费者，关闭发生在下游 ASGI app 完成后的 `finally`。
- 超限、disconnect、下游异常和正常完成都必须关闭 spool；413 payload 与现有稳定错误语义不变。

验收：强制 rollover 后 write/read/seek/close 均在线程池线程执行；伪造或缺失长度仍受实际字节限制；边界值可逐字节重放；disconnect、413 和下游异常不泄漏临时文件。

### 修复单元 AD：从完整的有效标签总体随机抽样

涉及：先取 ranked top-100 再随机抽样造成永久偏差的 P2 评论。

- KnowledgeStore 增加按精确 tag 选择启用 entry rowid 的有界随机查询，使用 JSON tag 成员匹配而非文本 search 排名。禁用集合在抽样前排除。
- 不使用 `ORDER BY RANDOM()` 扫描并排序完整大表。先取得符合条件的 rowid 总体或使用 reservoir sampling；当前社区总 entry 上限 20,000，可在工作线程中对 rowid 流做等概率 reservoir，内存保持 O(limit)。随后按选中 rowid 批量加载 entry。
- `CORPORA_SAMPLE_TAGS` 白名单、调用方 limit 1..3 和 material type 路由保持不变。相同 entry 不重复；少于 limit 时返回全部。
- 为可重复测试允许向内部 helper 注入 RNG，但产品路径继续使用进程随机源。

验收：构造 101+ 同标签条目并控制 RNG，原 top-100 外条目可被选中；禁用项永不出现；其他 tag、正文中仅出现标签文字但 tags 不含该值的 entry 不进入总体；1/3 上限和空集合行为不回归。

## 第四轮实施顺序、提交边界与关闭条件

1. 先提交本文和索引，冻结失败语义与测试口径。
2. 数据安全提交：U。迁移两个 P1 必须同一提交闭环，防止只保护数据库却继续丢 registry。
3. 查询与健康提交：W、X、AD。三者共享 KnowledgeStore/Service 读取边界，但测试按问题分组。
4. 身份与前端提交：V、Y。V 必须同步全部 i18n；Y 必须同步 Market 请求上限和 Main Server 校验。
5. 生命周期提交：Z、AA。TTS 只改 speech 所有权，job prune 只处理可信 terminal，不互相耦合。
6. 并发与 I/O 提交：AB、AC。先让 file lock 有独立跨进程测试，再把所有 spool 文件操作移出事件循环。
7. 全量回归后补写“第四轮实施结果”。只有实现提交已推送、精确反例测试通过、相邻失败语义有负例且远端相关 CI 通过，才回复并 resolve 对应线程。

第四轮不改变知识包五字段内容 Schema、不自动修复损坏数据库/作业、不扩大 20,000 chunk 与 10 MiB 文件预算，也不以自动删除 degraded 证据换取容量恢复。

## 第四轮实施结果

| 提交 | 覆盖单元 | 关键结果 |
| --- | --- | --- |
| `d33a80b25` | U | legacy 数据库、来源策略、向量和 registry 全部严格读取；任一已存在输入不可证实时中止且不发布目标 |
| `a6559c36d` | W、X、AD | LIKE 字面转义；degraded 轮询终止；普通数据库错误结构化降级；完整标签总体 reservoir 抽样 |
| `b8790f2bd` | V、Y | 前端在读取前拒绝超过 10 MiB 的文件并补齐八种语言；provider ID 统一为 ASCII 正整数格式 |
| `527b6e935` | Z、AA | TTS 按旧 speech ID 清理；terminal job 按 7 天与 100 条双上限安全裁剪 |
| `50495e40d` | AB、AC | mutation lock 增加可重入跨进程文件锁；spool 的 write/seek/read/close 全部离开事件循环 |
| `6e4a3e131` | AE、AF | spool 所有权移交前的取消/异常路径关闭临时文件；前端 degraded 立即终止轮询；状态默认值与作业状态集合统一来源 |

本地合并前回归覆盖知识库、公共知识路由、请求体守门、Plugin Market、Study Companion 轻量导入及核心 takeover 生命周期，共 377 项测试通过；本轮所有改动文件 Ruff 检查通过。前端 `vue-tsc --build` 与 i18n 完整性检查通过，八种语言均为 732 个键。测试退出后的 telemetry 日志在受限沙盒中仍会报告既存的本机配置目录写入失败，但 pytest 返回码为 0，不影响上述结果。GitHub CI 结果仍以对应提交上的远端检查为准。

## 第四轮审查正文补充

重新检查 review body 后发现两条未生成独立 review thread 的 outside-diff 有效评论。它们不改变 U–AD 的总体方案，但补齐 AC 的所有权异常路径和 X 的前端终态一致性。

### 修复单元 AE：spool 构造方在所有权移交前负责异常清理

- `_spool_bounded_body()` 创建 spool 后、成功返回给调用方前，是临时文件的唯一所有者。`receive()`、异步 `write()` 或最终 `seek()` 抛出任何 `BaseException`（包括取消）时，它必须在线程中关闭 spool 后重新抛出原异常。
- 正常、disconnect 和超限返回表示所有权已移交给 `__call__()`；仍由外层既有分支或 `finally` 关闭，避免双重关闭成为正确性前提。
- close 自身失败不能覆盖原始接收、写入或取消异常。测试分别注入 receive 取消与 write 失败，并断言 close 已执行且原异常保持不变。

### 修复单元 AF：管理界面与后端使用同一作业终态

- 前端导入轮询将 `degraded` 与 `failed/cancelled` 一样从 pending 集合移除，显示已有的操作失败提示并触发概览刷新；不得继续等待十分钟后显示“仍在处理”。
- 后端仍保留 degraded 作业证据，只有显式 discard 才删除；前端这里只停止自动轮询，不自动清理服务端状态。
- 状态统计使用 `TERMINAL_STATES | {DEGRADED_STATE}` 作为“不再 pending”的单一表达，同时保留 degraded 与可自动裁剪 terminal 的生命周期差异。

同一 review body 的三个维护性建议一并按最小范围处理：删除 Schema marker 校验中不可达的空字符串分支；用 helper 共享空 chunk status 字段；用 pack job 常量构造非 pending 状态集合。它们不单独改变外部契约，也不扩大本轮边界。

## 第五轮：持久化恢复与检索精度

第五轮来自提交 `0d727115c` 完成后新增的 7 条 review thread。逐项追踪生产调用链后 6 条成立，混合脚本 routing 一条不成立；其中 semantic entry 去重曾作为第三轮后续增强记录，本轮既然已有可构造的漏召回反例，正式纳入正确性边界。

### 修复单元 AG：作业状态时间字段必须先验证再参与排序

- `_read_job()` 在返回可信 state 前统一规范 `created_at` 与 `updated_at`：仅接受非负整数语义，布尔值、浮点、空值、非数字字符串和负数均把作业隔离为 `degraded`，原因码为 `invalid_job_timestamps`。
- identity 中可信的 `created_at` 仍可用于 degraded 展示；不可把损坏 state 的时间值传给 `int()` 排序或 TTL 裁剪。
- `list_pack_jobs()`、status 和 terminal prune 对任意合法 JSON state 都不能抛出类型转换异常。

### 修复单元 AH：健康响应的 chunk 字段集合固定

- `_empty_chunk_status()` 与 `KnowledgeStore.chunk_status()` 保持同一字段集合，补齐 `chunks_local`、`chunks_prebuilt_only` 以及所有 `chunks_local_*` 计数。
- 空数据库、未来 Schema、普通数据库不可用和健康数据库的 status 只允许数值不同，不允许字段缺失。用集合等价测试锁定契约。

### 修复单元 AI：semantic 截断以 entry 而非 chunk 为单位

- 完成来源与 disabled 过滤后，先扫描所有合格 chunk，为每个 entry rowid 保留最高分 chunk；随后才对唯一 entry 候选排序并应用现有候选预算和最终 `limit`。
- 相同分数使用 entry rowid 与 chunk index 建立稳定次序；`best_chunk_index` 继续指向该 entry 的最高分 chunk。
- 总向量上限仍为 20,000，单次扫描 O(chunks)，候选映射 O(unique entries)；不扩大快照和返回预算。

### 复核结论 AJ：混合脚本 routing 评论不成立

- `KnowledgeService._get_routing_state()` 是产品中唯一的 `RoutingConfig` 构造点；它使用 `_effective_match_policy()`，而该函数只在 `KNOWLEDGE_MATCH_POLICY` 上替换来源集合，保留 `latin_word_boundaries=False`。
- 因此评论所指 `_contains_latin()` 分支当前产品路径不可达。`C语言` 会进入完整的 compact strong term `c语言`，单独的 `C` 不会命中；仓库也没有第二个启用 Latin boundary 的 `MatchPolicy` 实例。
- 不为不可达的预留分支扩大本轮实现。在线程中回复完整调用依据并 resolve；若未来启用该开关，启用提交必须先定义纯 Latin 与混合脚本契约及回归测试。

### 修复单元 AK：degraded 作业必须有受控恢复入口

- Bridge allowlist 暴露既有 `POST packs/jobs/discard`，仍受 loopback、token、CSRF、64 KiB 正文上限和 Main Server 二次 mutation 校验保护。
- 前端 API 增加 discard 方法；管理页在知识包页展示隔离作业的 job/pack/reason，并要求用户确认后逐个丢弃。成功后刷新 status、pack 与 job 状态，失败显示既有操作失败反馈。
- 维护 CLI 增加互斥动作 `--discard-job JOB_ID`，只调用既有 `discard_degraded_pack_job()`；非 degraded、非法路径或不存在作业返回非零，不提供任意目录删除能力。

### 修复单元 AL：词法精确匹配保留有意义标点

- 搜索同时构造 Unicode NFKC、casefold、空白规范化但保留标点的 folded surface。标题和 alias 的 1000/950 精确分只比较该 surface；compact normalization 继续用于 contains、recognition 和 tag fallback。
- `C++`、`C#`、`.NET` 等不再折叠成同一个精确键；大小写和兼容字符仍可等价。FTS/LIKE 只负责候选召回，不改变最终精确排序。

### 修复单元 AM：degraded 不阻断无关向量维护

- indexer 的 pending gate 使用 `TERMINAL_STATES | {DEGRADED_STATE}`，与 `process_pack_jobs()` 和 status 的非 pending 语义一致。
- degraded 仍使 registry health 为 invalid 并保留人工恢复提示，但不会占用可推进作业、不会令无关 bundled/installed source 的 `index_embedding_batch()` 永久停摆。

## 第五轮实施顺序与关闭条件

1. AG、AH、AM 先修持久化和健康面，确保损坏作业不会让诊断与后台维护同时失效。
2. AI、AL 独立收敛检索语义，分别覆盖 chunk 拥塞和标点术语的反例；AJ 只回复不可达调用链证据。
3. AK 贯通 Main Server、Bridge、前端和 CLI；不复制删除逻辑，只暴露既有严格 discard 能力。
4. 完整回归、前端类型/i18n、窄屏横向溢出与远端 CI 通过后，逐条回复提交和测试依据，再 resolve 7 条线程。

## 第五轮实施结果

| 提交 | 覆盖单元 | 关键结果 |
| --- | --- | --- |
| `43c138ce4` | AG、AH、AM | 严格验证作业时间字段；空状态补齐本地向量策略字段；degraded 不再占用 pending gate |
| `2a114dd23` | AI、AL、AJ | semantic 在 entry 去重后截断；词法精确匹配保留标点；以唯一生产构造链证明混合脚本评论不成立 |
| `5557d1760` | AK | Bridge、管理界面和维护 CLI 统一暴露既有严格 discard；网页提供确认、反馈和八语言文案 |
| `4b75b24b4` | AN | identity 的创建时间在可信返回前按 state 同一规则规范化，拒绝布尔值与浮点 fallback |

本地合并前回归覆盖知识库、公共知识路由、请求体守门、Plugin Market、Study Companion 轻量导入及核心 takeover 生命周期，共 414 项测试通过；本轮 Python 改动 Ruff 检查通过。前端 `vue-tsc --build`、API Vitest（8 项）和 i18n 完整性检查通过，八种语言均为 736 个键。

恢复入口另在 390px 与 1024px 视口完成真实渲染。两种宽度的横向溢出均为 0，删除按钮保持 72×44px；窄屏底部增加含 safe-area 的滚动安全区后，按钮与固定浮层不再碰撞。独立 fresh-eyes 复审未发现新的 blocker 或 major。测试退出后的 telemetry 日志在受限沙盒中仍会报告既存的本机配置目录写入失败，但 pytest 返回码为 0。GitHub CI 结果以本节文档提交所在远端头部的检查为准。

## 第五轮审查正文补充：identity 时间戳信任边界

提交 `2a114dd23` 后的 review body 指出，AG 虽已严格验证 state 时间字段，但 `_validated_identity()` 仍先用 `int()` 转换 identity 的 `created_at`。这会接受 `true` 和 `1.5`；当 state 缺少 `created_at` 时，该值可能作为 fallback 进入正常作业。

### 修复单元 AN：identity 时间戳必须在可信返回前规范化

- `_validated_identity()` 在返回 `state="valid"` 前，使用与 state 相同的时间戳规范化函数检查 `created_at`。只接受非负整数或规范 ASCII 十进制字符串；布尔值、浮点、负数和其他字符串均使 identity 无效。
- 无效 identity 统一隔离为 `invalid_job_identity`，展示时间回退到可信目录 mtime；不得把不可信 identity 时间传给 state fallback、排序或裁剪。
- 保留对合法旧 identity 数字字符串的兼容，不改变 job/pack identity、容量计数或显式 discard 边界。

验收：state 缺少 `created_at` 且 identity 分别为 `true`、`1.5` 时，作业均进入 degraded，列表与 status 不抛异常；合法整数 identity 的恢复行为不回归。

## 第五轮后续线程：候选召回、作业身份与有界读取

提交 `4b75b24b4` 后重新收集全部未解决线程，又出现 4 条可构造的有效边界。它们不改变前述容量和失败语义，只补齐候选生成、legacy 作业认证、管理查询线程边界和内存任务保留上限。

### 修复单元 AO：标点精确项必须在通用候选截断前召回

- KnowledgeStore 增加按原始标题或 alias 精确查询的参数化入口，保留标点并支持 SQLite ASCII 不区分大小写；来源过滤与通用 FTS/LIKE 使用同一约束。
- KnowledgeRetriever 先合并精确候选，再补充各自有上限的 FTS/LIKE 候选；最终仍由 `_folded_exact_surface()` 做 NFKC、casefold 和空白规范化判分。精确查询不替代通用召回，也不扩大最终返回 limit。
- 精确候选自身仍使用现有 `candidate_limit`。同一 surface 有大量重复项时任取其中稳定前缀不影响“查询项被较宽 compact token 挤掉”的边界；disabled 余量继续计入上限。

验收：构造超过 candidate limit 的较早 `C` 候选，再插入 `C++`，查询 `C++` 且 limit=1 必须返回 `C++`；来源与 disabled 过滤不回归。

### 修复单元 AP：缺失 identity 的 legacy state 必须自证目录身份

- 保留当前对本 PR 早期 staged job 的兼容，但只有 state 自身完整满足 identity 契约时，缺失 `identity.json` 的作业才可继续：`job_id` 必须等于目录名且无路径语义，`pack_id` 合法，创建时间与容量计数均可验证。
- 抽取单一 identity payload 校验器，磁盘 identity 与 legacy state 共用；不得出现“有 identity 严格、无 identity 反而直接信任”的分叉。
- legacy state 认证失败后进入 `invalid_job_identity` degraded/orphan，不参与调度、不解析它声明的其他目录；仍可通过严格 discard 恢复。

验收：合法无 identity 的早期作业仍可激活；缺失 identity 且 state 的 job_id 指向其他目录或非法路径时只隔离当前目录，不能处理目标目录。

### 修复单元 AQ：管理目录的来源元数据一次读取且离开事件循环

- source registry 提供批量解析入口：一次读取并解析 `packs.json`，合并内置来源、社区来源和未知来源 fallback，返回请求所需 tag 的完整映射。
- entries 搜索页、普通目录页和单条详情在构造响应前，通过 `asyncio.to_thread` 批量加载来源映射；`_entry_payload()` 只做纯内存格式化，不再在事件循环中触发文件读取。
- registry 不可读时保持既有展示降级：社区来源显示安全的 tag/Unknown，不在只读目录请求中覆盖或修复文件。

验收：100 条、多个社区来源的页面只读取 registry 一次，读取发生在非请求线程；内置和未知来源展示不回归。

### 修复单元 AR：Marketplace 订阅任务记录同时受 TTL 与数量上限约束

- 与相邻安装任务注册表一致，内存 `_tasks` 最多保留 200 条；TTL 清理后仍超限时，按可信 `completed_at`、再按 `created_at` 删除最旧 terminal 记录。
- `_task_workers` 中的活动任务和尚无 `completed_at` 的记录永不因数量裁剪。创建与 done callback 后都执行裁剪，使快速成功/失败序列不能等一小时才释放。
- 裁剪 terminal 时不触碰正在 unsubscribe 的 package reservation；活动映射继续由 `_subscription_done()` 的所有权检查清理。

验收：201 条 terminal 只保留最新 200 条；混合活动与 terminal 时只删除最旧 terminal；TTL、同包去重、4 worker 上限和任务查询行为不回归。
