# Yui 星露谷驱动插件

让 N.E.K.O 通过 YuiToIssho 的本机 WebSocket Agent Gateway 驱动当前角色自己的星露谷化身。Gateway 也可以供遵守同一公开协议的其他本机 Agent 宿主使用。本插件提供三个 LLM 工具：

- `stardew_avatar_status`：只读查看连接、生命周期、身体、模式、工作与 Agent 状态；
- `stardew_avatar_view`：按需查看游戏身体周围 12 格内的有界分类摘要；
- `stardew_avatar_action`：提交召唤、召回、跟随、等待、停止和白名单持续工作。

无论当前 NEKO 角色名是什么，游戏内固定显示为 Yui 的角色都是当前 NEKO 角色本人在星露谷中的化身，不是另一个同名 NPC，也不会替换当前人格。主人用当前角色名、Yui、“你”或“游戏里的你”下令时，角色必须先调用真实游戏工具，再以当前人格和第一人称回应；括号动作和文字演出不能冒充游戏执行。

## 首次安装（Windows + Steam）

插件随附经过配套验证的 YuiToIssho 模组，无需另外下载或手动复制模组。首次安装需要在运行 N.E.K.O 的同一台 Windows 电脑、同一系统用户下操作：

1. 在 N.E.K.O 插件管理器安装并启动“Yui 星露谷驱动”，打开插件面板。
2. 面板自动寻找 Steam 游戏目录。存在多个位置时选择目标；未找到时，在路径输入框填入包含 `Stardew Valley.exe` 的 Steam 游戏目录，点击“检查目录”。操作目标显示在“当前操作目录”中。
3. 如果提示缺少 SMAPI，打开面板中的 [SMAPI 官网](https://smapi.io/)链接，下载并运行 Windows 安装器，按提示安装到当前游戏目录。完成后点击“刷新连接”。插件不会静默安装 SMAPI。
4. 退出游戏，点击“安装并连接”。插件将配套模组安装到 `Mods/YuiToIssho` 并开启桥接。操作结果会显示安装说明；有备份时同时显示备份位置。
5. 点击“启动游戏”，进入单人或自己作为房主的存档，等待面板显示“已连接”。启动请求成功仅表示已发出启动请求，实际连接和游戏世界就绪以面板诊断为准。
6. 直接告诉 N.E.K.O：`让 Yui 跟着我`、`看看你在星露谷里的状态` 或 `让 Yui 停止工作`。

已有 YuiToIssho 时，也可在退出游戏后点击“开启桥接”。无需填写密钥或端口；YuiToIssho 首次运行时会自动生成本机密钥。插件可在运行时配置中开启自动启动。

### 升级、配置与备份

“安装并连接”只安装随插件附带的配套版本，不会联网获取未经配套验证的最新模组。升级时保留原有 `config.json`、其中的密钥及其他配置，并保留模组目录中的额外文件；仅将 `EnableNekoBridge` 开启。原有 `EnableExperimentalFeatures` 设置保持不变。

已安装同一配套版本且桥接已开启时，按钮显示为“检查/修复安装”，可重新应用配套文件。

替换已有模组或修改已有配置前会在游戏目录的 `.yuitoissho-backups` 下保存完整模组目录（位于 `Mods` 外，避免被 SMAPI 当成重复模组加载）。操作结果显示本次备份位置。安装准备期间若配置或额外文件发生变化，会停止替换并保留最新文件，待其他修改结束后重试。激活新文件失败时会自动恢复原目录；若文件占用导致自动恢复也失败，错误信息会明确给出原安装的完整备份路径。

手动恢复时先关闭游戏和 SMAPI，再将当前 `Mods/YuiToIssho`（若存在）移到 `Mods` 外的另一个目录保留；将所选备份目录整体复制为 `Mods/YuiToIssho`。确认 `manifest.json`、`YuiToIssho.dll`、`config.json` 等直接位于该目录内，不要复制成额外一层子目录，也不要将旧文件覆盖合并到新安装中，以免残留新版文件。原备份应继续保留；恢复会同时恢复备份时的配置、桥接开关和密钥。若备份不是当前插件配套版本，可先在面板重新安装配套模组，再启动游戏。

若检测到更高版本、重复模组、损坏的配置或游戏正在运行，按照面板诊断处理后再重试，不要强行覆盖。

面板同时检查 SMAPI、已安装模组版本、桥接开关和游戏运行状态。安装完成但尚未连接时，先确认通过 SMAPI 启动了游戏并进入房主存档，再查看“连接诊断”。

持续工作动作 `work`、`work_stop`、`work_resume` 还要求游戏端将 `EnableExperimentalFeatures` 设为 `true`；召唤、跟随、等待与挥工具协作无需此开关。

YuiToIssho 会在固定的当前用户目录发布一份不含密钥的 v2 实例描述文件，其中包含动态回环 WebSocket 端点。插件读取描述文件指向的 YuiToIssho 自有配置，再用鉴权 `status` 验证实例；即使 N.E.K.O 先启动，也会在面板刷新或工具调用时重新发现。

`endpoint` 与 `token` 只用于成对的手动诊断覆盖，正常使用请保持为空。旧版 `himi_yui_config_path` 或固定端口不能定位动态 Gateway，也不会触发旧协议回退。面板中的游戏目录仅用于安装、配置与启动游戏，不用于绕过桥接鉴权。覆盖生效时可一键清除并恢复自动发现。不要分享或提交自动生成的真实密钥。

插件管理器的 Hosted UI 提供安装、桥接配置、启动游戏、连接诊断与游戏身体状态。安装区域会显示当前游戏目录，以便确认操作目标；面板不会显示密钥。发现多个已鉴权实例时可以选择本次会话目标。召唤、跟随、工作等游戏动作仍只通过 N.E.K.O 对话入口点执行。

在已经明确讨论星露谷或 YuiToIssho 的上下文中，“可以进了”“进来吧”“现在能进了”等进入许可会映射为 `summon`。对话模型应直接调用 `stardew_avatar_action`；若它只承诺进入却漏掉工具调用，Agent 兜底入口会先检查世界、身体和最近召唤状态，再进行一次幂等补偿。与游戏无关的“我弄好了”不会触发。

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
| `assist_start` | 无 | 开启一次后，玩家挥斧时协作砍伐、挥镰时协作割草 |
| `assist_status` | 无 | 查看协作授权与当前触发状态 |
| `assist_stop` | 无 | 关闭协作授权并停止未提交的协作工作 |

`kind` 可选 `Water`、`Harvest`、`Mow`、`Till`、`Chop`、`Mine`、`Forage`、`Pet`、`Milk`、`Shear`；`radius` 为 1～24；策略为 `until-clear` 或 `until-stopped`。

当主人要求“跟我一起砍树/割草”时使用 `assist_start`，而不是固定范围的 `work`。开启后无需重复调用：YuiToIssho 会等待玩家真实挥斧或挥镰，再在玩家附近使用 Yui 自己的工具执行相同类型工作；只装备工具不会触发。

YuiToIssho 会保护已形成的巨大作物、仍有巨大化可能的普通作物，以及公开作物数据中不属于标准作物成就集合的特殊作物。Viewer 返回 `GIANT-CROP-PROTECTED`、`GIANT-CROP-POTENTIAL` 或 `SPECIAL-CROP-PROTECTED` 时，插件必须把它解释成主动保护，不能当成遗漏、建议绕过或声称已经收获。

插件不会接受 Owner、Slot、地点或坐标。YuiToIssho 使用房主当前事实补齐这些字段，并在 SMAPI 主线程进入原有 Host 权威命令、任务和事务入口。工具返回的 `STARTED` 或 `ACCEPTED` 只表示请求已接收，不表示工作已经完成。

## 维护内附模组与构建安装包

在 N.E.K.O 仓库根目录，使用已构建、验证过的 YuiToIssho 发布包及其许可证更新内附资源；脚本保存固定版本和 SHA-256，不会联网下载或携带本机配置：

```bash
uv run python scripts/bundle_yui_stardew.py --archive '/path/to/YuiToIssho 0.31.0.zip' --license '/path/to/HimiYui/LICENSE'
uv run neko-plugin build yui_stardew --out dist/yui_stardew-0.1.4.neko-plugin
```

升级内附模组时需同步验证协议、游戏与 SMAPI 兼容范围，并更新插件版本。安装功能使用 N.E.K.O 宿主已有的运行依赖，不会为用户另行安装 Python 或依赖包。
