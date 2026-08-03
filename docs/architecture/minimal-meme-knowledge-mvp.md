# 最小侵入式公共梗知识库 MVP

> 状态：该内置数据方案已归档。CHIME 与 Corpora 资产保存在
> `codex/knowledge-datasets`；当前功能分支不再随包携带或启动导入它们。

## 结论

本 MVP 原设计不下载、不爬取、也不运行外部仓库代码。应用包内直接携带
[CHIME](https://github.com/yuboxie/chime) 的固定数据文件 `chime_full.json`，并在
Main Server 启动后后台导入已有的本地 SQLite/FTS5 知识库。它是公共参考资料，和
用户记忆、角色经历严格隔离。

固定数据版本：`865ef186a0e797ec5ac242524a3c45b30a429542`；数据文件 SHA-256：
`8514b8b3fef6fc2961a191c6fd815f35f3cfa3aebcd6eef3985ded48723a3c26`；预期
1,458 条。CHIME 的 MIT 许可证文本随数据一并打包。

## 范围与边界

- 复用 `MoegirlKnowledgeStore` 和 FTS5，并以通用内置工具
  `query_public_knowledge` 提供公共梗查询；结果可以包含萌娘百科、中文维基
  百科和 CHIME 三种公共来源。
- 不增加服务、进程、插件、向量模型或云端存储。
- 不修改 `recent.json`、`facts.json`、`reflections.json`、`persona.json`，也不把数据集
  内容表述成角色或用户的亲身经历。
- 不在 INFO 日志记录查询原文或知识正文；仅记录条数、增量数和错误类型。
- 保留原有萌娘百科的每日同步和实时兜底；CHIME 的导入本身没有网络请求。

## 数据流

```text
已安装应用包
  └─ knowledge/moegirl_knowledge/data/chime_full.json
        └─ 验证 SHA-256、JSON 顶层结构和 1,458 条记录
              └─ 单个 SQLite 事务写入 entries 与 FTS5
                    └─ <knowledge_dir>/moegirl-knowledge/knowledge.db
  └─ query_public_knowledge（collection="meme"）
       └─ search_moegirl_knowledge（兼容别名）
```

导入在后台任务中执行，不阻塞主程序启动或对话。关闭 Main Server 时该任务可取消；
导入失败保留已有数据库，并在 `chime_state.json` 中仅写入 `degraded` 与错误类型。

普通文本对话先执行一次轻量本地标题/别名匹配。只有高置信的短语型命中（至少三个规范化字符、
明确缩写，或用户明确询问含义的短词）才在该轮送往模型前附加一张短卡片。

本地标题/别名匹配对每条普通消息都运行，不以引号、问句、弹幕提示或特定句式为前提。语义路由功能
保留为**默认关闭**的实验能力：它会额外调用一次文本模型，不能与共享或限流的对话端点共用。启用前
必须配置独立、具备容量保证的路由端点。关闭时，本地未命中不再自动发起模型、百科或网页请求，继续
正常对话。未来启用时，路由只能返回 `skip` 或 `lookup(query, context)`；`lookup` 才会按萌娘百科、中文
维基百科、**已启用且已注册**的 `web_search` 插件顺序查询。外部请求严格串行；插件不可用、网络失败或
无可靠结果时继续正常对话。
百科层最多占用两秒，并按剩余时间在萌娘与中文维基之间串行分配，避免前者超时饿死后者；公共梗专用网页
补查最多再占用两秒，不改变用户主动使用普通网页搜索时的超时设置。
插件不可用、网络失败或无可靠结果时不注入“未命中”提示，角色按原上下文正常聊天。网页结果不写入
SQLite、用户记忆或 INFO 日志。

## 字段映射

| CHIME 字段 | 现有字段 | 规则 |
| --- | --- | --- |
| `meme` | `title` | 原样净化；不猜测语义同义词。仅允许生成确定性的规范化短语别名，以匹配人称和句末语气变化。 |
| `meaning` | `summary` | 工具卡片的简短释义。 |
| `meaning`、`origin`、`examples` | `content` | 组合为正文，保留原始信息缺失状态。 |
| `type_cn` | `tags` | `type:<值>`。 |
| `profanity`、`offense` | `tags` | 为真时加 `risk:profanity`、`risk:offense`。 |
| 固定值 | `tags` | `source:chime`、`scope:public`。 |
| 固定提交地址 | `source_url` | 仅用于可追溯归属，不在运行时访问。 |
| MIT | `source_license` | 每条记录显式携带。 |

条目 ID 为 `chime:<SHA-256(固定记录序号:规范化 meme)>`。数据集内出现规范化后同名的词条时，
仍保留各自的定义；不会擅自把它们合并为别名或断定其含义相同。

## 实现构成

| 文件 | 职责 |
| --- | --- |
| `knowledge/moegirl_knowledge/data/chime_full.json` | 随包的固定数据资产。 |
| `knowledge/moegirl_knowledge/data/LICENSE-CHIME.txt` | 原始 MIT 许可证。 |
| `knowledge/moegirl_knowledge/sources/chime.py` | 只读加载、完整性校验、字段转换；无 HTTP、无 `git clone`、无外部代码执行。 |
| `knowledge/moegirl_knowledge/store.py` | `upsert_many()` 在一次事务内更新 entries 与 FTS5。 |
| `app/main_server/moegirl_knowledge_runtime.py` | 启动后的后台导入、取消和最小状态记录。 |
| `main_logic/moegirl_knowledge_tool.py` | 按命中来源显示 CHIME（MIT 数据集）或萌娘百科，并提示风险标签。 |
| `main_logic/core/tool_calling.py` | 注册通用 `query_public_knowledge`。 |
| `main_routers/moegirl_knowledge_router.py` | 分来源状态 API，以及经 CSRF/Origin 保护的本地 CHIME 重导入操作；不返回查询或正文。 |

`CHIME_KNOWLEDGE_ENABLED=True` 只控制本地导入任务；它不表示允许网络访问。数据资产通过
`pyproject.toml` 的 wheel 强制包含规则进入发布包，避免开发环境可用、安装包缺文件。

## 验收与回归

1. 离线读取的 SHA-256、提交号和记录数必须与固定值一致。
2. 首次 `upsert_many()` 创建 1,458 条；再次导入全部为 unchanged，且无重复 FTS 行。
3. 既有检索器能命中 CHIME 条目；通用工具显示数据集来源而非伪造页面出处。
4. 哈希、格式、条数或转换任一失败时，不写入部分数据，不妨碍现有萌娘条目和对话。
5. 全部测试使用内置文件和 mock，不对 CHIME 仓库或其他第三方站点发起请求。
6. `GET /api/moegirl-knowledge/status` 能分别报告 CHIME 与萌娘百科的状态；萌娘百科降级不影响 CHIME 的 ready 状态。
7. CHIME 固定资产及其重导入流程已移至 `codex/knowledge-datasets`，不再随通用知识库功能分支打包。
8. 本地、萌百和中文维基均未命中时，才调用已注册的网络搜索插件；插件失败或未启用时返回空上下文，
   不会中断对话或持久化正文。
9. 普通句子中高置信出现内置梗标题时，模型在同一轮能看到临时参考卡；普通短词和无关句子不产生卡片。
10. 百科与插件严格串行，外部补查总耗时不超过四秒；百科最多占用两秒。

## 后续而非 MVP

未来若要更新数据集，只能通过一次明确的人工升级：审查新的发布版本、许可证和内容，更新
内置文件、提交号、SHA-256、预期条数与测试后随应用发布。运行中的客户端不自动追随
仓库分支，也不依赖维护者持续在线。
