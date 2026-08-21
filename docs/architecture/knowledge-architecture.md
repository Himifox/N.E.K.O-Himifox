# Knowledge / Corpus 架构

历史上的 `moegirl`、`meme` 和 `public-knowledge` 命名只代表早期数据来源或实现目录，不再构成当前产品架构。

当前系统只有一个公共知识数据库和一个检索入口：

- `material_type=knowledge`：事实、解释、定义和梗义；
- `material_type=corpus`：回复、对话和写作参考；
- `domain:meme`：可选主题标签，只影响回答风格，不创建独立数据库或检索接口。

首次打开新版时，如果尚未创建统一数据库，应用会优先迁移旧的
`public-knowledge/knowledge.db`；否则合并更早的 `moegirl-knowledge/knowledge.db`
与 `corpora/knowledge.db`。条目、可复用的 ready 向量、包注册信息和禁用状态
会一并迁移到 `knowledge/knowledge.db`；旧数据库不会删除，继续作为恢复副本。
检测到同一来源和标题存在内容冲突时，迁移会停止且不会发布半成品数据库。

当前 Schema、运行路径和发布方法见 [Knowledge / Corpus 知识包编写与发布](knowledge-corpus-authoring.md)。
