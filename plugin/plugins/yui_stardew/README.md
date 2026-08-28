# Yui 星露谷驱动插件

让 N.E.K.O 通过 HimiYui 的本机鉴权桥驱动当前角色自己的星露谷化身。插件提供三个 LLM 工具：

- `stardew_avatar_status`：只读查看连接、生命周期、身体、模式、工作与 Agent 状态；
- `stardew_avatar_view`：按需查看游戏身体周围 12 格内的有界分类摘要；
- `stardew_avatar_action`：提交召唤、召回、跟随、等待、停止和白名单持续工作。

无论当前 NEKO 角色名是什么，游戏内固定显示为 Yui 的角色都是当前 NEKO 角色本人在星露谷中的化身，不是另一个同名 NPC，也不会替换当前人格。主人用当前角色名、Yui、“你”或“游戏里的你”下令时，角色必须先调用真实游戏工具，再以当前人格和第一人称回应；括号动作和文字演出不能冒充游戏执行。

## 配置

无需手动配置密钥或端口：

1. 启动 Stardew Valley 并进入一次房主存档；HimiYui 会自动生成本机密钥。
2. 启动 N.E.K.O；“Yui 星露谷驱动”会自动启动并发现 HimiYui。
3. 直接告诉 N.E.K.O：`让 Yui 跟着我`、`让 Yui 浇附近的田` 或 `让 Yui 停止工作`。

HimiYui 会在固定的当前用户目录发布一份不含密钥的实例描述文件。插件读取描述文件指向的 HimiYui 自有配置，再用鉴权 `status` 验证实例；即使 N.E.K.O 先启动，也会在面板刷新或工具调用时重新发现。迁移期仍保留常见 Steam 安装扫描作为最后兜底。

`himi_yui_config_path`、`port` 与 `token` 只保留为旧版兼容覆盖，正常使用请保持为空。Hosted UI 不再提供路径输入或文件选择；旧覆盖生效时只允许一键清除并恢复自动发现。不要分享或提交自动生成的真实密钥。

插件管理器的 Hosted UI 只显示桥连接与游戏身体状态。发现多个已鉴权实例时可以选择本次会话目标；面板不会显示密钥或默认展示完整路径，也不会提供召唤、跟随、工作等游戏动作按钮，这些动作仍只通过 N.E.K.O 入口点执行。

## 动作

| action | 参数 | 含义 |
|---|---|---|
| `summon` | 无 | 召唤当前玩家唯一 Yui |
| `recall` | 无 | 召回 Yui |
| `follow` | 无 | 切换跟随 |
| `wait` | 无 | 原地等待 |
| `stop` | 无 | 停止当前行动并等待 |
| `work` | `kind`, `radius`, `completion_policy` | 以玩家当前格为圆心开始持续工作 |
| `work_stop` | 无 | 停止持续工作 |
| `work_resume` | 无 | 从新观察边界恢复已有工作 |

`kind` 可选 `Water`、`Harvest`、`Mow`、`Till`、`Chop`、`Mine`、`Forage`、`Pet`、`Milk`、`Shear`；`radius` 为 1～24；策略为 `until-clear` 或 `until-stopped`。

HimiYui 会保护已形成的巨大作物、仍有巨大化可能的普通作物，以及公开作物数据中不属于标准作物成就集合的特殊作物。Viewer 返回 `GIANT-CROP-PROTECTED`、`GIANT-CROP-POTENTIAL` 或 `SPECIAL-CROP-PROTECTED` 时，插件必须把它解释成主动保护，不能当成遗漏、建议绕过或声称已经收获。

插件不会接受 Owner、Slot、地点或坐标。HimiYui 使用房主当前事实补齐这些字段，并在 SMAPI 主线程进入原有 Host 权威命令、任务和事务入口。工具返回的 `STARTED` 或 `ACCEPTED` 只表示请求已接收，不表示工作已经完成。
