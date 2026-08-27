# Yui 星露谷驱动插件

让 N.E.K.O 通过 HimiYui 的本机鉴权桥驱动当前玩家唯一的 Yui。插件提供两个 LLM 工具：

- `yui_stardew_status`：只读查看连接、生命周期、身体、模式、工作与 Agent 状态；
- `yui_stardew_action`：提交召唤、召回、跟随、等待、停止和白名单持续工作。

## 配置

1. 启动过一次 HimiYui，在其 `config.json` 中合并以下字段：

   ```json
   {
     "EnableNekoBridge": true,
     "NekoBridgePort": 48910,
     "NekoBridgeToken": "请替换为至少16字符的本机随机密钥"
   }
   ```

2. 在 N.E.K.O 插件运行配置的 `[yui_stardew]` 中设置相同端口与密钥：

   ```toml
   [yui_stardew]
   port = 48910
   token = "与 HimiYui 完全相同的本机随机密钥"
   connect_timeout_seconds = 3.0
   request_timeout_seconds = 6.0
   ```

3. 启动 Stardew Valley 并进入房主存档，再从插件管理器启动 `yui_stardew`。修改运行配置后调用 `yui_stardew_reload_config`，或重启插件。

仓库不提供默认密钥。不要把真实密钥提交到 Git、发送到聊天、写进角色卡或分享配置。两端都只使用 `127.0.0.1:48910`；HimiYui 不监听局域网地址。

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

插件不会接受 Owner、Slot、地点或坐标。HimiYui 使用房主当前事实补齐这些字段，并在 SMAPI 主线程进入原有 Host 权威命令、任务和事务入口。工具返回的 `STARTED` 或 `ACCEPTED` 只表示请求已接收，不表示工作已经完成。
