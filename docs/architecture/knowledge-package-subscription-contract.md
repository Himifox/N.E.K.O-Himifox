# 知识包订阅交接接口（可信市场 v2）

## 定位

知识库运行时仍由 Main Server 内置托管。插件管理器只是知识库管理和未来市场订阅的用户入口，知识包本身不是可执行插件。

当前协议接口相当于“水电接口”：市场服务下载并校验纯数据制品，再把经过验证的数据交给本地接口；不需要直接修改 SQLite、检索器、对话递卡或记忆系统。

```text
知识市场服务
→ 下载知识正文、索引清单、向量三个独立制品
→ 校验可信市场身份、文件大小与三个 SHA-256
→ POST /api/public-knowledge/subscriptions/apply-v2
→ Main Server 再次校验协议、五字段词条、确定性分块和向量绑定
→ 原子替换该 community source
→ 校验成功时启用混合检索；否则安全降级为 BM25
```

## 三个发布制品

可信市场 v2 可发布以下三个相互绑定的制品：

| 制品 | 推荐后缀 | 内容 |
| --- | --- | --- |
| 知识正文 | `.neko-knowledge.json` | 现有五字段知识包 |
| 索引清单 | `.neko-knowledge.index.json` | 模型、分块版本、chunk 哈希与向量行号 |
| 向量矩阵 | `.neko-knowledge.vectors.f16` | little-endian float16、按清单顺序逐行排列 |

知识正文是唯一必需制品。索引清单和向量必须同时出现；缺少任意一个、摘要不匹配、版本不兼容或校验失败时，本地仍安装正文，但默认只使用 BM25。普通本地导入也默认 BM25，不会因导入知识而隐式加载 Embedding 模型。

索引制品的固定格式和校验顺序见 [预构建知识向量索引](knowledge-prebuilt-index.md)。

## 协议 v1（兼容）

本地端点：

```text
POST /api/public-knowledge/subscriptions/apply
```

请求结构：

```json
{
  "protocol_version": 1,
  "subscription": {
    "provider": "plugin-market",
    "remote_id": "knowledge/example-pack",
    "version": "1.0.0",
    "channel": "stable",
    "artifact_sha256": "64-character-lowercase-sha256"
  },
  "pack": {
    "schema_version": 2,
    "pack_id": "example-pack",
    "collection_id": "meme",
    "material_type": "knowledge",
    "source": {
      "name": "Example Pack",
      "homepage": "https://example.invalid",
      "license": "CC0-1.0"
    },
    "entries": [{
      "title": "Example",
      "terms": {"alias": [], "recognition": []},
      "tags": ["type:reference"],
      "summary": "A compact summary",
      "content": "Reference content"
    }]
  }
}
```

市场制品使用 `.neko-knowledge.json` 后缀，其文件字节必须等于 `pack` 对象按 UTF-8、JSON 键排序、无多余空白序列化后的结果。`artifact_sha256` 因此同时是下载文件摘要和规范化 `pack` 摘要。Market Bridge 先验证下载字节，Main Server 再独立复算，不信任调用方给出的结果。

协议 v1 只交付知识正文，没有可信的索引身份，因此激活后使用 BM25。该兼容路径不会接受预构建向量。知识包自身的 `schema_version` 独立于订阅协议版本：旧知识包 Schema v1 缺少类型时按 `knowledge` 处理；新知识包 Schema v2 必须在包根声明 `material_type=knowledge|corpus`，词条仍严格保持五字段。

## 协议 v2（可信市场）

v2 的市场版本描述符把三个制品分别列出。索引清单与向量可省略，但不能只提供其中一个：

```json
{
  "protocol_version": 2,
  "package_id": 42,
  "remote_id": "knowledge/example-pack",
  "pack_id": "example-pack",
  "version": "2.0.0",
  "channel": "stable",
  "artifacts": {
    "knowledge": {"url": "https://…/example.neko-knowledge.json", "sha256": "…", "bytes": 1024},
    "index_manifest": {"url": "https://…/example.neko-knowledge.index.json", "sha256": "…", "bytes": 2048},
    "vectors": {"url": "https://…/example.neko-knowledge.vectors.f16", "sha256": "…", "bytes": 4096}
  }
}
```

Market Bridge 校验并下载制品后，以受限 multipart 请求调用：

```text
POST /api/public-knowledge/subscriptions/apply-v2
```

正文使用 `pack` 文件字段；可选索引使用 `index_manifest` 和 `vectors` 文件字段。旧的 JSON `subscriptions/apply` 端点继续只处理 v1 正文包。

本地交接订阅元数据同时记录 `index_manifest_sha256`、`vectors_sha256` 和固定的 `trust=trusted_market`。只有来自受信任市场交接链、三个摘要全部匹配且本地二次校验通过的索引，才可标记为可信预构建索引。直接导入的 JSON、用户提供的旁路文件和 v1 订阅不能自行声明 `trusted_market`。

## 固定边界

- 只接受纯 JSON 数据包，不执行脚本，不加载插件入口。
- 词条仍只有 `title / terms / tags / summary / content` 五个业务字段。
- 订阅提供方、远端 ID、版本、通道和摘要保存在来源级 `packs.json`，不复制到每条词条。
- 数据包只能写入已注册的知识集合，来源标签固定为 `source:community.<pack_id>`。
- 更新按整个数据包原子替换；注册表写入失败时恢复旧来源。
- 管理器桥接仅允许固定知识 API 路径，不能作为任意 Main Server 代理。
- 所有写操作继续经过现有 Bridge Token、CSRF 和 Origin 校验。
- `knowledge` 包安装后默认不参与自动搭话，需由用户单独开启该数据包的自动上下文；`corpus` 包始终禁止自动搭话，只能在显式查询中使用。
- 社区包默认采用 `prebuilt_only` 策略：可信预构建索引可用时使用混合检索，否则使用 BM25。
- “允许本机维护向量”必须由用户按包显式开启；未授权时不会为社区包在本机补算或重建向量。
- 不写入用户记忆，也不持久化用户对话。

## 市场接线

插件市场通过独立知识包目录发布制品，不把知识包伪装成可执行插件。市场网页只把目录 ID、远端 ID、版本、通道、三个制品 URL、大小和摘要交给本地端点：

```text
POST /market/knowledge/subscribe
GET  /market/knowledge/tasks/{task_id}
GET  /market/knowledge/subscriptions
POST /market/knowledge/unsubscribe
```

本地 Bridge 只从允许的 HTTPS 制品主机下载，分别执行大小、后缀、SHA-256 和规范 JSON 校验，再调用 Main Server 交接端点。Main Server 不信任 Bridge 的结论，会重新验证知识正文、分块身份、模型契约和向量数据。安装结果以本地 `packs.json` 为准；Market 账户记录为尽力同步，失败不回滚已经安全落地的本地知识包。
