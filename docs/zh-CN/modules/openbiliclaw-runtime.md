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

`NekoManagedLLMProvider` 在每次 OpenBiliClaw 后台调用时读取 NEKO 当前
conversation 模型快照，再通过 NEKO 既有 `create_chat_llm_async()` 发起请求。
因此模型或路由切换会在下一次调用生效，无需重启 Core。API Key 只存在于 NEKO
配置解析结果和调用期内存，不会写入 OpenBiliClaw 的 `config.toml`；调用统一标记为
`openbiliclaw` 进入 NEKO Token 统计。Provider 支持输出预算、JSON 输出、超时、取消
和 usage 映射，并遵循 NEKO 不主动下发 temperature 的约定。

启动时，适配层会把嵌入目录中的旧 standalone LLM 实例迁移为不含密钥的
`neko-conversation` 占位路由；首次构造及每次 Core 热重载前都会重新应用该投影。
即使来源初始化或设置保存触发 reload，磁盘里的旧 DeepSeek/OpenAI 直连配置也不会
重新接管调用。暂时无法解析 conversation 路由时会安全失败，而不是回退直连。
NEKO 自带的 `free-model` 公共服务只允许用户对话，不接受后台画像/候选分析；适配层会
识别并禁用这一路由，Core 以 degraded 模式保留插件桥接且不会重复请求。要启用后台
分析，请先在 NEKO 中配置一个允许此类调用的自有会话模型，然后重启 NEKO。

“统一模型”表示 NEKO 统一管理路由、凭据与最终说话者，不表示整个系统只有一次模型
请求。OpenBiliClaw 仍可在后台调用同一路由完成画像分析、候选评估和推荐理由生成；
这些模块诊断 usage 不应再与 NEKO 总费用重复相加。

内容向量仍由 OpenBiliClaw 独立配置。NEKO 的角色记忆向量与 OpenBiliClaw 的内容
向量具有不同数据结构，不能因为都叫“向量”就共享存储。

## 单一说话者与推荐交接

```text
OpenBiliClaw 后台 → NEKO 管理的模型路由 → 结构化推荐池
NEKO 主动聊天 → preview（无 LLM、不消费）→ 既有 Phase 1
             → 既有 Phase 2（唯一猫娘台词）→ 成功投递 → 确认展示
```

- 健康 Core 每轮最多预览 3 条已完成评估和推荐理由的候选；预览不刷新来源、不调用
  LLM、不写展示历史。
- Phase 1 总候选预算为 OpenBiliClaw 固定保留 1 个槽位，其余来源继续轮询；没有
  第二次 Phase 1。
- Phase 2 继续使用 NEKO 人设、记忆和语言配置生成最终台词。正常聊天、主动聊天和
  工具链都不调用 `core.chat()`；该接口只保留给 Web、CLI 与兼容用途。
- 只有被选中且真正提交给用户的候选才确认展示；`[PASS]`、用户抢占、投递失败、
  Core degraded、空池或预览超时都不会消费候选，也不会阻断其它主动聊天来源。
- Prompt 只接收标题、URL、平台、作者、推荐依据、主题、置信度和 `item_key`，不注入
  OpenBiliClaw 完整画像，避免两套记忆互相覆盖。

浏览器扩展仍是 OpenBiliClaw 采集平台行为和浏览器会话的“手脚”。NEKO 插件系统与
MCP 无需开启；但扩展本身仍需安装和配置。NEKO 关闭期间扩展持久缓存行为事件，
NEKO 恢复后通过 `127.0.0.1:8420` 自动补传。

## 状态与恢复

NEKO 主服务的 `GET /api/openbiliclaw/status` 仅允许本机访问，返回运行状态、扩展
连接地址、数据目录、降级标记和脱敏错误，不返回模型密钥。

- `NEKO_OPENBILICLAW_ENABLED=0`：仅关闭这项内建集成；
- `NEKO_OPENBILICLAW_PORT=<端口>`：修改本机桥接端口，默认 `8420`，浏览器扩展
  配置也必须同步修改。

依赖在 `pyproject.toml` 与 `uv.lock` 中固定到准确的 Core 提交。NEKO 继续使用
`bilibili-api-dev` 提供共享的 `bilibili_api` 导入，uv 会屏蔽发生文件冲突的上游轮子。
