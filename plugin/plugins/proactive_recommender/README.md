# 个性化主动推荐插件

这个插件在不修改 NEKO 本体的前提下完成内容推荐闭环：

1. 从现有 `bus.memory` 最近一小时窗口读取用户原话。
2. 通过配置好的 Agent 模型抽取非敏感兴趣；模型不可用时使用本地保守规则。
3. 调用已有 `web_search:search`，也可选择调用 `bilibili_danmaku:bili_search`。
4. 对候选做相关性、质量、去重和负反馈排序。
5. 通过全局主动聊天开关、安静时段、隐私前台、离开状态、日限额、最小间隔和忽略退避门控。
6. 使用隐藏的 `push_message(..., ai_behavior="respond")` 让 NEKO 主模型按当前角色自然开口。

## 启用

插件默认 `enabled = false` 且 `shadow_mode = true`。先在插件配置中把
`recommendation.enabled` 改为 `true`，观察 `recommendation_status` 的候选和 shadow
历史；确认效果后再把 `shadow_mode` 改为 `false`。

B 站来源默认关闭。只有在 `bilibili_danmaku` 插件可用时，才应开启
`recommendation.sources.bilibili`。

状态保存在插件自己的 PluginStore 中；状态接口不会返回用户原始对话。
