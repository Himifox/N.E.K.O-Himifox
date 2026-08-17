# OpenBiliClaw 内建运行时

> **当前契约。** 本页记录由 N.E.K.O 主服务进程直接管理的一方集成。

`app/openbiliclaw_runtime.py` 在进程内嵌入一个 `OpenBiliClawCore`，并在
`http://127.0.0.1:8420` 托管浏览器扩展既有 API。它既不是用户插件，也不是
MCP 通道；关闭这两套可选功能不会禁用 OpenBiliClaw。

## 生命周期与存储

- N.E.K.O 完成存储初始化后，由主服务启动 Core。
- 主服务退出时先停止本地 Uvicorn 桥接器，再由 ASGI 关闭 Core 的任务、队列、
  客户端与数据库。
- 数据和配置位于 `<N.E.K.O 数据根目录>/integrations/openbiliclaw/`，不与角色
  记忆数据库混用。
- 导入失败或 `8420` 端口被占用时，集成状态为 `unavailable`，但不会阻止 NEKO
  本体启动。

浏览器扩展继续使用原有 `/api/*` HTTP/WebSocket 契约，用户无需再运行
`openbiliclaw start`。NEKO 未运行时本地监听器不存在；支持离线缓存的扩展版本
可以保留行为事件，待监听器恢复后补传。

## 模型边界

适配器会读取 NEKO 已解析的对话模型配置，并以内存实例路由的方式提供给 Core，
不会把 API Key 复制进 OpenBiliClaw 的 `config.toml`。自定义与 Qwen 兼容端点使用
OpenAI-compatible 协议适配器。

内容向量仍由 OpenBiliClaw 独立配置。NEKO 的角色记忆向量与 OpenBiliClaw 的内容
向量具有不同数据结构，不能因为都叫“向量”就共享存储。

## 状态与恢复

NEKO 主服务的 `GET /api/openbiliclaw/status` 仅允许本机访问，返回运行状态、扩展
连接地址、数据目录、降级标记和脱敏错误，不返回模型密钥。

- `NEKO_OPENBILICLAW_ENABLED=0`：仅关闭这项内建集成；
- `NEKO_OPENBILICLAW_PORT=<端口>`：修改本机桥接端口，默认 `8420`，浏览器扩展
  配置也必须同步修改。

依赖在 `pyproject.toml` 与 `uv.lock` 中固定到准确的 Core 提交。NEKO 继续使用
`bilibili-api-dev` 提供共享的 `bilibili_api` 导入，uv 会屏蔽发生文件冲突的上游轮子。
