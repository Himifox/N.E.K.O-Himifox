const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const musicUiPath = path.resolve(__dirname, '..', '..', 'static', 'jukebox', 'music_ui.js');
const source = fs.readFileSync(musicUiPath, 'utf8');

function sourceBetween(startMarker, endMarker) {
  const start = source.indexOf(startMarker);
  const end = source.indexOf(endMarker, start);
  assert.ok(start >= 0 && end > start, `music timing source slice not found: ${startMarker}`);
  return source.slice(start, end);
}

function createTimingApi() {
  let now = 0;
  const context = {
    Date: { now: () => now },
    performance: { now: () => now },
    console,
  };
  vm.createContext(context);
  vm.runInContext(
    `${sourceBetween('const SKIP_CONFIG =', '// 从 localStorage 恢复冷却状态')}
     ${sourceBetween('function musicCloseFeedbackEventType', '// 用户关闭播放器时结算')}
     globalThis.api = {
       resetMusicPlaybackTiming,
       startActiveMusicPlayback,
       stopActiveMusicPlayback,
       getActiveMusicPlaybackMs,
       musicCloseFeedbackEventType,
     };`,
    context,
  );
  return {
    api: context.api,
    setNow(value) { now = value; },
  };
}

test('active playback timing excludes stopped intervals and is idempotent', () => {
  const timing = createTimingApi();
  timing.setNow(1_000);
  timing.api.startActiveMusicPlayback();
  timing.setNow(2_000);
  timing.api.startActiveMusicPlayback();
  timing.setNow(3_500);
  assert.equal(timing.api.stopActiveMusicPlayback(), 2_500);

  timing.setNow(13_500);
  assert.equal(timing.api.getActiveMusicPlaybackMs(), 2_500);
  timing.api.startActiveMusicPlayback();
  timing.setNow(19_000);
  assert.equal(timing.api.stopActiveMusicPlayback(), 8_000);
  assert.equal(timing.api.stopActiveMusicPlayback(), 8_000);

  timing.api.resetMusicPlaybackTiming();
  assert.equal(timing.api.getActiveMusicPlaybackMs(), 0);
});

test('close classification prefers active playback and falls back to wall time', () => {
  const { api } = createTimingApi();

  assert.equal(api.musicCloseFeedbackEventType({
    active_playback_ms: 2_500,
    played_wall_ms: 24_000,
  }), 'music_hard_skip');
  assert.equal(api.musicCloseFeedbackEventType({
    active_playback_ms: 8_000,
    played_wall_ms: 24_000,
  }), 'music_early_close');
  assert.equal(api.musicCloseFeedbackEventType({
    active_playback_ms: 12_252,
    played_wall_ms: 23_940,
    completion_ratio: 0.037,
  }), 'music_early_close');
  assert.equal(api.musicCloseFeedbackEventType({
    active_playback_ms: 18_000,
    played_wall_ms: 24_000,
  }), 'music_normal_close');
  assert.equal(api.musicCloseFeedbackEventType({
    played_wall_ms: 8_000,
  }), 'music_early_close');
  assert.equal(api.musicCloseFeedbackEventType({
    active_playback_ms: null,
    played_wall_ms: 8_000,
  }), 'music_early_close');
});
