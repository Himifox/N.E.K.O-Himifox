# 数据备份插件

该插件把备份能力集成到 N.E.K.O 的原生插件运行时中，不启动额外的 Flask 服务。

- `core`：`config`、`character_cards`、`memory`
- `assets`：`card_faces`、`live2d`、`vrm`、`mmd`、`pngtuber`、`workshop`
- 快照保存在当前 N.E.K.O 数据根目录的 `plugins/data_backup/data/snapshots` 下。
- 恢复前会自动创建一份安全快照；恢复后需要重启 N.E.K.O。
- 符号链接不会进入快照，所有操作仅接受固定组名和插件生成的快照 ID。

设计参考 [MemoryCat](https://github.com/JohnChiao75/MemoryCat) 的快照与分组备份思路。MemoryCat 以 Apache License 2.0 发布；本实现针对 N.E.K.O SDK v2 重新组织，并保留本说明作为来源致谢。
