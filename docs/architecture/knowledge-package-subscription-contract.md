# 知识包订阅交接接口

## 定位

知识库运行时仍由 Main Server 内置托管。插件管理器只是知识库管理和未来市场订阅的用户入口，知识包本身不是可执行插件。

当前预留的协议接口相当于“水电接口”：未来市场服务只需下载并校验知识包，再把经过验证的数据交给本地接口；不需要修改 SQLite、检索器、对话递卡或记忆系统。

```text
知识市场服务
→ 下载纯数据知识包
→ 校验发布者与 SHA-256
→ POST /api/public-knowledge/subscriptions/apply
→ Main Server 再次校验协议、摘要和五字段词条
→ 原子替换该 community source
→ 下次本地查询或对话命中生效
```

## 协议 v1

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
    "schema_version": 1,
    "pack_id": "example-pack",
    "collection_id": "meme",
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

## 固定边界

- 只接受纯 JSON 数据包，不执行脚本，不加载插件入口。
- 词条仍只有 `title / terms / tags / summary / content` 五个业务字段。
- 订阅提供方、远端 ID、版本、通道和摘要保存在来源级 `packs.json`，不复制到每条词条。
- 数据包只能写入已注册的知识集合，来源标签固定为 `source:community.<pack_id>`。
- 更新按整个数据包原子替换；注册表写入失败时恢复旧来源。
- 管理器桥接仅允许固定知识 API 路径，不能作为任意 Main Server 代理。
- 所有写操作继续经过现有 Bridge Token、CSRF 和 Origin 校验。
- 安装后默认不参与自动搭话，需由用户单独开启该数据包的自动上下文。
- 不写入用户记忆，也不持久化用户对话。

## 市场接线

插件市场通过独立知识包目录发布制品，不把知识包伪装成可执行插件。市场网页只把目录 ID、远端 ID、版本、通道、制品 URL 和摘要交给本地端点：

```text
POST /market/knowledge/subscribe
GET  /market/knowledge/tasks/{task_id}
GET  /market/knowledge/subscriptions
POST /market/knowledge/unsubscribe
```

本地 Bridge 负责受限下载、10 MB 限制、SHA-256 和规范 JSON 校验，再调用 Main Server 交接端点。安装结果以本地 `packs.json` 为准；Market 账户记录为尽力同步，失败不回滚已经安全落地的本地知识包。
