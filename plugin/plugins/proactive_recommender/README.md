# 个性化主动推荐插件

这个插件在不修改 NEKO 本体的前提下完成内容推荐闭环：

1. 从现有 `bus.memory` 最近一小时窗口读取用户原话。
2. 通过配置好的 Agent 模型抽取非敏感兴趣；模型不可用时使用本地保守规则。
3. 调用已有 `web_search:search`，也可选择调用 `bilibili_danmaku:bili_search`。
4. 对候选做相关性、质量、去重和负反馈排序。
5. 通过全局主动聊天开关、安静时段、隐私前台、离开状态、日限额、最小间隔和忽略退避门控。
6. 使用隐藏的 `push_message(..., ai_behavior="respond")` 让 NEKO 主模型按当前角色自然开口。

## 启用

插件默认 `enabled = false` 且 `shadow_mode = true`。打开插件的“个性化主动推荐”
Hosted UI 控制台即可调整开关、内容来源、相关性阈值、安静时段和频率门控；建议先
开启插件但保留影子模式，观察候选与拦截原因，确认效果后再切换为正式运行。

B 站来源默认关闭。只有在 `bilibili_danmaku` 插件可用时，才应开启
`recommendation.sources.bilibili`。

控制台会明确展示插件实际使用与明确不使用的信息，并显示最近一次检查为何放行或
拦截。状态保存在插件自己的 PluginStore 中；控制台和状态接口都不会返回用户原始
对话。
