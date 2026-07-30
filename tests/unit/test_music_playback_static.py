import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
MUSIC_UI_PATH = ROOT / "static" / "jukebox" / "music_ui.js"
MUSIC_UI_CSS_PATH = ROOT / "static" / "css" / "music_ui.css"
PROACTIVE_UI_PATH = ROOT / "static" / "app" / "app-proactive.js"
APP_CHAT_PATH = ROOT / "static" / "app" / "app-chat.js"
APP_WEBSOCKET_PATH = ROOT / "static" / "app" / "app-websocket.js"
WEBSOCKET_ROUTER_PATH = ROOT / "main_routers" / "websocket_router.py"
LOCALES_DIR = ROOT / "static" / "locales"
MUSIC_ROUTER_PATH = ROOT / "main_routers" / "music_router.py"
MUSIC_CRAWLERS_PATH = ROOT / "utils" / "music_crawlers.py"
DEFAULT_MUSIC_COVER_PATH = ROOT / "static" / "assets" / "music" / "music-cover-placeholder.png"
PAGES_ROUTER_PATH = ROOT / "main_routers" / "pages_router.py"
MUSIC_PLAYER_TEMPLATES = (ROOT / "templates" / "index.html", ROOT / "templates" / "chat.html")


def test_music_dispatch_waits_for_media_and_reports_real_failure():
    source = MUSIC_UI_PATH.read_text(encoding="utf-8")
    dispatch_source = APP_CHAT_PATH.read_text(encoding="utf-8")

    assert "waitForMusicMediaReady" in source
    assert "const result = await executePlay(" in source
    assert "window.sendMusicMessageDetailed" in source
    assert "window.sendMusicMessage = async function" in source
    assert "return result.ok === true" in source
    assert "canTryNextCandidate" in source
    assert "canTryNextMusicCandidate(mediaResult.reason)" in source
    retryable_failures = source.split("const canTryNextMusicCandidate", 1)[1].split("].includes(reason);", 1)[0]
    assert "'media_error'" in retryable_failures
    assert "'track_too_long'" in retryable_failures
    assert "'load_timeout'" in retryable_failures
    assert "musicPlayResult(false, 'unsupported_stream', true)" in source
    assert "musicPlayResult(false, 'unsafe_url', true)" in source
    assert "MAX_RECOMMENDED_TRACK_DURATION_SECONDS = 10 * 60" in source
    assert "duration >= MAX_RECOMMENDED_TRACK_DURATION_SECONDS" in source
    assert "playbackOptions.source === 'proactive'" in source
    assert "window.dispatchMusicPlayDetailed" in dispatch_source
    assert "window.dispatchMusicPlay = async function" in dispatch_source
    assert "sendMusicMessageDetailed(trackInfo, true, options)" in dispatch_source
    assert "return new Promise(function (resolve)" in dispatch_source
    assert "musicDispatchResult(false, 'ui_not_ready', false)" in dispatch_source
    assert "result.ok === true && options.source === 'proactive'" in dispatch_source
    assert "return 'queued'" not in dispatch_source
    assert "isUnsupportedMusicStream" in source
    assert "endsWith('.m3u8')" in source
    assert "const backendProxyDomains = new Set(MUSIC_CONFIG.allowlist)" in source
    assert "const toBackendMusicProxyUrl = (url) =>" in source
    assert source.count("if (parsed.protocol !== 'https:')") == 2
    assert "['http:', 'https:'].includes(parsed.protocol)" not in source
    assert "trackInfo.url = toBackendMusicProxyUrl(originalUrl)" in source
    assert "trackInfo.url.includes('music.163.com')" not in source


def test_proactive_music_only_retries_candidate_specific_failures():
    source = PROACTIVE_UI_PATH.read_text(encoding="utf-8")

    assert "for (var musicIndex = 0; musicIndex < musicLinks.length; musicIndex++)" in source
    assert "window.dispatchMusicPlayDetailed(track, { source: 'proactive' })" in source
    assert "if (dispatchResult.ok === true)" in source
    assert "if (dispatchResult.canTryNextCandidate !== true)" in source
    assert "音乐派发因非候选错误停止" in source
    assert "音乐候选不可用，尝试下一条" in source
    assert "musicLinks = normalizedLinks.filter" in source
    assert "name: musicLink.title || '未知曲目'" not in source
    assert "artist: musicLink.artist || '未知艺术家'" not in source


def test_proactive_request_rechecks_music_state_before_search():
    source = PROACTIVE_UI_PATH.read_text(encoding="utf-8")
    player_source = MUSIC_UI_PATH.read_text(encoding="utf-8")

    assert "const isMusicOccupied = () =>" in player_source
    assert "localAudio && !localAudio.ended && !localPlayer._loadError" in player_source
    assert "mirrorBarLastState && mirrorBarLastState.track" in player_source
    assert "window.isMusicOccupied = isMusicOccupied" in player_source
    assert "var musicPlayingBeforeRequest" in source
    assert "var musicOccupiedBeforeRequest = isMusicOccupiedNow()" in source
    assert "var musicRateLimitedBeforeRequest" in source
    assert "requestBody.is_music_occupied = !!musicOccupiedBeforeRequest" in source
    assert (
        "requestBody.enabled_modes = requestBody.enabled_modes.filter(function (mode) "
        "{ return mode !== 'music'; });"
    ) in source
    assert source.index("var musicOccupiedBeforeRequest") < source.index(
        "var proactiveBody = JSON.stringify(requestBody)"
    )


def test_user_music_requests_retry_candidates_and_discard_stale_dispatches():
    source = APP_WEBSOCKET_PATH.read_text(encoding="utf-8")

    assert "response.type === 'music_play_candidates'" in source
    assert (
        "source: 'user'," in source
    )
    assert "requestId: response.request_id" in source
    assert "dispatchResult.canTryNextCandidate !== true" in source
    assert "_musicCandidateDispatchEpoch" in source
    assert "_musicCandidateDispatchQueue" in source
    assert "catch (error)" in source
    assert "canTryNextCandidate: true" in source
    assert "没有可用的音乐派发接口" in source
    assert "if (accepted === 'queued')" in source
    assert "return 'queued';" in source
    assert "window._latestMusicCandidateRequestId" in source
    assert "if (latestRequestId > 0 && requestId <= latestRequestId) return;" in source
    assert "window.cancelPendingMusicMediaReady(response.request_id);" in source


def test_new_track_cancels_pending_media_readiness_wait():
    source = MUSIC_UI_PATH.read_text(encoding="utf-8")
    send_source = source.split(
        "window.sendMusicMessageDetailed = async function", 1
    )[1].split("window.sendMusicMessage = async function", 1)[0]

    assert "let pendingMusicMediaReadyCancel = null;" in source
    assert "cancelWait = () => finish(false, 'superseded');" in source
    assert "if (pendingMusicMediaReadyCancel) pendingMusicMediaReadyCancel();" in send_source
    assert send_source.index("++latestMusicRequestToken") < send_source.index(
        "pendingMusicMediaReadyCancel()"
    )
    assert "cancelWait.requestId = requestId ?? null;" in source
    assert "window.cancelPendingMusicMediaReady = (requestId) =>" in source
    assert "nextRequestId < pendingRequestId" in source
    assert "window.cancelPendingMusicMediaReady(response.request_id);" in APP_WEBSOCKET_PATH.read_text(
        encoding="utf-8"
    )


def test_music_player_reports_confirmed_state_to_backend():
    player_source = MUSIC_UI_PATH.read_text(encoding="utf-8")
    router_source = WEBSOCKET_ROUTER_PATH.read_text(encoding="utf-8")

    assert "function reportMusicPlaybackState(state, track, playbackContext)" in player_source
    assert "function createMusicPlaybackReportContext(playbackId, options, track, token)" in player_source
    assert "function getOwnedMusicPlaybackReportContext(player, state)" in player_source
    assert "action: 'music_playback_state'" in player_source
    assert "localPlayer._musicPlaybackReportContext = playbackReportContext" in player_source
    assert "context.token !== latestMusicRequestToken" in player_source
    assert "getOwnedMusicPlaybackReportContext(boundPlayer, 'playing')" in player_source
    assert "getOwnedMusicPlaybackReportContext(boundPlayer, playbackState)" in player_source
    assert "getOwnedMusicPlaybackReportContext(boundPlayer, 'ended')" in player_source
    assert "getOwnedMusicPlaybackReportContext(boundPlayer, 'error')" in player_source
    assert ") !== reportContext" in player_source
    assert "reportMusicPlaybackState('playing', null, reportContext)" in player_source
    assert "reportMusicPlaybackState('ended', null, reportContext)" in player_source
    assert "reportMusicPlaybackState('error', null, reportContext)" in player_source
    assert 'elif action == "music_playback_state":' in router_source
    assert "handle_music_playback_state(" in router_source


def test_same_track_fast_path_rebuilds_missing_player_instance():
    player_source = MUSIC_UI_PATH.read_text(encoding="utf-8")

    fast_path = player_source.split(
        "if (isSameTrack(trackInfo) && isPlayerInDOM()) {",
        1,
    )[1].split("const currentToken = ++latestMusicRequestToken;", 1)[0]
    assert "if (!player) {" in fast_path
    assert "destroyMusicPlayer(true, false, true);" in fast_path
    assert fast_path.index("if (!player) {") < fast_path.index(
        "player._musicPlaybackReportContext = playbackReportContext;"
    )


def test_missing_music_cover_stays_out_of_data_and_uses_frontend_placeholder():
    player_source = MUSIC_UI_PATH.read_text(encoding="utf-8")
    player_style = MUSIC_UI_CSS_PATH.read_text(encoding="utf-8")
    crawler_source = MUSIC_CRAWLERS_PATH.read_text(encoding="utf-8")

    assert "'cover': cover or ''" in crawler_source
    assert "dummyimage.com" not in crawler_source
    assert "defaultCoverPath: '/static/assets/music/music-cover-placeholder.png'" in player_source
    assert "const normalizeMusicCoverUrl = (cover) =>" in player_source
    assert "hostname.endsWith('.music.126.net')" in player_source
    assert "parsed.protocol = 'https:'" in player_source
    assert "const normalizedCover = normalizeMusicCoverUrl(cover)" in player_source
    assert "thumbnailUrl: displayCoverUrl" in player_source
    assert "applyMusicCover" not in player_source
    assert player_source.count('class="music-bar-equalizer"') == 2
    assert player_source.count('class="music-bar-equalizer-bar"') == 6
    assert ".music-player-bar.is-playing .music-bar-equalizer-bar" in player_style
    assert "@keyframes musicBarEqualizer" in player_style
    assert "music-bar-fallback" not in player_source
    assert "dummyimage.com" not in player_source
    assert DEFAULT_MUSIC_COVER_PATH.stat().st_size > 0


def test_music_player_assets_are_versioned_with_the_page():
    pages_source = PAGES_ROUTER_PATH.read_text(encoding="utf-8")

    assert '_PROJECT_ROOT / "static/jukebox/music_ui.js"' in pages_source
    assert '_PROJECT_ROOT / "static/css/music_ui.css"' in pages_source
    assert '_PROJECT_ROOT / "static/assets/music/music-cover-placeholder.png"' in pages_source
    for template_path in MUSIC_PLAYER_TEMPLATES:
        template_source = template_path.read_text(encoding="utf-8")
        assert '/static/css/music_ui.css?v={{ static_asset_version }}' in template_source
        assert '/static/jukebox/music_ui.js?v={{ static_asset_version }}' in template_source


def test_all_locales_define_music_player_labels_and_failures():
    required = {
        "unknownTrack",
        "unknownArtist",
        "unknownSource",
        "volumeControl",
        "closePlayer",
        "trackTooLong",
        "loadTimeout",
        "loading",
        "playError",
        "loadError",
        "loginRequired",
        "playlistAmbiguous",
        "sourceEmpty",
    }

    for locale_path in sorted(LOCALES_DIR.glob("*.json")):
        data = json.loads(locale_path.read_text(encoding="utf-8"))
        assert required <= set(data["music"]), locale_path.name


def test_music_proxy_streams_one_upstream_response_and_tees_small_cache():
    source = MUSIC_ROUTER_PATH.read_text(encoding="utf-8")

    assert "StreamingResponse(" in source
    assert "_stream_music_response(" in source
    assert "async def _stream_music(" not in source
    assert "cache_body = bytearray() if cache_key else None" in source
    assert "if cache_key and cache_body is not None:" in source
