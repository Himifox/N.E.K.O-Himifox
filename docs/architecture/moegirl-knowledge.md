# 公共知识模块迁移说明

历史上的 `moegirl` / `meme` 命名只代表早期数据来源和实现目录，不再构成公开产品架构。

当前公共知识统一使用：

- 一个 `public-knowledge/knowledge.db`；
- 一个显式查询工具；
- 一次 BM25、一次 Query Embedding 和一次 RRF；
- `knowledge|corpus` 两种内容用途；
- `domain:meme` 等普通主题标签。

普通聊天只会自动注入用户已授权 `knowledge` 包的明确词条命中；`corpus` 仅能显式查询。完整说明见 [Knowledge / Corpus 知识包编写与发布](knowledge-corpus-authoring.md)。
