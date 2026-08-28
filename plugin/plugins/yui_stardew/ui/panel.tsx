import {
  Alert,
  Button,
  Card,
  Field,
  Grid,
  Inline,
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
  port?: number
  source?: string
  world_ready?: boolean
  host_authoritative?: boolean
}

type Discovery = {
  state?: string
  code?: string
  message?: string
  source?: string
  port?: number
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

type DashboardState = {
  protocol_version?: number
  discovery?: Discovery
  connection?: Connection
  avatar?: AvatarState
  ui_token?: string
}

const SOURCE_LABELS: Record<string, string> = {
  discovery_v1: "HimiYui 自动发现",
  legacy_scan: "旧版安装扫描",
  environment: "环境变量覆盖",
  manual_path: "旧版路径覆盖",
  manual_credentials: "旧版端口与密钥",
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
  const toast = useToast()

  useEffect(() => {
    if (!new Set(["not_found", "offline", "auth_failed", "invalid"]).has(discovery.state || "")) {
      return
    }
    const timer = window.setInterval(() => {
      void props.api.refresh().catch(() => undefined)
    }, 5000)
    return () => window.clearInterval(timer)
  }, [discovery.state, props.api])

  async function selectInstance(instanceId: string) {
    if (!canSelect || !instanceId) {
      toast.error("实例选择动作尚未可用，请刷新插件状态。")
      return
    }
    setSelecting(instanceId)
    try {
      await props.api.call("yui_stardew_select_instance", {
        instance_id: instanceId,
        ui_token: uiToken,
      })
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
      await props.api.call("yui_stardew_clear_compatibility_override", {
        ui_token: uiToken,
      })
      await props.api.refresh()
      toast.success("已清除旧版覆盖，恢复 HimiYui 自动发现。")
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
      subtitle="HimiYui 管理桥配置；此处只负责自动发现、连接状态与故障恢复。"
    >
      <Stack>
        <Inline align="center" justify="space-between">
          <StatusBadge
            tone={connectionTone}
            label={STATE_LABELS[discoveryState] || discoveryState}
          />
          <RefreshButton label="刷新连接" />
        </Inline>

        <Alert tone={discoveryState === "ready" ? "info" : "warning"}>
          {discovery.code ? `${discovery.code}：` : ""}
          {discovery.message || connection.message || "正在读取本机桥状态。"}
        </Alert>

        <Grid cols={4}>
          <StatCard label="桥协议" value={`v${state.protocol_version || "-"}`} />
          <StatCard label="端口" value={discovery.port || "-"} />
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
                      label: `${candidate.label || "HimiYui"}${candidate.world_ready && candidate.host_authoritative ? " · 游戏已就绪" : ""}`,
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
                    {clearing ? "清除中…" : "清除兼容覆盖"}
                  </Button>
                ) : null}
              </Inline>
            </Stack>
          </Card>
        </Grid>

        <Tip>
          正常情况下无需设置游戏路径；启动 Stardew Valley 与 HimiYui 后会自动连接。
        </Tip>
        <Text>
          召唤、跟随、等待、停止、召回和工作仍只由 N.E.K.O 根据你的对话指令调用，不会出现在本面板。
        </Text>
      </Stack>
    </Page>
  )
}
