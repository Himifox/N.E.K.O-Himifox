# Knowledge / Corpus 知识包编写与发布

## 统一模型

公共知识只使用一个数据库和一套检索接口。`material_type` 表示内容用途，不表示数据库、索引或工具：

| 类型 | 内容 | 普通聊天 | 显式公共知识查询 |
| --- | --- | --- | --- |
| `knowledge` | 事实、定义、解释、梗义、出处 | 用户启用包后，明确标题/别名/识别词可自动注入 | BM25 + Embedding + RRF |
| `corpus` | 回复范例、对话样例、写作与语气素材 | 永不自动注入 | BM25 + Embedding + RRF |

`meme` 不再是集合或第三种材料类型。它只是可选主题标签 `domain:meme`：梗的含义属于 `knowledge`，梗式回复样例属于 `corpus`。该标签只影响结果交给模型时的表达策略，不改变存储和检索路径。

## 知识包 Schema v3

Schema v3 删除 `collection_id`。包根只能包含 `schema_version`、`pack_id`、`material_type`、`source` 和 `entries`：

```json
{
  "schema_version": 3,
  "pack_id": "example-meme-knowledge",
  "material_type": "knowledge",
  "source": {
    "name": "Example Publisher",
    "homepage": "https://example.invalid",
    "license": "CC0-1.0"
  },
  "entries": [
    {
      "title": "示例梗",
      "terms": {
        "alias": ["示例别名"],
        "recognition": ["这是什么梗"]
      },
      "tags": ["domain:meme"],
      "summary": "一句简短、可核验的解释。",
      "content": "完整含义、出处与使用边界。"
    }
  ]
}
```

Corpus 包只需把 `material_type` 改为 `corpus`，正文可写成“用户输入 / 参考回复”。每条词条仍严格只允许：

```text
title / terms / tags / summary / content
```

`chunks`、哈希、向量、模型 ID 和索引状态都是系统派生数据，不能写入原始包。

Schema v1/v2 和 `collection_id=meme|corpora` 不再兼容；发布者必须重新生成规范 Schema v3 原始包及其摘要。

## 原始包与预构建索引

原始 `.neko-knowledge.json` 是唯一真实来源，必须存在。预构建索引是可选性能缓存：

```text
<pack>.neko-knowledge.json
<pack>.neko-knowledge.index.json
<pack>.neko-knowledge.vectors.f16
```

当前固定契约：

```text
embedding_model_id      = local-text-retrieval-v1-256d-int8-mlen1024
embedding_input_version = 2
chunker_version         = 1
embedding_dimensions    = 256
vector_encoding         = float16-le-row-major
```

构建并验证：

```powershell
uv run --python 3.11 python scripts/build_knowledge_pack_index.py dist/example.neko-knowledge.json --output-dir dist
uv run --python 3.11 python scripts/build_knowledge_pack_index.py dist/example.neko-knowledge.json --verify --manifest dist/example.neko-knowledge.index.json --vectors dist/example.neko-knowledge.vectors.f16
```

索引缺失或校验失败不阻止原文安装：包立即以 BM25 工作。社区包只有在用户明确允许本机维护向量后才进入本地 Embedding 队列。

## 运行路径

```text
导入 Schema v3 包
  → 同一 knowledge/knowledge.db
  → entries + FTS + knowledge_chunks

普通聊天
  → 只对已授权 knowledge 包运行明确词条匹配
  → corpus 不参与

显式查询
  → 一次 BM25 + 一次 Query Embedding
  → 同一候选池内 RRF
  → material_type 仅用于过滤或回答策略
  → 一次 LLM 回复
```

Knowledge 与 corpus 不会触发两次 Query Embedding 或两次 LLM 请求。

自动上下文没有梗专用模糊规则。标题和别名过短、容易与普通句子冲突时，作者应提供更明确的 `terms.recognition`（例如“上头是什么意思”），而不是依赖系统删除语气词或替换代词后猜测含义。

## 本地管理 API

```text
GET  /api/public-knowledge/status
GET  /api/public-knowledge/packs
POST /api/public-knowledge/packs/material-type
POST /api/public-knowledge/packs/auto-context
POST /api/public-knowledge/packs/index-policy
```

修改包用途只更新来源级策略，不改写原始词条。`corpus` 无法开启自动上下文。

完整制品协议另见：

- [知识包订阅交接接口](knowledge-package-subscription-contract.md)
- [预构建知识向量索引](knowledge-prebuilt-index.md)
- [GitHub Actions 发布示例](knowledge-prebuilt-index-github-actions.md)
