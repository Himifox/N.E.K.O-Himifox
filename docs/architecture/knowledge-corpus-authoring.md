# Knowledge / Corpus 知识包编写与发布

## 1. 两种材料的边界

知识包 Schema v2 在包根声明 `material_type`，词条本身仍然只有五个业务字段。

| 类型 | 适合内容 | 猫娘的使用方式 | 普通聊天自动注入 |
| --- | --- | --- | --- |
| `knowledge` | 事实、定义、解释、梗的含义和出处 | 回答“是什么、为什么、什么意思” | 仅在用户明确启用该包后允许 |
| `corpus` | 回复范例、对话样例、语气和写作风格素材 | 根据当前情况引用、改写或模仿 | 永远禁止，只能显式查询 |

`meme` 是内容主题，不是第三种材料类型。解释一个梗属于 `knowledge`；收集“遇到这种话可以怎样回复”的样例属于 `corpus`。

旧 Schema v1 包仍可安装，并统一按 `knowledge` 处理。需要语料语义的新包应发布为 Schema v2。

## 2. Schema v2

### Knowledge 示例

```json
{
  "schema_version": 2,
  "pack_id": "example-meme-knowledge",
  "collection_id": "meme",
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
      "tags": ["type:引用"],
      "summary": "一句简短、可核验的解释。",
      "content": "完整含义、出处与使用边界。"
    }
  ]
}
```

### Corpus 示例

```json
{
  "schema_version": 2,
  "pack_id": "example-reply-corpus",
  "collection_id": "corpora",
  "material_type": "corpus",
  "source": {
    "name": "Example Publisher",
    "homepage": "https://example.invalid",
    "license": "CC0-1.0"
  },
  "entries": [
    {
      "title": "这个梗也太老了吧",
      "terms": {
        "alias": [],
        "recognition": ["这个梗太老了怎么回复"]
      },
      "tags": ["content:dialogue-sample"],
      "summary": "面对‘梗太老’时的轻松回复参考。",
      "content": "用户输入：这个梗也太老了吧\n参考回复：那我下次争取用个更新的梗。"
    }
  ]
}
```

每条词条只允许：

```text
title / terms / tags / summary / content
```

禁止上传 `chunks`、`chunk_id`、`content_hash`、Embedding、模型 ID 或索引状态。这些都是确定性派生数据。

## 3. 原始包与预构建索引

原始 `.neko-knowledge.json` 是唯一真实来源，必须存在。预构建向量只是可选缓存：

```text
<pack>.neko-knowledge.json
<pack>.neko-knowledge.index.json
<pack>.neko-knowledge.vectors.f16
```

当前只接受以下固定契约：

```text
embedding_model_id      = local-text-retrieval-v1-256d-int8-mlen1024
embedding_input_version = 2
chunker_version         = 1
embedding_dimensions    = 256
vector_encoding         = float16-le-row-major
```

在项目 Python 3.11 环境中构建：

```powershell
uv run --python 3.11 python scripts/build_knowledge_pack_index.py `
  dist/example.neko-knowledge.json `
  --output-dir dist
```

不加载模型、独立复验三个制品：

```powershell
uv run --python 3.11 python scripts/build_knowledge_pack_index.py `
  dist/example.neko-knowledge.json `
  --verify `
  --manifest dist/example.neko-knowledge.index.json `
  --vectors dist/example.neko-knowledge.vectors.f16
```

索引缺失、损坏或模型契约不匹配时，原始包仍然安装并立即提供 BM25；社区包默认不会消耗用户设备算力重建向量。只有用户明确开启“允许本机维护向量”后，NEKO 才为该包执行本地 Embedding。

## 4. 查询运行方式

普通聊天：

```text
明确梗标题/别名 → mention/trie → 最多一张临时 knowledge 卡片
未明确命中       → 不运行语义 RAG
corpus            → 永不自动注入
```

显式公共知识查询：

```text
一次请求
├─ BM25：并行召回所选集合
└─ Query Embedding：全请求只生成一次
       ↓
每个集合使用同一个查询向量扫描一次
       ↓
一次跨集合 RRF
       ↓
按意图优先 knowledge 或 corpus，并从同一候选池补足结果
       ↓
一次 LLM 回复
```

因此 corpus 无结果时回退 knowledge 不会产生第二次 Query Embedding，也不会增加第二次 LLM 请求。

## 5. 本地管理 API

查看包的声明类型、用户覆盖和索引状态：

```text
GET /api/public-knowledge/packs?collection=meme
GET /api/public-knowledge/packs?collection=corpora
```

本地修正错误分类，不改写原文、chunk 或向量：

```http
POST /api/public-knowledge/packs/material-type
Content-Type: application/json

{
  "collection": "corpora",
  "pack_id": "example-reply-corpus",
  "material_type": "corpus"
}
```

发送 `null` 可恢复发布者声明。将 corpus 包设为自动上下文会返回 `auto_context_not_allowed`。

## 6. 已验证制品与可复现记录

### ruozhiba-qa 三制品冒烟

- GitHub 仓库：`Himifox/NEKO-plugins-test`
- prerelease：`v0.0.0-ruozhiba-smoke.20260819.2`
- 条目/chunk：`1,494 / 1,494`
- 导入：`index_origin=prebuilt`
- 信任：`index_trust=trusted_market`
- 校验：`index_validation=accepted`
- 本地维护：`false`

摘要：

```text
pack     7ab180a448ed5fcf30a5700b71166d8ef78f15512a7011291dddcd5fbc7a2ea9
manifest ae6a27f5ada66b2dda495f0dd1b17d5ccfb1f41c7871b2932710a2ac1a54db32
vectors  8e3c70a0ceb5bd5de6789c1d183cd062d0f11e72413d9f07167ade7f2c3668ea
```

真实查询结果：精确问题和同义改写均命中目标，负例无纯语义命中；10 次预热后同义查询平均 `174.9 ms`、p95/最大值 `185.6 ms`。

### zh-meme-sft-8k 转换记录

- 数据集：`GaryYang123/zh-meme-sft-8k`
- 固定 revision：`84838bb5b1b023325499012ba956c485bbf592b4`
- 许可：MIT
- 总行数：8,680
- 分包：4,500 + 4,180
- 用途：`corpus`，不是事实来源，禁止自动上下文

源文件摘要：

```text
train.jsonl      bc608141183e007e3ecb78dd06de273af2d3b2507c140fdd934944511090de20
validation.jsonl 871de9f307979a1cfe40f48173adfe06c35547005ec79f50415bddc86b4892e1
test.jsonl       458709dffaec81fce930da3ee56c1a0547beb48251c2245772a3b35214184095
```

原始转换包摘要：

```text
zh-meme-sft-8k-01 e3c4e10172d53e77a8c56e8bfcf3fae195392665ee36c33e567f9190ec7dcffb
zh-meme-sft-8k-02 5f6501fa9d6e72708f5fc72756849bbf5009b21298decc191161a6027a980402
```

这些旧摘要对应 Schema v1 验证制品。正式发布 corpus 时必须重新输出 Schema v2，并声明 `material_type=corpus`，因此新制品摘要必然不同。

## 7. 本机验证目录不入 Git

仓库根目录的 `.codex-runtime/` 保存隔离 SQLite、日志、下载的数据集、三制品副本和应用配置；`.pnpm-store/` 是包管理器缓存。两者都属于本机可重建或可能含私有状态的产物，禁止提交。

关键协议、固定 revision、摘要、Release 和验收数据记录在本文档中。`.codex-runtime/` 原目录不会被删除；根 `.gitignore` 同时保护它不被普通 `git clean -fd` 清除或被误加入提交。

## 8. 回归命令

```powershell
uv run --python 3.11 pytest -q `
  tests/unit/test_knowledge_packs.py `
  tests/unit/test_knowledge_hybrid_retrieval.py `
  tests/unit/test_knowledge_prebuilt_index.py `
  tests/unit/test_knowledge_pack_jobs.py `
  tests/unit/test_public_knowledge_router.py `
  tests/unit/test_moegirl_fallback_layers.py
```

完整三制品协议另见：

- [知识包订阅交接接口](knowledge-package-subscription-contract.md)
- [预构建知识向量索引](knowledge-prebuilt-index.md)
- [GitHub Actions 发布示例](knowledge-prebuilt-index-github-actions.md)
