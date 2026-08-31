import {
  Alert,
  Button,
  Card,
  EmptyState,
  Field,
  Grid,
  Input,
  Page,
  PasswordInput,
  Select,
  Stack,
  Switch,
  Tabs,
  Text,
  Textarea,
  useEffect,
  useForm,
  useState,
  useToast,
} from "@neko/plugin-ui"
import type { HostedAction, PluginSurfaceProps } from "@neko/plugin-ui"

type SelfieConfig = {
  api_format?: string
  base_url?: string
  model?: string
  size?: string
  character_prompt?: string
  prompt_suffix?: string
  negative_prompt?: string
  default_style?: string
  reference_source?: string
  reference_image_path?: string
  public_base_url?: string
  context_enabled?: boolean
  diary_enabled?: boolean
}

type RecentImage = {
  filename?: string
  public_url?: string
  preview_url?: string
  created_at?: string
}

type LifeEvent = {
  id?: string | number
  time?: string
  title?: string
  diary?: string
  mood?: string
  level?: "mundane" | "notable" | "highlight"
  photo_url?: string
}

type DashboardState = {
  ready?: boolean
  configured?: boolean
  api_key_configured?: boolean
  config?: SelfieConfig
  recent_images?: RecentImage[]
  diary_enabled?: boolean
  diary_events?: LifeEvent[]
  pending_count?: number
}

type PageTurnState = {
  from: number
  to: number
  direction: "forward" | "backward"
}

const GENERATE_TIMEOUT_MS = 300_000

function openExternalUrl(url: string): void {
  if (!/^https?:\/\//i.test(url)) return
  if (window.parent && window.parent !== window) {
    window.parent.postMessage(
      { type: "neko-hosted-surface-open-external", payload: { url } },
      "*",
    )
    return
  }
  window.open(url, "_blank", "noopener,noreferrer")
}

function openPhoto(event: { preventDefault: () => void }, url?: string): void {
  event.preventDefault()
  if (url) openExternalUrl(url)
}

const STORYBOOK_STYLES = `
  :root {
    --story-ink: #1d2c43;
    --story-muted: #627188;
    --story-paper: #fffaf0;
    --story-ribbon: #7799c4;
    --story-navy: #1d2b42;
    --story-navy-frame: rgba(29, 43, 66, .48);
    --story-gold-frame: rgba(184, 154, 99, .48);
    --story-paper-frame: rgba(255, 250, 240, .58);
    --story-mat-frame: rgba(233, 223, 203, .52);
    --story-leaf: #718567;
    --story-page-edge: #d8cfbd;
    --story-cover-deep: #101c2e;
    --story-cover-mid: #263b58;
    --page-turn-duration: 620ms;
  }
  body { overflow-x: clip; }
  .neko-page { min-width: 0; }
  .storybook-reader, .storybook-reader * { box-sizing: border-box; }
  .story-stage { min-height: clamp(560px, 76vh, 760px); display: grid; place-items: center; overflow: hidden; border-radius: 24px; background: radial-gradient(circle at 50% 24%, rgba(255, 255, 255, .9), transparent 35%), linear-gradient(150deg, rgba(130, 185, 230, .2), rgba(238, 227, 207, .34)); }
  .story-cover { position: relative; width: 100%; height: 100%; background: var(--story-paper); overflow: hidden; }
  .story-cover-image { display: block; width: 100%; height: 100%; object-fit: contain; }
  .storybook-reader { min-width: 0; padding: clamp(8px, 2vw, 22px) 0 28px; perspective: 1400px; }
  .story-book { position: relative; isolation: isolate; width: min(390px, calc(100% - 38px)); aspect-ratio: 2 / 3; border-radius: 3px 12px 12px 3px; background: var(--story-paper); box-shadow: -8px 9px 0 var(--story-navy), 0 28px 55px rgba(29, 43, 66, .28); overflow: hidden; transform-style: preserve-3d; animation: story-open 700ms cubic-bezier(.18,.77,.24,1) both; }
  .story-book::before { content: ""; position: absolute; z-index: 20; top: 0; bottom: 0; left: 0; width: 10px; pointer-events: none; background: linear-gradient(90deg, rgba(29, 43, 66, .56), rgba(255,255,255,.2)); box-shadow: 3px 0 8px rgba(29, 43, 66, .14); }
  .story-book::after { content: ""; position: absolute; z-index: 19; right: 1px; bottom: 0; left: 8px; height: 7px; border-radius: 0 0 10px 2px; pointer-events: none; background: repeating-linear-gradient(0deg, var(--story-page-edge) 0 1px, var(--story-paper) 1px 3px); box-shadow: 0 -2px 5px rgba(29, 43, 66, .08); }
  .story-page-layer { position: absolute; inset: 0; min-width: 0; border-radius: inherit; transform-origin: left center; transform-style: preserve-3d; }
  .story-page-layer.is-base { z-index: 1; overflow: hidden; background: var(--story-paper); }
  .story-page-layer.is-turning { z-index: 8; pointer-events: none; will-change: transform; }
  .story-page-layer.is-turning-forward { animation: book-page-forward var(--page-turn-duration) cubic-bezier(.45, .02, .18, .98) both; }
  .story-page-layer.is-turning-backward { animation: book-page-backward var(--page-turn-duration) cubic-bezier(.45, .02, .18, .98) both; }
  .story-page-layer.is-cover-sheet { --page-turn-duration: 720ms; }
  .story-face { position: absolute; inset: 0; overflow: hidden; border-radius: inherit; background: var(--story-paper); backface-visibility: hidden; -webkit-backface-visibility: hidden; }
  .story-face-back { transform: rotateY(180deg); background: radial-gradient(circle at 88% 9%, rgba(130, 185, 230, .12), transparent 26%), repeating-linear-gradient(0deg, rgba(29, 43, 66, .022) 0 1px, transparent 1px 5px), var(--story-paper); box-shadow: inset -12px 0 22px rgba(29, 43, 66, .13); }
  .story-face-back::before { content: ""; position: absolute; inset: 14px; border: 1px solid rgba(29, 43, 66, .34); box-shadow: 0 0 0 4px var(--story-paper), 0 0 0 5px rgba(119, 153, 196, .42); }
  .is-cover-sheet .story-face-back { background: linear-gradient(100deg, var(--story-cover-deep), var(--story-navy) 18%, var(--story-cover-mid) 100%); box-shadow: inset -18px 0 28px rgba(0, 0, 0, .28); }
  .page-turn-light { position: absolute; z-index: 30; inset: 0; pointer-events: none; background: linear-gradient(90deg, rgba(29, 43, 66, .28), transparent 20%, transparent 70%, rgba(255, 255, 255, .42)); opacity: 0; }
  .is-turning-forward .story-face-front .page-turn-light { animation: page-light-forward var(--page-turn-duration) ease-in-out both; }
  .is-turning-backward .story-face-front .page-turn-light { animation: page-light-backward var(--page-turn-duration) ease-in-out both; }
  .page-turn-cast { position: absolute; z-index: 6; inset: 0; border-radius: inherit; pointer-events: none; background: linear-gradient(90deg, rgba(29, 43, 66, .42), rgba(29, 43, 66, .08) 42%, transparent 76%); transform-origin: left center; }
  .page-turn-cast.is-cover-turn { --page-turn-duration: 720ms; }
  .page-turn-cast.is-forward { animation: cast-shadow-forward var(--page-turn-duration) ease-in-out both; }
  .page-turn-cast.is-backward { animation: cast-shadow-backward var(--page-turn-duration) ease-in-out both; }
  .story-page { position: relative; min-width: 0; height: 100%; padding: clamp(48px, 7vw, 62px) clamp(30px, 6vw, 46px) 72px; color: var(--story-ink); background: radial-gradient(circle at 88% 9%, rgba(130, 185, 230, .16), transparent 24%), radial-gradient(circle at 8% 91%, rgba(113, 133, 103, .1), transparent 22%), repeating-linear-gradient(0deg, rgba(29, 43, 66, .018) 0 1px, transparent 1px 5px), var(--story-paper); overflow: hidden; }
  .story-page::before { content: ""; position: absolute; z-index: 0; inset: 14px; border: 1px solid var(--story-navy); box-shadow: 0 0 0 4px var(--story-paper), 0 0 0 5px rgba(119, 153, 196, .62); pointer-events: none; }
  .story-page > * { position: relative; z-index: 1; }
  .story-page.is-photo-page { background: radial-gradient(circle at 88% 9%, rgba(216, 170, 165, .15), transparent 22%), radial-gradient(circle at 8% 91%, rgba(130, 185, 230, .16), transparent 24%), var(--story-paper); }
  .page-turn-zone { position: absolute; z-index: 30; top: 0; bottom: 0; width: 50%; border: 0; padding: 0; background: transparent; cursor: pointer; }
  .page-turn-zone:disabled { cursor: progress; }
  .page-turn-zone:focus { outline: none; }
  .page-turn-zone:focus-visible::after { color: var(--story-ribbon); }
  .page-turn-zone::after { position: absolute; top: 50%; color: rgba(29, 43, 66, 0); font: 34px/1 Georgia, serif; transform: translateY(-50%); transition: color 160ms ease; }
  .page-turn-zone:hover::after { color: rgba(29, 43, 66, .42); }
  .page-turn-left { left: 0; }
  .page-turn-left::after { content: "‹"; left: 14px; }
  .page-turn-right { right: 0; }
  .page-turn-right::after { content: "›"; right: 14px; }
  .page-ornament { position: absolute; width: 54px; height: 54px; opacity: .72; pointer-events: none; }
  .page-ornament::before, .page-ornament::after { content: ""; position: absolute; border: 1px solid var(--story-leaf); border-radius: 100% 0 100% 0; transform: rotate(28deg); }
  .page-ornament::before { width: 36px; height: 15px; left: 5px; top: 12px; }
  .page-ornament::after { width: 24px; height: 11px; left: 24px; top: 29px; }
  .ornament-top-left { top: 13px; left: 14px; }
  .ornament-bottom-right { right: 14px; bottom: 13px; transform: rotate(180deg); }
  .story-date { display: inline-block; padding-bottom: 8px; border-bottom: 1px solid var(--story-ribbon); color: var(--story-ribbon); font-size: 11px; font-weight: 700; letter-spacing: .16em; text-transform: uppercase; }
  .story-heading { margin: 16px 0 22px; color: var(--story-navy); font-family: "Songti SC", "SimSun", Georgia, serif; font-size: clamp(25px, 4vw, 37px); line-height: 1.25; letter-spacing: .04em; text-wrap: balance; }
  .story-copy { margin: 0; color: var(--story-muted); font-family: "Songti SC", "SimSun", Georgia, serif; font-size: clamp(15px, 2.1vw, 17px); line-height: 2.05; text-align: justify; text-wrap: pretty; }
  .story-dropcap::first-letter { float: left; margin: 7px 9px 0 0; color: var(--story-ribbon); font-size: 50px; line-height: .74; }
  .story-photo { position: relative; display: block; width: min(100%, 310px); margin: 24px auto 20px; padding: 7px 7px 29px; border: 1px solid var(--story-gold-frame); background: linear-gradient(145deg, var(--story-paper-frame), var(--story-mat-frame)); box-shadow: 0 0 0 3px var(--story-navy-frame), 0 0 0 4px var(--story-gold-frame), 0 10px 20px rgba(29, 43, 66, .14); color: inherit; text-decoration: none; transform: rotate(-1.5deg); }
  .story-photo:nth-of-type(even) { transform: rotate(2deg); }
  .photo-mat { display: grid; width: 100%; aspect-ratio: 1; min-width: 0; min-height: 0; padding: 6px; grid-template: minmax(0, 1fr) / minmax(0, 1fr); place-items: center; overflow: hidden; border: 1px solid var(--story-navy-frame); background: rgba(119, 153, 196, .12); box-shadow: inset 0 0 0 2px var(--story-mat-frame), inset 0 0 14px rgba(29, 43, 66, .1); }
  .photo-mat img { display: block; width: 100%; height: 100%; min-width: 0; min-height: 0; object-fit: contain; background: transparent; }
  .photo-caption { position: absolute; right: 10px; bottom: 8px; left: 10px; overflow: hidden; color: var(--story-muted); font: 12px/1.2 "Songti SC", "SimSun", Georgia, serif; text-align: center; text-overflow: ellipsis; white-space: nowrap; }
  .photo-stack { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px; align-items: start; }
  .photo-stack .story-photo { width: 100%; margin: 10px 0 4px; padding: 6px 6px 24px; }
  .album-photo-frame { display: block; min-width: 0; padding: 7px; border: 1px solid var(--story-gold-frame); border-radius: 2px; background: linear-gradient(145deg, var(--story-paper-frame), var(--story-mat-frame)); box-shadow: 0 0 0 3px var(--story-navy-frame), 0 6px 14px rgba(29, 43, 66, .13); transition: transform 160ms ease, box-shadow 160ms ease; }
  .album-photo-frame:hover { box-shadow: 0 0 0 3px rgba(29, 43, 66, .5), 0 9px 20px rgba(29, 43, 66, .18); transform: translateY(-2px); }
  .album-photo-frame:focus-visible { outline: 3px solid var(--story-ribbon); outline-offset: 5px; }
  .album-photo-frame .photo-mat { padding: 5px; }
  .empty-vignette { margin: 54px auto 0; max-width: 330px; padding: 25px 21px; border-block: 1px solid var(--story-ribbon); color: var(--story-muted); font-family: "Songti SC", "SimSun", Georgia, serif; line-height: 1.9; text-align: center; }
  .page-number { position: absolute; z-index: 11; right: 0; bottom: 22px; left: 0; color: var(--story-ribbon); font: 12px/1 Georgia, serif; text-align: center; white-space: nowrap; pointer-events: none; }
  @keyframes story-open { from { opacity: 0; transform: perspective(1300px) rotateY(-15deg) scale(.92); } to { opacity: 1; transform: perspective(1300px) rotateY(0) scale(1); } }
  @keyframes book-page-forward { 0% { opacity: 1; transform: rotateY(0deg) translateZ(2px); } 56% { opacity: 1; transform: rotateY(-58deg) translateZ(8px); } 100% { opacity: .12; transform: rotateY(-91deg) translateZ(2px); } }
  @keyframes book-page-backward { 0% { opacity: .12; transform: rotateY(-91deg) translateZ(2px); } 44% { opacity: 1; transform: rotateY(-58deg) translateZ(8px); } 100% { opacity: 1; transform: rotateY(0deg) translateZ(2px); } }
  @keyframes page-light-forward { 0%, 100% { opacity: 0; } 44% { opacity: .78; } 58% { opacity: .32; } }
  @keyframes page-light-backward { 0%, 100% { opacity: 0; } 42% { opacity: .34; } 56% { opacity: .78; } }
  @keyframes cast-shadow-forward { 0%, 100% { opacity: 0; transform: scaleX(1); } 46% { opacity: .68; transform: scaleX(.2); } }
  @keyframes cast-shadow-backward { 0%, 100% { opacity: 0; transform: scaleX(1); } 54% { opacity: .68; transform: scaleX(.2); } }
  @media (max-width: 720px) { .story-stage { min-height: 560px; } }
  @media (max-width: 420px) {
    .neko-page { padding-inline: max(0px, min(18px, calc((100% - 1px) / 8))); }
    .neko-page header, .neko-tabs, .neko-tab-panel { min-width: 0; max-width: 100%; }
    .neko-tab-list { min-width: 0; max-width: 100%; overflow-x: auto; }
    .story-stage { min-height: 535px; }
  }
  @media (max-width: 120px) {
    .neko-page { padding-inline: 0; }
    .neko-page > header { display: none; }
    .neko-tab-button { min-width: 0; padding-inline: 4px; overflow-wrap: anywhere; }
    .story-page { padding: 52px 4px 52px; }
    .story-heading, .story-copy { overflow-wrap: anywhere; }
    .story-heading { font-size: 15px; }
    .story-copy { font-size: 12px; line-height: 1.6; }
    .page-ornament { display: none; }
    .empty-vignette { margin-top: 20px; padding-inline: 2px; }
    .photo-caption { display: none; }
    .story-photo { padding: 2px 2px 1px; transform: none; }
    .photo-mat { padding: 1px; }
  }
  @media (prefers-reduced-motion: reduce) {
    .story-cover, .story-book, .story-page { animation: none; transition: none; }
    .story-page-layer.is-turning-forward, .story-page-layer.is-turning-backward, .page-turn-cast, .page-turn-light { animation-duration: 1ms; }
  }
`

const defaultConfig = {
  api_format: "dashscope",
  base_url: "https://dashscope.aliyuncs.com/api/v1",
  api_key: "",
  model: "qwen-image-2.0",
  size: "1024x1024",
  character_prompt: "",
  prompt_suffix: "",
  negative_prompt: "",
  default_style: "standard",
  reference_source: "none",
  reference_image_path: "",
  public_base_url: "",
  context_enabled: true,
  diary_enabled: true,
}

function actionById(actions: HostedAction[], id: string): HostedAction | undefined {
  return actions.find((action) => action.id === id || action.entry_id === id)
}

function storyDate(value?: string): string {
  const date = String(value || "").split("T")[0]
  return date ? date.replace(/-/g, ".") : ""
}

export default function SelfiePainterPanel(props: PluginSurfaceProps<DashboardState>) {
  const { state, actions, t } = props
  const safeState = state || {}
  const config = safeState.config || {}
  const recent = Array.isArray(safeState.recent_images) ? safeState.recent_images : []
  const events = Array.isArray(safeState.diary_events) ? safeState.diary_events : []
  const storyPhotos: RecentImage[] = [
    ...events
      .filter((event) => event.photo_url)
      .map((event) => ({
        filename: event.title || String(event.id || "diary photo"),
        public_url: event.photo_url,
        preview_url: event.photo_url,
        created_at: event.time,
      })),
    ...recent,
  ].slice(0, 3)
  const saveAction = actionById(actions || [], "selfie_save_config")
  const generateAction = actionById(actions || [], "selfie_generate_webui")
  const clearDiaryAction = actionById(actions || [], "selfie_clear_diary")
  const configForm = useForm(defaultConfig)
  const [scene, setScene] = useState("")
  const [style, setStyle] = useState(String(config.default_style || "standard"))
  const [generating, setGenerating] = useState(false)
  const [bookPage, setBookPage] = useState(0)
  const [pageTurn, setPageTurn] = useState<PageTurnState | null>(null)
  const toast = useToast()

  useEffect(() => {
    configForm.setValues({
      api_format: String(config.api_format || "openai"),
      base_url: String(config.base_url || ""),
      api_key: "",
      model: String(config.model || ""),
      size: String(config.size || "1024x1024"),
      character_prompt: String(config.character_prompt || ""),
      prompt_suffix: String(config.prompt_suffix || ""),
      negative_prompt: String(config.negative_prompt || ""),
      default_style: String(config.default_style || "standard"),
      reference_source: String(config.reference_source || "none"),
      reference_image_path: String(config.reference_image_path || ""),
      public_base_url: String(config.public_base_url || ""),
      context_enabled: config.context_enabled !== false,
      diary_enabled: config.diary_enabled !== false,
    })
    setStyle(String(config.default_style || "standard"))
  }, [
    config.api_format,
    config.base_url,
    config.model,
    config.size,
    config.character_prompt,
    config.prompt_suffix,
    config.negative_prompt,
    config.default_style,
    config.reference_source,
    config.reference_image_path,
    config.public_base_url,
    config.context_enabled,
    config.diary_enabled,
  ])

  async function saveConfig() {
    if (!saveAction) {
      toast.error(t("panel.errors.actionUnavailable"))
      return
    }
    try {
      await props.api.call("selfie_save_config", { ...configForm.values })
      configForm.setField("api_key", "")
      await props.api.refresh()
      toast.success(t("panel.messages.saved"))
    } catch (error) {
      toast.error(error instanceof Error ? error.message : String(error))
    }
  }

  async function generateSelfie() {
    if (!generateAction) {
      toast.error(t("panel.errors.actionUnavailable"))
      return
    }
    setGenerating(true)
    try {
      await props.api.call(
        "selfie_generate_webui",
        { scene: scene.trim(), style },
        { timeoutMs: GENERATE_TIMEOUT_MS },
      )
      await props.api.refresh()
      toast.success(t("panel.messages.generated"))
    } catch (error) {
      toast.error(error instanceof Error ? error.message : String(error))
    } finally {
      setGenerating(false)
    }
  }

  async function clearDiary() {
    if (!clearDiaryAction) {
      toast.error(t("panel.errors.actionUnavailable"))
      return
    }
    if (!window.confirm(t("panel.diary.clearConfirm"))) return
    try {
      await props.api.call("selfie_clear_diary", {})
      setBookPage(0)
      await props.api.refresh()
      toast.success(t("panel.messages.diaryCleared"))
    } catch (error) {
      toast.error(error instanceof Error ? error.message : String(error))
    }
  }

  const styleOptions = [
    { value: "standard", label: t("panel.options.standard") },
    { value: "mirror", label: t("panel.options.mirror") },
    { value: "photo", label: t("panel.options.photo") },
  ]

  const apiFormatOptions = [
    { value: "openai", label: t("panel.options.openai") },
    { value: "dashscope", label: t("panel.options.dashscope") },
    { value: "modelscope", label: t("panel.options.modelscope") },
  ]

  function selectApiFormat(value: unknown) {
    const apiFormat = String(value)
    configForm.setField("api_format", apiFormat)
    if (apiFormat === "dashscope") {
      configForm.setField("base_url", "https://dashscope.aliyuncs.com/api/v1")
      configForm.setField("model", "qwen-image-2.0")
      configForm.setField("size", "1024x1024")
    }
  }

  const photoGallery = (
    <Card title={t("panel.diary.albumTitle")}>
      <Stack>
        <Text>{t("panel.diary.albumHint")}</Text>
        {recent.length ? (
          <Grid cols={3}>
            {recent.map((image) => (
              <a
                className="album-photo-frame"
                key={image.filename}
                href={image.public_url || "#"}
                target="_blank"
                rel="noreferrer"
                onClick={(event) => openPhoto(event, image.public_url)}
              >
                <span className="photo-mat">
                  <img
                    src={image.preview_url || ""}
                    alt={image.filename || "NEKO selfie"}
                    loading="lazy"
                    decoding="async"
                  />
                </span>
              </a>
            ))}
          </Grid>
        ) : (
          <EmptyState title={t("panel.diary.albumEmpty")} description={t("panel.diary.albumEmptyHint")} />
        )}
      </Stack>
    </Card>
  )

  function turnBook(direction: PageTurnState["direction"]) {
    if (pageTurn) return
    const step = direction === "forward" ? 1 : -1
    setPageTurn({ from: bookPage, to: (bookPage + step + 3) % 3, direction })
  }

  function finishPageTurn() {
    if (!pageTurn) return
    setBookPage(pageTurn.to)
    setPageTurn(null)
  }

  function renderBookPage(page: number) {
    if (page === 0) {
      return (
        <div className="story-cover">
          <img
            className="story-cover-image"
            src="/plugin/selfie_painter_neko/ui/journal-cover.png"
            alt={t("panel.diary.coverTitle")}
            decoding="async"
            fetchPriority="high"
          />
        </div>
      )
    }
    if (page === 1) {
      return (
        <article className="story-page">
          <span className="page-ornament ornament-top-left" aria-hidden="true" />
          <span className="story-date">{t("panel.diary.todayTitle")}</span>
          <h2 className="story-heading">{events[0]?.title || t("panel.diary.emptyTitle")}</h2>
          {events.length ? (
            events.slice(0, 2).map((event) => (
              <div key={event.id || `${event.time}-${event.title}`}>
                <p className="story-copy story-dropcap">{event.diary || ""}</p>
                {event.mood ? <p className="story-copy">{`${t("panel.diary.mood")}：${event.mood}`}</p> : null}
              </div>
            ))
          ) : (
            <>
              <p className="story-copy story-dropcap">{t("panel.diary.emptyDescription")}</p>
              <div className="empty-vignette">
                {`${t("panel.diary.stepObserve")} · ${t("panel.diary.stepRemember")} · ${t("panel.diary.stepPhoto")}`}
              </div>
            </>
          )}
          <span className="page-number">2 / 3</span>
        </article>
      )
    }
    return (
      <article className="story-page is-photo-page">
        <span className="page-ornament ornament-bottom-right" aria-hidden="true" />
        <span className="story-date">{t("panel.diary.albumTitle")}</span>
        <h2 className="story-heading">{t("panel.diary.bookPhotoTitle")}</h2>
        {storyPhotos.length ? (
          <div className="photo-stack">
            {storyPhotos.map((image) => (
              <a
                className="story-photo"
                key={image.filename}
                href={image.public_url || "#"}
                target="_blank"
                rel="noreferrer"
                onClick={(event) => openPhoto(event, image.public_url)}
              >
                <span className="photo-mat">
                  <img
                    src={image.preview_url || ""}
                    alt={image.filename || "NEKO selfie"}
                    loading="lazy"
                    decoding="async"
                  />
                </span>
                <span className="photo-caption">{storyDate(image.created_at) || t("panel.diary.bookKeepsake")}</span>
              </a>
            ))}
          </div>
        ) : (
          <div className="empty-vignette">
            {t("panel.diary.albumEmpty")}
            <br />
            {t("panel.diary.albumEmptyHint")}
          </div>
        )}
        <span className="page-number">3 / 3</span>
      </article>
    )
  }

  const baseBookPage = pageTurn?.direction === "forward" ? pageTurn.to : pageTurn?.from ?? bookPage
  const turningBookPage = pageTurn?.direction === "forward" ? pageTurn.from : pageTurn?.to

  const diaryView = (
    <div className="storybook-reader">
      <div className="story-stage">
        <div className="story-book">
          <div className="story-page-layer is-base" key={`base-page-${baseBookPage}`}>
            {renderBookPage(baseBookPage)}
          </div>
          {pageTurn && turningBookPage !== undefined ? (
            <>
              <span
                className={`page-turn-cast is-${pageTurn.direction}${turningBookPage === 0 ? " is-cover-turn" : ""}`}
                aria-hidden="true"
              />
              <div
                className={`story-page-layer is-turning is-turning-${pageTurn.direction}${
                  turningBookPage === 0 ? " is-cover-sheet" : ""
                }`}
                aria-hidden="true"
                onAnimationEnd={(event) => {
                  if (event.target === event.currentTarget) finishPageTurn()
                }}
              >
                <div className="story-face story-face-front">
                  {renderBookPage(turningBookPage)}
                  <span className="page-turn-light" aria-hidden="true" />
                </div>
                <div className="story-face story-face-back" aria-hidden="true" />
              </div>
            </>
          ) : null}
          <button
            className="page-turn-zone page-turn-left"
            type="button"
            aria-label={t("panel.diary.previousPage")}
            disabled={Boolean(pageTurn)}
            onClick={() => turnBook("backward")}
          />
          <button
            className="page-turn-zone page-turn-right"
            type="button"
            aria-label={t("panel.diary.nextPage")}
            disabled={Boolean(pageTurn)}
            onClick={() => turnBook("forward")}
          />
        </div>
      </div>
    </div>
  )

  const selfieView = (
    <Stack>
      <Card title={t("panel.generate.title")}>
        <Stack>
          <Alert tone={safeState.configured ? "success" : "warning"}>
            {safeState.configured ? t("panel.selfie.readyHint") : t("panel.selfie.missingHint")}
          </Alert>
          <Field label={t("panel.generate.scene")}>
            <Textarea value={scene} placeholder={t("panel.generate.scenePlaceholder")} onChange={setScene} />
          </Field>
          <Field label={t("panel.generate.style")}>
            <Select value={style} options={styleOptions} onChange={(value) => setStyle(String(value))} />
          </Field>
          <Button
            tone="primary"
            disabled={!generateAction || generating || !safeState.configured}
            onClick={generateSelfie}
          >
            {generating ? t("panel.generate.working") : t("panel.generate.submit")}
          </Button>
        </Stack>
      </Card>
      {photoGallery}
    </Stack>
  )

  const settingsView = (
    <Stack>
      <Card title={t("panel.diary.settingsTitle")}>
        <Stack>
          <Alert tone="info">{t("panel.diary.privacyNotice")}</Alert>
          <Switch
            checked={Boolean(configForm.values.context_enabled)}
            label={t("panel.diary.autoRecord")}
            onChange={(value) => configForm.setField("context_enabled", Boolean(value))}
          />
          <Switch
            checked={Boolean(configForm.values.diary_enabled)}
            label={t("panel.diary.autoPhoto")}
            onChange={(value) => configForm.setField("diary_enabled", Boolean(value))}
          />
          <Button disabled={!clearDiaryAction || !events.length} onClick={clearDiary}>
            {t("panel.diary.clear")}
          </Button>
        </Stack>
      </Card>

      <Card title={t("panel.config.title")}>
        <Stack>
          <Grid cols={2}>
            <Field label={t("panel.config.apiFormat")}>
              <Select value={configForm.values.api_format} options={apiFormatOptions} onChange={selectApiFormat} />
            </Field>
            <Field label={t("panel.config.size")}>
              <Select
                value={configForm.values.size}
                options={["1024x1024", "1024x1536", "1536x1024", "768x1024", "1024x768"]}
                onChange={(value) => configForm.setField("size", String(value))}
              />
            </Field>
          </Grid>
          <Field label={t("panel.config.baseUrl")} required>
            <Input value={configForm.values.base_url} onChange={(value) => configForm.setField("base_url", value)} />
          </Field>
          <Field
            label={t("panel.config.apiKey")}
            help={safeState.api_key_configured ? t("panel.config.apiKeyConfigured") : t("panel.config.apiKeyMissing")}
          >
            <PasswordInput value={configForm.values.api_key} onChange={(value) => configForm.setField("api_key", value)} />
          </Field>
          <Alert tone="warning">{t("panel.config.secretWarning")}</Alert>
          <Field label={t("panel.config.model")} required>
            <Input value={configForm.values.model} onChange={(value) => configForm.setField("model", value)} />
          </Field>
          <Field label={t("panel.config.characterPrompt")}>
            <Textarea
              value={configForm.values.character_prompt}
              onChange={(value) => configForm.setField("character_prompt", value)}
            />
          </Field>
          <Field label={t("panel.config.promptSuffix")}>
            <Textarea value={configForm.values.prompt_suffix} onChange={(value) => configForm.setField("prompt_suffix", value)} />
          </Field>
          <Field label={t("panel.config.negativePrompt")}>
            <Textarea
              value={configForm.values.negative_prompt}
              onChange={(value) => configForm.setField("negative_prompt", value)}
            />
          </Field>
          <Grid cols={2}>
            <Field label={t("panel.config.defaultStyle")}>
              <Select
                value={configForm.values.default_style}
                options={styleOptions}
                onChange={(value) => configForm.setField("default_style", String(value))}
              />
            </Field>
            <Field label={t("panel.config.referenceSource")}>
              <Select
                value={configForm.values.reference_source}
                options={[
                  { value: "none", label: t("panel.options.none") },
                  { value: "active_character", label: t("panel.options.activeCharacter") },
                  { value: "file", label: t("panel.options.file") },
                ]}
                onChange={(value) => configForm.setField("reference_source", String(value))}
              />
            </Field>
          </Grid>
          {configForm.values.reference_source === "file" ? (
            <Field label={t("panel.config.referencePath")}>
              <Input
                value={configForm.values.reference_image_path}
                onChange={(value) => configForm.setField("reference_image_path", value)}
              />
            </Field>
          ) : null}
          <Field label={t("panel.config.publicBaseUrl")} help={t("panel.config.publicBaseUrlHelp")}>
            <Input
              value={configForm.values.public_base_url}
              onChange={(value) => configForm.setField("public_base_url", value)}
            />
          </Field>
          <Button tone="success" disabled={!saveAction} onClick={saveConfig}>
            {t("panel.config.save")}
          </Button>
        </Stack>
      </Card>
    </Stack>
  )

  return (
    <>
      <style>{STORYBOOK_STYLES}</style>
      <Page title={t("panel.title")} subtitle={t("panel.subtitle")}>
        <Tabs
          items={[
            { id: "diary", label: t("panel.tabs.diary"), content: diaryView },
            { id: "selfie", label: t("panel.tabs.selfie"), content: selfieView },
            { id: "settings", label: t("panel.tabs.settings"), content: settingsView },
          ]}
        />
      </Page>
    </>
  )
}
