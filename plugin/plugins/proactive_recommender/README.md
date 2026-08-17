# OpenBiliClaw 个性化推荐兼容层

这个插件在插件进程内完成两条个性化信号链路，不要求浏览器扩展直接访问 NEKO 本体：

1. 从 NEKO 现有 `bus.memory` 读取最近一小时的用户消息。
2. 在 `127.0.0.1:8421` 提供 OpenBiliClaw 扩展兼容接口，接收扩展主动上报的跨平台行为事件。
3. 把两类信号压缩成轻量、可检查的非敏感兴趣词。
4. 调用已有 `web_search:search`，也可选调用 `bilibili_danmaku:bili_search` 发现候选。
5. 经过相关性、去重、全局主动聊天开关、隐私前台、离开状态、安静时段和频率门控后，通过隐藏的 `push_message(..., ai_behavior="respond")` 让 NEKO 自然开口。

## 浏览器扩展兼容范围

当前实现的是“行为事件兼容层”，支持 OpenBiliClaw 扩展后台所需的：

- `GET /api/ping`
- `GET /api/health`
- `GET /api/runtime-status`
- `GET /api/runtime-stream`（WebSocket）
- `POST /api/events`
- 通知、认知更新和 delight 的空队列/确认接口

这不是 OpenBiliClaw 完整后端的复制品。扩展的账号抓取任务队列、初始化流程、内容池与弹窗推荐列表不在当前兼容范围内。

## 隐私边界

- 兼容服务只绑定本机回环地址 `127.0.0.1`。
- B 站和抖音 Cookie 接口会明确拒绝请求；Cookie 不会被读取或保存。
- 不保存完整 URL、DOM 快照、原始页面标题或原始平台事件。
- 长期状态只保存事件指纹、按平台计数和提炼后的兴趣词。
- 不推断健康、政治、宗教、财务等敏感属性。

## 使用

1. 在 Hosted UI 中启用“OpenBiliClaw 行为事件桥”。
2. 在 OpenBiliClaw 浏览器扩展里把后端设为 `127.0.0.1`、端口设为 `8421`；如果在 Hosted UI 修改了端口，两边保持一致。`8421` 刻意避开完整 OpenBiliClaw 后端默认使用的 `8420`。
3. 先使用影子模式观察兴趣和候选，确认后再切换正式运行。

候选搜索仍依赖已安装的搜索插件；启用 B 站搜索前，需要 `bilibili_danmaku` 插件提供 `bili_search` 能力。
