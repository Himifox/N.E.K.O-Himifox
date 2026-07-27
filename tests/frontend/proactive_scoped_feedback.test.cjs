const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const sourcePath = path.resolve(__dirname, '..', '..', 'static', 'app', 'app-proactive.js');
const source = fs.readFileSync(sourcePath, 'utf8');

function sourceBetween(startMarker, endMarker) {
  const start = source.indexOf(startMarker);
  const end = source.indexOf(endMarker, start);
  assert.ok(start >= 0 && end > start, `source slice not found: ${startMarker}`);
  return source.slice(start, end);
}

function createSubmitApi(responsePayload) {
  let request = null;
  const context = {
    window: {
      lanlan_config: { lanlan_name: 'YUI' },
      addEventListener() {},
      nekoLocalMutationSecurity: {
        async getMutationHeaders() { return { 'X-CSRF-Token': 'test' }; },
      },
    },
    fetch: async (url, options) => {
      request = { url, options };
      return {
        ok: true,
        status: 200,
        async json() { return responsePayload; },
      };
    },
  };
  vm.createContext(context);
  vm.runInContext(
    `${sourceBetween(
      'async function _submitProactiveScopedFeedback',
      'function _showProactiveFeedbackActions',
    )}\nthis.submit = _submitProactiveScopedFeedback;`,
    context,
  );
  return { submit: context.submit, getRequest: () => request };
}

test('scoped feedback payload carries no source material or candidate identifiers', async () => {
  const harness = createSubmitApi({
    success: true,
    logged: true,
    state_updated: true,
    feedback_scope: 'source_affinity',
  });

  await harness.submit(
    { turn_id: 'turn-1', source_type: 'news', source_feedback_available: true },
    'source_not_interested',
  );

  const request = harness.getRequest();
  const payload = JSON.parse(request.options.body);
  assert.equal(request.url, '/api/proactive/recommendation/feedback');
  assert.equal(request.options.headers['X-CSRF-Token'], 'test');
  assert.deepEqual(Object.keys(payload).sort(), [
    'event_type', 'lanlan_name', 'metadata', 'turn_id',
  ]);
  assert.equal(payload.event_type, 'source_not_interested');
  assert.deepEqual(payload.metadata, { ui_generation: 'dual_scope_v1' });
  assert.equal(JSON.stringify(payload).includes('candidate'), false);
  assert.equal(JSON.stringify(payload).includes('news'), false);
});

test('logged diagnostic feedback is not presented as an applied preference', async () => {
  const harness = createSubmitApi({
    success: true,
    logged: true,
    state_updated: false,
    state_reason: 'pending_material_mismatch',
  });

  await assert.rejects(
    harness.submit({ turn_id: 'turn-2' }, 'source_not_interested'),
    /pending_material_mismatch/,
  );
});

test('feedback actions require a delivered turn context before rendering', () => {
  assert.match(source, /context\.ui_generation !== 'dual_scope_v1'/);
  assert.match(source, /context\.turn_id !== targetTurnId/);
  assert.match(source, /realisticGeminiCurrentTurnId !== targetTurnId/);
  assert.match(source, /context\.source_feedback_available === true/);
});

test('feedback actions render through the visible cross-window React chat', () => {
  assert.match(source, /window\.__nekoMirrorChatAppend/);
  assert.match(source, /role: 'system'/);
  assert.match(source, /type: 'buttons'/);
  assert.match(source, /react-chat-window:action/);
  assert.match(source, /window\.__nekoMirrorChatUpdate/);
});
