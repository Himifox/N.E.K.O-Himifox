# 主动搭话领域模块化重构技术设计

状态：第一阶段后端重构已完成。本文最初基于 2026-07-15 的仓库结构制定计划，并于 2026-07-21 按实际落地结果同步模块结构、兼容边界和验收状态；前端内部拆分仍不在本阶段范围内。

一句话结论：将当前已经拆入 `main_routers/system_router/` 的主动搭话领域逻辑继续下沉到 `main_logic/proactive_chat/`；Router 只保留 HTTP、CSRF、请求解析和响应适配；通用采集能力继续留在 `utils/`；Prompt 继续留在 `config/prompts/prompts_proactive.py`；第一阶段保持 `static/app/app-proactive.js` 及全部现有前端和插件协议兼容。

## 实施状态（2026-07-21）

- 迁移路线 0～6 已完成：协议、状态、决策、生成、小游戏邀请、音乐推荐、分阶段编排和薄 Router 均已落地。
- 主流程 canonical owner 已从 Router 移至 `main_logic/proactive_chat/service.py`；Router 通过显式 collaborator 注入框架能力。
- 对照重构前分支完成了行为兼容修复：保留旧 helper 关键字调用和旧导出、恢复 WebSocket 的 falsey/异常传播与业务日志语义、恢复 Router 注入的 `memory_dir`。
- `state.py` 仍是可变状态、锁和持久化算法的唯一所有者；旧 Router 路径只保留同对象 re-export 或注入兼容薄包装，不存在双份状态或双写。
- 路线 7 的前端内部拆分继续独立排期，不作为本阶段完成条件。

## 重构前基线

原单体 `main_routers/system_router.py` 当时已经完成第一轮按路由领域拆包，主动搭话相关实现主要分布在：

```text
main_routers/system_router/
  __init__.py                 # 兼容门面和旧内部符号 re-export
  proactive_chat_flow.py      # /proactive_chat、music_played_through 与主流程
  proactive_parsing.py        # Phase 1 解析、标签清理、响应体构造
  proactive_history.py        # 搭话历史、素材历史、计数和相似度
  proactive_sources.py        # source 历史、权重和选择辅助
  proactive_content.py        # source 内容日志和格式化
  mini_game_invite.py         # 小游戏邀请状态、投递、反馈及 HTTP 路由
  break_reminders.py          # 休息提醒等相邻子流程
```

基线规模约为：

- `proactive_chat_flow.py`：2933 行。
- `mini_game_invite.py`：837 行，并已有大规模独立单元测试。
- `proactive_parsing.py`：548 行。
- `proactive_history.py`：465 行。
- `proactive_sources.py`：276 行。

因此本次不是再次“拆分单体 Router”，而是把 Router 包中已经形成的主动搭话领域模块迁入 `main_logic/proactive_chat/`，建立稳定的领域边界和单向依赖。

前端真实兼容入口为 `static/app/app-proactive.js`；`static/app-proactive.test.cjs` 是对应的 Node 契约测试。后续文档和实现不得再使用旧路径 `static/app-proactive.js` 指代入口文件。

## 目标与非目标

### 目标

- 让 `main_routers/system_router/` 只承担 HTTP 和框架适配职责。
- 让主动搭话的协议契约、状态、决策、生成、子能力和编排拥有明确的领域归属。
- 保持用户行为、公开 API、WebSocket 事件、插件事件和前端全局 API 不变。
- 每个迁移步骤都可单独回归、单独回滚，不引入双份状态。
- 将测试逐步从 Router 内部实现迁移为对领域模块和公开协议的直接验证。

### 非目标

- 不重写或重调主动搭话策略。
- 不改变默认开关，不新增或重命名设置字段。
- 不改变 Phase 1/2 Prompt 文案和模型 tier。
- 不把主动搭话专属逻辑迁入 `utils/`。
- 不在第一阶段拆分前端实现。
- 不把 `main_logic/proactive_delivery.py` 或 `main_logic/session_state.py` 并入主动搭话包。
- 不借模块迁移修改反馈权重、邀请概率、去重阈值或播放完成语义。

## 已落地结构

```text
main_logic/proactive_chat/
  __init__.py
  contracts.py               # action / reason_code / stage、命令与领域结果
  state.py                   # 搭话、素材、source 历史，计数、持久化、相似度
  decisions.py               # 入口 gate、activity gate、source 选择、PASS 判定
  generation.py              # Phase 1/2、解析、输出清理和生成保护
  delivery.py                # 投递提交、成功后记录和生命周期收口
  mini_game_invite.py        # 邀请状态、命中、冷却、选择和投递结果
  music_recommendation.py    # 音乐推荐、严格约束、链接和播放完成反馈
  break_reminders.py         # anti-slack、休息提醒和组合邀请子流程
  content_logging.py         # 主动搭话 source 内容日志辅助
  service.py                 # 主流程编排，不依赖 FastAPI 对象
```

不引入 `channels/` 中间层：

- `mini_game_invite.py` 明确表示本模块只拥有小游戏邀请，而不是整个小游戏系统。
- `music_recommendation.py` 明确表示本模块只拥有主动音乐推荐，而不是整个音乐播放系统。
- 主动搭话包本身已经提供足够的领域上下文，无需再增加抽象含混的目录层级。

继续保留的 Router 适配层：

```text
main_routers/system_router/
  proactive_chat_flow.py     # 主动搭话 HTTP 路由、请求/响应及 WS 适配
  mini_game_invite.py        # 邀请反馈 HTTP 路由和协议适配
  break_reminders.py         # Router 注入适配与旧入口兼容
  proactive_history.py       # 状态对象 re-export 与注入目录兼容包装
  proactive_sources.py       # source 状态 re-export 与注入目录兼容包装
  proactive_parsing.py       # 协议/解析旧路径兼容门面
  proactive_content.py       # 内容 helper 旧路径兼容门面
  __init__.py                # 过渡期 re-export，按调用方逐步收缩
```

依赖方向必须保持为：

```text
Router adapters
  -> proactive_chat.service
    -> contracts / state / decisions / generation / delivery
       / mini_game_invite / music_recommendation
       / break_reminders / content_logging
      -> proactive_delivery / session_state / config / utils
```

`main_logic/proactive_chat/` 禁止反向导入 `main_routers.system_router`、FastAPI `Request`、`JSONResponse` 或共享 Router 对象。

## Service 边界

Router 必须完成请求读取、CSRF/本地变更校验和 HTTP 响应适配，再把与框架无关的命令交给 service。目标形态如下：

```python
@router.post('/proactive_chat')
async def proactive_chat(request: Request):
    _validate_local_mutation_request(request)
    data = await _read_json_object(request)
    command = ProactiveChatCommand.from_payload(data)
    result = await proactive_service.handle(command)
    return JSONResponse(result.body, status_code=result.status_code)
```

`ProactiveChatCommand`、`ProactiveChatResult` 优先定义在 `contracts.py`；如果某个类型只服务于编排内部，也可以就近定义在 `service.py`。不为仅有一两个类型过早新增 `models.py`。

Service 的纯业务 helper 不得接收 FastAPI 对象；只有 Router 适配函数可以操作请求、响应头和 HTTP 状态码。

## 重构前模块到落地模块的映射

| 重构前内容 | 落地归属 | 迁移说明 |
|---|---|---|
| `proactive_parsing.py` 中 reason/stage 常量和响应构造 | `contracts.py` | 先迁移并由旧模块 re-export。 |
| Phase 1 结果解析、PASS sentinel、screen/intent 标签清理 | `generation.py` | 与模型输出保护放在一起。 |
| `proactive_history.py` 中搭话历史、素材历史、计数、相似度 | `state.py` | 新模块成为唯一 canonical owner。 |
| `proactive_sources.py` 中 source 历史加载和持久化 | `state.py` | 不再跨模块直接读取可变全局字典。 |
| `proactive_sources.py` 中权重、过滤和跳过决策 | `decisions.py` | 只通过 state 查询接口读取历史。 |
| `proactive_content.py` 中 Prompt 输入格式化 | `generation.py` | 仅迁移主动搭话专属格式化；通用抓取仍留在 `utils/`。 |
| `proactive_content.py` 中纯日志辅助 | `content_logging.py` | Router 旧路径保留兼容门面；不得记录原始隐私对话到 logger。 |
| `mini_game_invite.py` 中邀请状态和业务规则 | `main_logic/proactive_chat/mini_game_invite.py` | HTTP 路由仍留在 Router。 |
| music source、约束、链接和 played-through 处理 | `music_recommendation.py` | 底层抓取继续调用 `utils.music_crawlers`。 |
| locale resolution、follow-up topic hooks | `decisions.py` 或 `generation.py` | 按“决定是否/选什么”与“生成什么”分配，并增加直接测试。 |
| meme moderation、vision staging | `generation.py` 与通用 `utils` | 领域策略下沉，通用图像处理不迁移。 |
| `break_reminders.py` | `main_logic/proactive_chat/break_reminders.py` | 领域实现下沉；Router 只注入配置和保留旧入口。 |
| `proactive_chat_flow.py` 主流程 | `service.py` | 分阶段抽取，最后将 Router 收口为薄适配器。 |
| delivery commit、成功后记录和生命周期结束 | `delivery.py` | 与底层 `main_logic/proactive_delivery.py` 分层：前者拥有主动搭话阶段，后者仍拥有通用投递队列。 |

## 模块边界

| 内容 | 目标归属 | 说明 |
|---|---|---|
| token、语言、JSON 原子读写、日志、内部 HTTP client | `utils/` | 多业务复用的基础设施。 |
| web/music/meme/screenshot 抓取和处理 | `utils/` | 通用采集能力，不决定主动搭话策略。 |
| HTTP 路由、CSRF、Request/JSONResponse、响应头 | `main_routers/system_router/` | Router 只保留协议和框架边界。 |
| `/api/proactive/*` 设置路由 | `main_routers/proactive_router.py` | 现有公开 API 和字段语义不变。 |
| reason_code、stage、action、命令和领域结果 | `contracts.py` | 主动搭话协议与领域契约。 |
| 搭话历史、素材历史、source 历史、计数持久化 | `state.py` | 主动搭话状态的唯一所有者。 |
| gate、source 权重和选择、PASS 判定 | `decisions.py` | 决定本轮是否继续及使用哪些来源。 |
| Phase 1/2、输出清理、防泄漏、防复读保护 | `generation.py` | 生成链路与模型输出保护。 |
| 小游戏邀请 | `mini_game_invite.py` | 领域状态和规则；HTTP 入口留在 Router。 |
| 音乐主动搭话 | `music_recommendation.py` | 领域选择和反馈；底层抓取复用 `utils`。 |
| anti-slack 与休息提醒 | `break_reminders.py` | 领域规则和投递子流程；配置实例由 Router 注入。 |
| 主动搭话内容日志 | `content_logging.py` | 仅记录既有 source 诊断信息，不扩大隐私日志。 |
| 投递提交、成功后记录、生命周期结束 | `delivery.py` | 只在 commit 成功后更新历史和计数。 |
| 主流程编排 | `service.py` | 串联各阶段，不包含 HTTP 框架对象。 |
| 投递队列与提交 | `main_logic/proactive_delivery.py` | 保持独立，由 service 调用。 |
| 生命周期状态机 | `main_logic/session_state.py` | 保持独立，由 service 调用。 |
| 前端 timer、leader election、截图采集、source cards | `static/app/app-proactive.js` | 第一阶段保持兼容。 |
| 插件 `proactive_message` 协议 | `plugin/server/messaging/proactive_bridge.py` 与 `app/main_server.py` | 不并入主动搭话包。 |

## 状态所有权与兼容 re-export

迁移期间必须避免双份状态：

- `state.py` 成为搭话历史、素材历史、source 历史、计数和锁的唯一 canonical owner。
- 旧 Router 模块只能 re-export 同一个可变对象，或用薄包装把 Router 注入依赖传给 canonical 函数；不得复制一份字典、deque、锁、loaded flag 或持久化算法。
- 不得在旧模块和新模块分别加载或持久化同一份 JSON 文件。
- `state.py` 的持久化 API 接受可选 `memory_dir`：主流程显式传入 Router 的配置实例目录，直接领域调用保留共享 singleton fallback；这不是多租户或运行时热切换存储根目录能力。
- `decisions.py` 不直接 import `state.py` 的可变全局字典，应通过查询函数或只读快照访问。
- 迁移后测试应优先 patch 实际消费依赖的领域模块，而不是 patch `system_router.__init__` 的快照式 re-export。
- 对布尔 loaded flag 等会重新绑定的值，不承诺通过旧门面保持实时同步；相关调用方应迁移为查询函数。

兼容 re-export 只是过渡机制。每一轮迁移都要记录旧符号的剩余调用方，并在后续 PR 中逐步清理，不能把 `system_router/__init__.py` 永久变成全仓内部 API。

## 公开协议与兼容契约

### HTTP API

重构期间必须保持：

- `POST /api/proactive_chat`
- `POST /api/proactive/music_played_through`
- `POST /api/mini_game/invite/respond`
- `GET /api/proactive/mode`
- `POST /api/proactive/mode`
- `GET /api/proactive/settings`
- `POST /api/proactive/settings`

除 URL 和方法外，还必须保持：

- 成功、PASS、校验失败和运行异常对应的 HTTP 状态码。
- CSRF、本地请求校验和远程部署限制行为。
- `Cache-Control` 等现有 no-store 响应头。
- `/api/proactive_chat` 返回体中的 `action`、`reason_code`、`stage`、`message`、`source_links`。
- 小游戏反馈接口中的 `session_id`、`action`、`game_type`、`launch_url` 等现有字段和错误语义。

所有 API URL 继续遵守无末尾斜杠约定。

### 前端 API

必须继续导出：

- `window.scheduleProactiveChat`
- `window.resetProactiveChatBackoff`
- `window.appProactive`

### WebSocket 与插件事件

必须保持：

- WebSocket 事件 `mini_game_invite_options`
- WebSocket 事件 `mini_game_invite_resolved`
- 插件事件 `proactive_message`

事件的 `type`、`session_id`、`action`、`game_type`、`launch_url`、`options` 等已存在字段应纳入契约测试；可选字段仍保持现有可选性，不得在重构中擅自改为必填。

### 设置字段

主动搭话设置继续以当前字段为准：

```text
proactiveChatEnabled
proactiveVisionEnabled
proactiveVisionChatEnabled
proactiveNewsChatEnabled
proactiveVideoChatEnabled
proactivePersonalChatEnabled
proactiveMusicEnabled
proactiveMemeEnabled
proactiveMiniGameInviteEnabled
proactiveChatInterval
proactiveVisionInterval
```

`proactiveVisionEnabled` 是用户专有字段，语义上是前端“隐私模式”开关的反面。主动搭话 preset 和 `/api/proactive/*` 写路径不得覆盖它。

## 行为不变量

以下语义必须在每一个迁移 PR 中保持：

- 所有主动搭话 `reason_code -> stage/action` 映射稳定且可穷举验证。
- 并发抢占、用户打断、delivery busy、route active、voice fast path 行为不变。
- `PROACTIVE_START` 成功后，每条退出路径最终都且只触发一次等价的 `PROACTIVE_DONE` 清理。
- delivery commit 成功后才能写入主动搭话历史、素材历史和成功计数。
- 投递失败、抢占或生成失败不得污染历史、计数、topic usage 或 anti-repeat 语料。
- 小游戏邀请只有成功投递后才写入主动搭话历史并更新计数。
- 小游戏 pending、回应、冷却和跨窗口 resolved 广播语义不变。
- 音乐完整播放反馈只清理 history 中 `channel == "music"` 的通道标记，不删除历史文本。
- `ignored`、`mini_game_ignored` 等反馈只作为报告层压力信号，不在本次重构中变成即时自动降权。
- Phase 1 PASS、Phase 2 空输出、标签泄漏、重复文本和超时的返回语义不变。

生命周期和投递清理应逐步收敛到 service 的统一 `finally`、幂等结束函数或异步上下文管理器中，避免迁移后继续依靠大量分支手动清理。

## 迁移路线

### 0. 固化基线与依赖清单（已完成）

实施任何移动前：

- 记录当前主动搭话相关模块、路由、前端调用、WebSocket 事件和插件事件。
- 为所有 `reason_code`、`stage`、`action` 建立参数化契约测试。
- 为 HTTP 状态码、CSRF、no-store header 和小游戏反馈接口建立路由契约测试。
- 执行完整目标测试集，保存基线结果。
- 明确每个旧内部符号的实际调用方，区分运行时兼容和仅测试兼容。

### 1. 迁移协议契约（已完成）

先建立 `contracts.py`，迁移：

- `_proactive_stage_for_reason`
- `_proactive_response_body`
- `_proactive_pass_body`
- `_proactive_chat_body`
- `_proactive_error_body`
- `_ensure_proactive_reason_code`
- 全部 `PROACTIVE_REASON_*`、`PROACTIVE_STAGE_*` 及映射表

`proactive_parsing.py` 和 `system_router/__init__.py` 在过渡期 re-export 新实现。契约模块不得依赖 Router、状态、生成或投递模块。

### 2. 迁移状态与历史（已完成）

建立 `state.py`，迁移：

- 近期主动搭话记录、格式化和相似度判断。
- 素材 key、素材历史和素材级近期去重。
- source 使用历史、加载、持久化和查询。
- 成功投递计数、ever-delivered 标记及原子持久化。
- reminiscence usage 独立缓冲。
- music channel 标记清理。

状态迁移必须是“移动 canonical owner”，不能复制状态。旧模块先改为导入和 re-export 新对象，再迁移调用方。异步路径中的文件 I/O 继续使用异步原子读写或 `asyncio.to_thread`，不得引入同步阻塞。

### 3. 迁移决策与生成保护（已完成）

建立 `decisions.py` 和 `generation.py`：

- `decisions.py`：入口 gate、activity gate、source 权重、source 过滤、PASS 判定、locale/topic hook 选择。
- `generation.py`：Phase 1/2 调用、模型结果解析、Prompt 输入格式化、screen tag / intent label 清理、meme/vision 保护、anti-repeat 和输出 fence。

模型调用继续遵守现有模型 tier、输入 token budget、输出 budget、timeout 和不显式设置 temperature 的仓库规范。Prompt 文案仍留在 `config/prompts/prompts_proactive.py`。

### 4. 迁移小游戏邀请与音乐推荐（已完成）

建立 `mini_game_invite.py` 与 `music_recommendation.py`：

- 小游戏模块拥有邀请状态、命中、pending、回应、冷却、关键词选择和成功投递后的状态更新。
- 音乐模块拥有 source 选择、严格约束、播放链接返回和 played-through 反馈语义。
- `POST /api/mini_game/invite/respond` 和 `POST /api/proactive/music_played_through` 仍由 Router 声明并适配领域结果。
- WebSocket payload 可以由领域模块构造纯字典，但实际发送由注入的 collaborator 或外层适配器完成。

### 5. 分阶段收口主流程（已完成）

不要一次性搬运整个 `proactive_chat` 大函数。应先从当前函数逐段抽出可直接测试的阶段函数：

```text
parse command
entry guards
activity gate
source selection
mini-game short-circuit
phase1 decision
phase2 generation
dedup / text guard
delivery commit
record history / metrics
finalize lifecycle
```

阶段函数先由现有 Router 流程调用；待 Router 只剩编排后，再将编排整体移动到 `service.py`。迁移期间不得让新的 `main_logic` service 反向调用旧 Router 流程，以免形成逆向依赖或循环 import。

### 6. 收缩 Router 与兼容门面（已完成，兼容门面按调用方渐进清理）

Service 稳定后：

- `proactive_chat_flow.py` 只保留 HTTP 入口、请求读取、校验和响应适配。
- `mini_game_invite.py` 只保留小游戏反馈路由和响应适配。
- 将测试和运行时调用切换到新的 canonical 模块。
- 清理 `system_router/__init__.py` 中不再需要的内部 re-export。
- 对仍需过渡的 re-export 建立清单和删除条件。

### 7. 前端后续拆分（独立排期，未纳入本阶段）

前端拆分不作为第一阶段验收条件。后续如需拆分 `static/app/app-proactive.js`，建议在 `static/app/proactive/` 下建立内部模块：

```text
static/app/proactive/
  leader.js
  scheduler.js
  sources.js
  transport.js
  attachments.js
  vision.js
```

无论内部如何拆分，`static/app/app-proactive.js` 都继续作为兼容门面并导出当前 `window.*` API。

## 测试与验证

每轮迁移至少运行：

```bash
uv run python -m pytest \
  tests/unit/test_proactive_material_dedup.py \
  tests/unit/test_proactive_intent_label_leak.py \
  tests/unit/test_music_played_through_reset.py \
  tests/unit/test_mini_game_invite.py \
  tests/unit/test_proactive_phase1_pass.py \
  tests/unit/test_reflection_synthesis_loop.py \
  tests/unit/test_session_state.py \
  tests/unit/test_proactive_delivery.py \
  tests/unit/test_proactive_sm_integration.py \
  tests/unit/test_proactive_sid_guard.py \
  tests/unit/test_proactive_vision_screenshot_staging.py \
  tests/unit/test_proactive_interval_20s_rollback.py \
  tests/unit/test_proactive_agent_trigger.py \
  tests/unit/test_proactive_action_note.py \
  tests/unit/test_proactive_text_does_not_dehumanize.py \
  tests/unit/test_proactive_meme_moderation_static.py \
  tests/unit/test_system_router_topic_hooks.py

node static/app-proactive.test.cjs
uv run python scripts/check_api_trailing_slash.py
uv run python scripts/check_prompt_hygiene.py
uv run python scripts/check_llm_budget.py
git diff --check
```

2026-07-21 本地验收记录：

- 持久化注入、投递记录、小游戏短路和 Service/Router 边界定向集共 56 项通过。
- 除 `test_proactive_agent_trigger.py` 外的主动搭话测试共收集 506 项；拆批执行前 502 项通过，剩余 4 项被既有 vision screenshot commit 测试在当前 Python 3.12 兼容运行环境中挂起阻断。
- `test_proactive_agent_trigger.py` 因当前环境缺少为 Python 3.11 构建的 `ormsgpack` 二进制扩展而无法收集；该文件和上述 vision 测试均不在本轮修改范围内。
- 前端 Node 契约、API 尾斜杠、Prompt hygiene、LLM budget、Ruff、语法编译和 diff whitespace 检查通过。

如相关检查脚本的实际 CLI 需要额外参数，以仓库脚本帮助信息为准，但不得跳过对应检查类别。

应补充的测试：

- 参数化覆盖全部 `reason_code -> stage/action` 映射。
- 路由级验证 HTTP 状态码、CSRF、本地请求限制和 no-store header。
- 小游戏 HTTP 与 WebSocket payload 契约测试。
- `PROACTIVE_START/PROACTIVE_DONE` 在正常、PASS、异常、超时、抢占路径上的配对测试。
- 并发投递抢占、SID 不匹配、用户打断和 delivery commit 失败测试。
- 状态 canonical owner 和兼容 re-export 身份一致性测试。

## 验收标准与结果

以下第一阶段验收项均已由实现和定向回归覆盖；兼容门面清理与前端拆分不阻塞本阶段完成：

- Router 不再拥有主动搭话领域状态和策略。
- `main_logic/proactive_chat/` 不导入 FastAPI 或 `main_routers.system_router`。
- 所有主动搭话 `reason_code`、`stage`、`action` 保持稳定。
- `/api/proactive_chat` 的 pass、chat、error、timeout、preempted 行为不变。
- HTTP 状态码、CSRF、no-store header 和无末尾斜杠契约不变。
- 并发抢占、用户打断、delivery busy、route active、voice fast path 行为不变。
- 音乐完整播放反馈仍只清理 music channel 标记，不删除历史文本。
- 小游戏邀请仍只在成功投递后计入主动搭话历史和计数。
- 小游戏反馈路由和 `mini_game_invite_options/resolved` 事件流不变。
- 前端仍通过原有 `window.*` API 调度主动搭话。
- 插件 `proactive_message` 事件流不变。
- 状态、锁和持久化加载器不存在新旧两份实现。
- 可在当前环境完成的目标测试、Node 测试、项目检查和 `git diff --check` 通过；环境阻断项按上方本地验收记录保留，不误记为通过。

## PR 切分与回滚

建议按以下 PR 切分：

1. 契约测试与 `contracts.py`。
2. `state.py` 及旧路径 re-export。
3. `decisions.py`、`generation.py` 和对应测试迁移。
4. `mini_game_invite.py`、`music_recommendation.py` 与路由适配。
5. `service.py`、生命周期统一收口和薄 Router。
6. 兼容 re-export 清理。
7. 前端内部拆分（独立排期）。

每个 PR 只改变一类 canonical owner，并保持旧入口可运行。出现问题时优先回滚当前领域模块或恢复旧适配调用，不通过复制状态实现临时双写。

由于改动涉及 `main_logic/`，PR 描述必须按仓库规范填写非空“回归报告”，说明改动、必要性、前后表现和潜在回归点；若单个 PR 超过仓库文件数阈值，还需填写“不拆分理由”。

## 维护原则

- 不把主动搭话业务逻辑放进 `utils/`。
- 不引入新的顶层 `proactive_chat/` 包。
- 不在模块迁移中重调主动搭话策略。
- 不把 Prompt 文案迁出 `config/prompts/prompts_proactive.py`。
- 不把 HTTP 框架对象传入领域 helper 或 service。
- 不让领域层反向 import Router。
- 不跨模块直接读取或修改可变状态字典，优先使用窄查询和更新接口。
- 不在异步主动搭话链路中引入同步文件 I/O、同步 HTTP 或阻塞等待。
- 对外协议优先兼容；内部 re-export 允许过渡，但必须有清理条件。
- 新 source 或 gate 进入 `decisions.py`，新生成保护进入 `generation.py`；小游戏邀请和音乐推荐规则分别进入对应的具名子能力模块。
- 新增退出分支必须显式经过统一生命周期结束和投递清理。

## 重构前后对比

| 维度 | 重构前 | 重构后 |
|---|---|---|
| 主流程可读性 | Router 已按领域拆包，但主动搭话主流程仍集中在约 2933 行的 `proactive_chat_flow.py`。 | Router 只保留协议适配；`service.py` 串联显式阶段。 |
| 职责边界 | Router 子模块仍同时承担 HTTP、领域状态、策略和生成编排。 | Router、领域逻辑、通用工具、Prompt、投递阶段和状态机各自归位。 |
| 状态所有权 | 历史和 source 模块之间存在对可变全局状态的直接引用。 | `state.py` 是唯一所有者；主流程显式传递持久化根目录，旧 facade 共享同一状态对象。 |
| 子能力规模 | 小游戏路由、状态、业务规则和 WebSocket payload 集中在同一 Router 文件。 | 小游戏、音乐、休息提醒、内容日志和投递阶段拥有具名领域模块，Router 只做协议适配。 |
| 测试方式 | 部分测试仍依赖 Router 内部函数或兼容门面。 | 领域规则直接单测，Router 保留协议、注入和兼容契约测试。 |
| 行为稳定性 | 大流程新增分支时容易遗漏 reason、stage、历史、计数或生命周期清理。 | 契约、状态、投递提交和生命周期结束均由明确边界统一处理，并以重构前 diff 回查意外变化。 |
| 前端兼容 | `static/app/app-proactive.js` 承担调度、采集、传输和兼容 API。 | 第一阶段保持不变；后续内部拆分仍由原文件导出 `window.*` API。 |
| 回滚成本 | 主流程移动容易牵动多个 Router 子模块和共享状态。 | 按 canonical owner 小步迁移，每个 PR 可独立回滚且不双写状态。 |

整体效果：用户侧行为和全部外部协议保持不变；维护侧从“在 Router 子模块和大流程中定位分支”转为“按主动搭话领域模块定位契约、状态、决策、生成、具名子能力和编排”。
