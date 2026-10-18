import {
  Alert,
  Button,
  Card,
  Field,
  Grid,
  Inline,
  Input,
  KeyValue,
  Page,
  RefreshButton,
  Select,
  Stack,
  StatCard,
  StatusBadge,
  Text,
  Tip,
  useEffect,
  useState,
  useToast,
} from "@neko/plugin-ui"
import type { HostedAction, PluginSurfaceProps } from "@neko/plugin-ui"

type Candidate = {
  instance_id?: string
  label?: string
  transport?: string
  source?: string
  world_ready?: boolean
  host_authoritative?: boolean
}

type Discovery = {
  state?: string
  code?: string
  message?: string
  source?: string
  transport?: string
  instance_id?: string
  verified_at?: string
  candidates?: Candidate[]
  compatibility_override?: boolean
  open_config_directory?: string
}

type Connection = {
  reachable?: boolean
  ok?: boolean
  code?: string
  message?: string
}

type AvatarState = {
  world_ready?: boolean
  host_authoritative?: boolean
  companion_exists?: boolean
  body_present?: boolean
  mode?: string
  location?: string
  behavior?: string
  brain_phase?: string
  work_kind?: string
  work_state?: string
  vital_state?: string
}

type SetupState = {
  supported?: boolean
  code?: string
  message?: string
  games?: Array<{ path: string; label: string }>
  selected_path?: string
  installed_version?: string
  bundled_version?: string
  smapi_installed?: boolean
  bridge_enabled?: boolean
  game_running?: boolean
  can_install?: boolean
  can_enable_bridge?: boolean
  can_launch?: boolean
  backup_available?: boolean
}

type SetupResult = {
  ok?: boolean
  message?: string
  backup_path?: string
}

type DashboardState = {
  protocol_version?: number
  discovery?: Discovery
  connection?: Connection
  avatar?: AvatarState
  setup?: SetupState
  ui_token?: string
}

const SOURCE_LABELS: Record<string, string> = {
  discovery_v2: "YuiToIssho 自动发现",
  manual_credentials: "手动 WebSocket 覆盖",
  compatibility_override: "兼容覆盖",
  unconfigured: "等待发现",
}

const STATE_LABELS: Record<string, string> = {
  ready: "已连接",
  discovering: "正在发现",
  not_found: "等待启动",
  offline: "桥尚未响应",
  ambiguous: "需要选择实例",
  auth_failed: "认证失败",
  invalid: "发现信息无效",
  disabled: "桥未启用",
}

function hasAction(actions: HostedAction[], id: string): boolean {
  return actions.some((action) => action.id === id || action.entry_id === id)
}

function yesNo(value: boolean | undefined): string {
  if (value === undefined) return "未知"
  return value ? "是" : "否"
}

function actionResult(envelope: any): Record<string, any> {
  if (!envelope || typeof envelope !== "object") return {}
  return envelope.result && typeof envelope.result === "object"
    ? envelope.result
    : envelope
}

function openLocalPath(path: string): void {
  if (!path) return
  window.parent.postMessage(
    { type: "neko-hosted-surface-open-path", payload: { path } },
    "*",
  )
}

export default function YuiStardewPanel(
  props: PluginSurfaceProps<DashboardState>,
) {
  const state = props.state || {}
  const discovery = state.discovery || {}
  const connection = state.connection || {}
  const avatar = state.avatar || {}
  const setup = state.setup || {}
  const uiToken = state.ui_token || ""
  const candidates = Array.isArray(discovery.candidates)
    ? discovery.candidates.filter(
        (candidate) => typeof candidate.instance_id === "string" && candidate.instance_id,
      )
    : []
  const actions = props.actions || []
  const canSelect =
    !!uiToken && hasAction(actions, "yui_stardew_select_instance")
  const canClear =
    !!uiToken &&
    !!discovery.compatibility_override &&
    hasAction(actions, "yui_stardew_clear_compatibility_override")
  const [selecting, setSelecting] = useState("")
  const [clearing, setClearing] = useState(false)
  const [setupAction, setSetupAction] = useState("")
  const [gamePath, setGamePath] = useState("")
  const [lastSetupResult, setLastSetupResult] = useState<SetupResult | null>(null)
  const setupBusy = !!setupAction
  const games = Array.isArray(setup.games) ? setup.games : []
  const installLabel = setup.installed_version &&
    setup.installed_version === setup.bundled_version && setup.bridge_enabled
    ? "检查/修复安装"
    : "安装并连接"
  const canSetupAction = (id: string) =>
    setup.supported === true && !!uiToken && hasAction(actions, id) && !setupBusy
  const toast = useToast()

  useEffect(() => {
    // Also refresh the initial empty context and a connected game's world/body.
    // Starting the plugin or loading a save must not require a manual refresh.
    let refreshing = false
    const timer = window.setInterval(() => {
      if (refreshing) return
      refreshing = true
      void props.api.refresh().catch(() => undefined).finally(() => { refreshing = false })
    }, 5000)
    return () => window.clearInterval(timer)
  }, [props.api])

  async function runSetupAction(id: string, args: Record<string, any> = {}) {
    if (!canSetupAction(id)) return
    setSetupAction(id)
    setLastSetupResult(null)
    try {
      const timeoutMs = id === "yui_stardew_install_mod" || id === "yui_stardew_enable_bridge" ? 70000 : 30000
      const result = actionResult(await props.api.call(id, { ...args, ui_token: uiToken }, { timeoutMs }))
      const outcome: SetupResult = {
        ok: result.ok === true,
        message: typeof result.message === "string" ? result.message : "操作未返回确认结果，请刷新后检查安装状态。",
        backup_path: typeof result.backup_path === "string" ? result.backup_path : undefined,
      }
      setLastSetupResult(outcome)
      if (outcome.ok) toast.success(outcome.message)
      else toast.error(outcome.message)
      try {
        await props.api.refresh()
      } catch {
        toast.warning("状态刷新失败，请点击刷新连接查看最新结果。")
      }
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error)
      setLastSetupResult({ ok: false, message })
      toast.error(message)
    } finally {
      setSetupAction("")
    }
  }

  async function selectInstance(instanceId: string) {
    if (!canSelect || !instanceId) {
      toast.error("实例选择动作尚未可用，请刷新插件状态。")
      return
    }
    setSelecting(instanceId)
    try {
      const result = actionResult(await props.api.call("yui_stardew_select_instance", {
        instance_id: instanceId,
        ui_token: uiToken,
      }))
      if (result.selected !== true) throw new Error(result.message || "实例未切换，请刷新后重试。")
      await props.api.refresh()
      toast.success("已切换到当前游戏实例。")
    } catch (error) {
      toast.error(error instanceof Error ? error.message : String(error))
    } finally {
      setSelecting("")
    }
  }

  async function clearCompatibilityOverride() {
    if (!canClear) return
    setClearing(true)
    try {
      const result = actionResult(await props.api.call("yui_stardew_clear_compatibility_override", {
        ui_token: uiToken,
      }))
      if (result.cleared !== true) throw new Error(result.message || "手动覆盖未清除，请刷新后重试。")
      await props.api.refresh()
      toast.success("已清除手动覆盖，恢复 YuiToIssho 自动发现。")
    } catch (error) {
      toast.error(error instanceof Error ? error.message : String(error))
    } finally {
      setClearing(false)
    }
  }

  const discoveryState = discovery.state || "not_found"
  const connectionTone =
    discoveryState === "ready"
      ? "success"
      : discoveryState === "ambiguous"
        ? "warning"
        : discoveryState === "not_found" || discoveryState === "offline"
          ? "default"
          : "danger"

  return (
    <Page
      title="Yui 星露谷驱动"
      subtitle="安装配套模组、连接星露谷，在对话中与你的 Yui 一起游玩。"
    >
      <Stack>
        <Inline align="center" justify="space-between">
          <StatusBadge
            tone={connectionTone}
            label={STATE_LABELS[discoveryState] || discoveryState}
          />
          <RefreshButton label="刷新连接" />
        </Inline>

        {state.setup ? <Card title="安装并连接星露谷">
          <Stack>
            <Alert tone={setup.supported === false ? "warning" : "info"}>
              {setup.message || "正在检查 Windows Steam 游戏与配套模组。"}
            </Alert>
            {setup.supported === true ? (
              <Stack>
                {games.length > 0 ? (
                  <Field label="Steam 游戏目录" help="检测到多个安装位置时，先选择要使用的游戏。">
                    <Select
                      value={setup.selected_path || ""}
                      options={[
                        { value: "", label: "请选择游戏位置" },
                        ...games.map((game) => ({ value: game.path, label: game.label || game.path })),
                      ]}
                      disabled={!canSetupAction("yui_stardew_select_game")}
                      onChange={(value) => {
                        if (value) void runSetupAction("yui_stardew_select_game", { game_path: String(value) })
                      }}
                    />
                  </Field>
                ) : null}
                <Field
                  label={games.length ? "其他游戏目录（可选）" : "选择游戏位置"}
                  help="找不到游戏时，填入包含 Stardew Valley.exe 的 Steam 游戏目录，再点击检查目录。"
                >
                  <Inline>
                    <Input
                      value={gamePath}
                      placeholder="例如 D:\SteamLibrary\steamapps\common\Stardew Valley"
                      disabled={setupBusy}
                      onChange={setGamePath}
                    />
                    <Button
                      disabled={!gamePath.trim() || !canSetupAction("yui_stardew_select_game")}
                      onClick={() => void runSetupAction("yui_stardew_select_game", { game_path: gamePath.trim() })}
                    >
                      {setupAction === "yui_stardew_select_game" ? "检查中…" : "检查目录"}
                    </Button>
                  </Inline>
                </Field>
                <KeyValue items={[
                  { key: "path", label: "当前操作目录", value: setup.selected_path || "尚未选择" },
                  { key: "smapi", label: "SMAPI", value: setup.smapi_installed ? "已安装" : "未检测到" },
                  { key: "installed", label: "已安装 Yui", value: setup.installed_version || "未安装" },
                  { key: "bundled", label: "插件配套 Yui", value: setup.bundled_version || "不可用" },
                  { key: "bridge", label: "桥接已开启", value: yesNo(setup.bridge_enabled) },
                  { key: "running", label: "游戏运行中", value: yesNo(setup.game_running) },
                  { key: "backup", label: "升级备份", value: setup.backup_available ? "已有备份" : "替换已有模组前自动备份" },
                ]} />
                {setup.selected_path && !setup.smapi_installed ? (
                  <Alert tone="warning">
                    请先前往 <a href="https://smapi.io/" target="_blank" rel="noopener noreferrer">SMAPI 官网</a>，下载并运行 Windows 安装器，按提示安装到上方游戏目录。完成后点击“刷新连接”，再安装 Yui。
                  </Alert>
                ) : null}
                {setup.game_running ? <Tip>更新模组或开启桥接前，请先退出游戏。</Tip> : null}
                <Inline>
                  <Button
                    tone="primary"
                    disabled={!setup.can_install || !canSetupAction("yui_stardew_install_mod")}
                    onClick={() => void runSetupAction("yui_stardew_install_mod")}
                  >
                    {setupAction === "yui_stardew_install_mod" ? "安装中…" : installLabel}
                  </Button>
                  <Button
                    disabled={!setup.can_enable_bridge || !canSetupAction("yui_stardew_enable_bridge")}
                    onClick={() => void runSetupAction("yui_stardew_enable_bridge")}
                  >
                    {setupAction === "yui_stardew_enable_bridge" ? "开启中…" : "开启桥接"}
                  </Button>
                  <Button
                    disabled={!setup.can_launch || !canSetupAction("yui_stardew_launch_game")}
                    onClick={() => void runSetupAction("yui_stardew_launch_game")}
                  >
                    {setupAction === "yui_stardew_launch_game" ? "启动中…" : "启动游戏"}
                  </Button>
                </Inline>
                <Tip>安装会保留原有配置和额外文件，开启桥接；替换已有模组前会保存备份。实验农活设置保持原样。启动后请进入单人或自己作为房主的存档，等待下方连接验证。</Tip>
              </Stack>
            ) : null}
            {lastSetupResult ? (
              <Alert tone={lastSetupResult.ok ? "success" : "danger"}>
                <Stack>
                  <Text>{lastSetupResult.message}</Text>
                  {lastSetupResult.backup_path ? <Text>备份位置：{lastSetupResult.backup_path}</Text> : null}
                </Stack>
              </Alert>
            ) : null}
          </Stack>
        </Card> : null}

        <Alert tone={discoveryState === "ready" ? "info" : "warning"}>
          {discovery.code ? `${discovery.code}：` : ""}
          {discovery.message || connection.message || "正在读取本机桥状态。"}
        </Alert>

        <Grid cols={4}>
          <StatCard label="桥协议" value={`v${state.protocol_version || "-"}`} />
          <StatCard label="传输" value={discovery.transport === "websocket" ? "WebSocket" : "-"} />
          <StatCard
            label="连接来源"
            value={SOURCE_LABELS[discovery.source || "unconfigured"] || discovery.source || "未知"}
          />
          <StatCard
            label="游戏世界"
            value={
              <StatusBadge
                tone={avatar.world_ready ? "success" : "default"}
                label={avatar.world_ready ? "已就绪" : "未就绪"}
              />
            }
          />
        </Grid>

        {discoveryState === "ambiguous" ? (
          <Card title="选择当前游戏实例">
            <Stack>
              <Alert tone="warning">
                多个本机实例均已通过鉴权。选择只对本次插件会话有效，重启后会重新验证。
              </Alert>
              <Field label="已验证实例" help="列表不会显示配置路径或桥密钥。">
                <Select
                  value={selecting || ""}
                  options={[
                    { value: "", label: "请选择当前实例" },
                    ...candidates.map((candidate) => ({
                      value: candidate.instance_id || "",
                      label: `${candidate.label || "YuiToIssho"}${candidate.world_ready && candidate.host_authoritative ? " · 游戏已就绪" : ""}`,
                    })),
                  ]}
                  disabled={!canSelect || !!selecting}
                  onChange={(value) => void selectInstance(String(value || ""))}
                />
              </Field>
            </Stack>
          </Card>
        ) : null}

        <Grid cols={2}>
          <Card title="游戏状态">
            <KeyValue
              items={[
                { key: "host", label: "Host 权威", value: yesNo(avatar.host_authoritative) },
                { key: "companion", label: "Yui 记录", value: yesNo(avatar.companion_exists) },
                { key: "body", label: "游戏身体", value: yesNo(avatar.body_present) },
                { key: "location", label: "地点", value: avatar.location || "-" },
                { key: "mode", label: "模式", value: avatar.mode || "-" },
                { key: "behavior", label: "行为", value: avatar.behavior || "-" },
                { key: "brain", label: "Agent 阶段", value: avatar.brain_phase || "-" },
                { key: "vital", label: "生命状态", value: avatar.vital_state || "-" },
                { key: "work", label: "当前工作", value: avatar.work_kind || "-" },
                { key: "workState", label: "工作阶段", value: avatar.work_state || "-" },
              ]}
            />
          </Card>

          <Card title="连接诊断">
            <Stack>
              <KeyValue
                items={[
                  { key: "state", label: "状态", value: STATE_LABELS[discoveryState] || discoveryState },
                  { key: "code", label: "原因码", value: discovery.code || "-" },
                  { key: "source", label: "来源", value: SOURCE_LABELS[discovery.source || "unconfigured"] || "未知" },
                  { key: "verified", label: "最近验证", value: discovery.verified_at || "尚未验证" },
                ]}
              />
              <Inline justify="end">
                {discovery.open_config_directory ? (
                  <Button
                    tone="default"
                    onClick={() => openLocalPath(discovery.open_config_directory || "")}
                  >
                    打开配置目录
                  </Button>
                ) : null}
                {discovery.compatibility_override ? (
                  <Button
                    tone="default"
                    disabled={!canClear || clearing}
                    onClick={() => void clearCompatibilityOverride()}
                  >
                    {clearing ? "清除中…" : "清除手动覆盖"}
                  </Button>
                ) : null}
              </Inline>
            </Stack>
          </Card>
        </Grid>

        <Tip>
          完成首次安装后，游戏与 N.E.K.O 在同一台电脑、同一系统用户下运行即可自动连接，无需填写端口或密钥。
        </Tip>
        <Text>
          召唤、跟随、等待、停止、召回和工作仍只由 N.E.K.O 根据你的对话指令调用，不会出现在本面板。
        </Text>
      </Stack>
    </Page>
  )
}
