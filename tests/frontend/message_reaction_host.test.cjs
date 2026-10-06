const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const test = require('node:test');

const base = path.join(__dirname, '../../static/app/app-react-chat-window');
const sources = ['geometry-and-messages.js', 'message-bundle-actions-and-prompts.js']
  .map(file => fs.readFileSync(path.join(base, file), 'utf8'));
const flush = () => new Promise(resolve => setImmediate(resolve));

function fixture() {
  const calls = [];
  const timers = new Map();
  let timerId = 0;
  const I = {
    _sortKeySeq: 0,
    state: { messages: [], _galgameRequestSeq: 0 },
    renderWindow() {},
    isCatLocalChatActive() { return false; },
  };
  const window = {
    __appReactChatWindowParts: I,
    appState: { lanlan_name: 'Neko' },
    nekoLocalMutationSecurity: { async getMutationHeaders() { return { 'X-CSRF-Token': 'test-csrf' }; } },
  };
  const ctx = vm.createContext({
    window, console, Date, AbortController,
    setTimeout(fn) { const id = ++timerId; timers.set(id, fn); return id; },
    clearTimeout(id) { timers.delete(id); },
    fetch(url, options) {
      return new Promise((resolve, reject) => {
        calls.push({ url, options, body: JSON.parse(options.body), resolve, reject });
      });
    },
  });
  for (const source of sources) vm.runInContext(source, ctx);
  I.renderWindow = () => {};
  I.invalidatePendingGalgameRequest = () => false;
  return { I, window, calls, expire() { for (const fn of [...timers.values()]) fn(); } };
}

function message(id = 'user-1', extra = {}) {
  return { id, role: 'user', author: 'You', time: '10:00', status: 'sent',
    blocks: [{ type: 'text', text: `hello ${id}` }], ...extra };
}

function deliver(call, reaction = { emoji: '❤️', author: 'Neko' }, messageId = call.body.message_id) {
  call.resolve({ ok: true, async json() { return { message_id: messageId, reaction }; } });
}

test('background reaction uses security headers, persona identity, bounded previous context and exact message ID', async () => {
  const { I, calls } = fixture();
  I.setMessages([message('older'), message('reply', { role: 'assistant', author: 'Neko' })]);
  assert.equal(calls.length, 0, 'restored history must not launch model requests');
  I.appendMessage(message());
  await flush();
  assert.equal(calls.length, 1);
  const call = calls[0];
  assert.equal(call.url, '/api/chat/reaction');
  assert.equal(call.options.headers['X-CSRF-Token'], 'test-csrf');
  assert.equal(call.body.lanlan_name, 'Neko');
  assert.equal(call.body.message_id, 'user-1');
  assert.deepEqual(call.body.context, [
    { role: 'user', text: 'hello older' }, { role: 'assistant', text: 'hello reply' },
  ]);
  deliver(call);
  await flush();
  const reacted = I.state.messages.find(m => m.id === 'user-1');
  assert.equal(reacted.reaction.emoji, '❤️');
  assert.equal(reacted.blocks[0].text, 'hello user-1');
  const cloned = I.cloneMessage(reacted);
  cloned.reaction.emoji = '😂';
  assert.equal(reacted.reaction.emoji, '❤️', 'snapshot must not share mutable reaction objects');
});

for (const status of ['sending', 'streaming']) {
  test(`${status} waits for final sent state and subsequent updates do not duplicate requests`, async () => {
    const { I, calls } = fixture();
    I.appendMessage(message('user-1', { status }));
    await flush();
    assert.equal(calls.length, 0);
    I.updateMessage('user-1', { status: 'sent' });
    I.updateMessage('user-1', { time: '10:01' });
    await flush();
    assert.equal(calls.length, 1);
    deliver(calls[0]);
    await flush();
    assert.equal(I.state.messages[0].reaction.emoji, '❤️');
    assert.equal(calls.length, 1);
  });
}

test('non-user, failed, image-only, tutorial and local cat messages never call the model', async () => {
  const { I, calls } = fixture();
  for (const role of ['assistant', 'system', 'tool']) I.appendMessage(message(role, { role }));
  I.appendMessage(message('failed', { status: 'failed' }));
  I.appendMessage(message('image', { blocks: [{ type: 'image', url: '/image.png' }] }));
  I.appendMessage(message('yui-guide-demo'));
  I.appendMessage(message('icebreaker-user-demo'));
  I.isCatLocalChatActive = () => true;
  I.appendMessage(message('local-cat'));
  await flush();
  assert.equal(calls.length, 0);
});

test('out-of-order reactions attach to their own messages', async () => {
  const { I, calls } = fixture();
  I.appendMessage(message('first'));
  I.appendMessage(message('second'));
  await flush();
  deliver(calls[1], { emoji: '🎉', author: 'Neko' });
  deliver(calls[0], { emoji: '🤗', author: 'Neko' });
  await flush();
  assert.deepEqual(Array.from(I.state.messages, m => m.reaction.emoji), ['🤗', '🎉']);
});

test('burst messages wait for available slots without losing or duplicating decisions', async () => {
  const { I, calls } = fixture();
  for (let index = 0; index < 8; index++) I.appendMessage(message(`burst-${index}`));
  await flush();
  assert.equal(calls.length, 3, 'only three model requests may run concurrently');
  I.updateMessage('burst-3', { time: '10:01' });
  for (let index = 0; index < 8; index++) {
    assert.equal(calls[index].body.message_id, `burst-${index}`);
    deliver(calls[index], index % 2 ? null : { emoji: '❤️', author: 'Neko' });
    await flush();
    assert.equal(calls.length, Math.min(8, index + 4));
  }
  assert.equal(new Set(calls.map(call => call.body.message_id)).size, 8);
  assert.equal(I.state.messages.filter(item => item.reaction).length, 4);
});

for (const change of ['clear', 'restore', 'character', 'edit', 'remove', 'failed', 'icebreaker']) {
  test(`queued reactions are invalidated after ${change}`, async () => {
    const { I, window, calls } = fixture();
    for (let index = 0; index < 4; index++) I.appendMessage(message(`queued-${index}`));
    await flush();
    assert.equal(calls.length, 3);
    if (change === 'clear') I.clearMessages();
    if (change === 'restore') I.setMessages([message('queued-3')]);
    if (change === 'character') window.appState.lanlan_name = 'Other';
    if (change === 'edit') I.updateMessage('queued-3', { blocks: [{ type: 'text', text: 'changed' }] });
    if (change === 'remove') I.removeMessage('queued-3');
    if (change === 'failed') I.updateMessage('queued-3', { status: 'failed' });
    if (change === 'icebreaker') I.state.messages.find(m => m.id === 'queued-3').source = 'new_user_icebreaker';
    for (const call of calls.slice()) deliver(call, null);
    await flush();
    assert.equal(calls.length, 3, 'a stale queued message must never reach the model');
    I.clearMessages();
  });
}

test('timeouts release slots for queued messages without starting duplicate requests', async () => {
  const { I, calls, expire } = fixture();
  for (let index = 0; index < 4; index++) I.appendMessage(message(`timeout-${index}`));
  await flush();
  expire();
  await flush();
  assert.equal(calls.length, 4);
  assert.equal(calls[3].body.message_id, 'timeout-3');
  assert.ok(calls.slice(0, 3).every(call => call.options.signal.aborted));
  deliver(calls[3]);
  await flush();
  assert.equal(I.state.messages[3].reaction.emoji, '❤️');
});

test('accepted late reactions notify export preview after updating the host snapshot', async () => {
  const { I, window, calls } = fixture();
  const refreshed = [];
  window.appChatExport = { refreshMessageReaction(id) {
    refreshed.push({ id, emoji: I.state.messages.find(m => m.id === id).reaction.emoji });
  } };
  I.appendMessage(message());
  await flush();
  deliver(calls[0]);
  await flush();
  assert.deepEqual(refreshed, [{ id: 'user-1', emoji: '❤️' }]);
});

for (const change of ['clear', 'restore', 'character', 'edit', 'remove', 'failed']) {
  test(`pending response is ignored after ${change}`, async () => {
    const { I, window, calls } = fixture();
    I.appendMessage(message());
    await flush();
    const old = calls[0];
    if (change === 'clear') { I.clearMessages(); I.appendMessage(message()); }
    if (change === 'restore') I.setMessages([message()]);
    if (change === 'character') window.appState.lanlan_name = 'Other';
    if (change === 'edit') I.updateMessage('user-1', { blocks: [{ type: 'text', text: 'changed' }] });
    if (change === 'remove') I.removeMessage('user-1');
    if (change === 'failed') I.updateMessage('user-1', { status: 'failed' });
    deliver(old);
    await flush();
    assert.equal(I.state.messages.some(m => m.reaction), false);
    if (change === 'clear' || change === 'restore' || change === 'remove') {
      assert.equal(old.options.signal.aborted, true);
    }
    I.clearMessages();
  });
}

for (const result of ['null', 'bad-id', 'bad-emoji', 'bad-author', 'network', 'http']) {
  test(`optional ${result} result leaves normal chat untouched`, async () => {
    const { I, calls } = fixture();
    I.appendMessage(message());
    await flush();
    const call = calls[0];
    if (result === 'network') call.reject(new Error('network'));
    else if (result === 'http') call.resolve({ ok: false });
    else if (result === 'null') deliver(call, null);
    else if (result === 'bad-id') deliver(call, undefined, 'another-message');
    else if (result === 'bad-author') deliver(call, { emoji: '❤️', author: 'Other' });
    else deliver(call, { emoji: '<img src=x onerror=alert(1)>', author: 'Neko' });
    await flush();
    assert.equal(I.state.messages[0].reaction, undefined);
    assert.equal(I.state.messages[0].status, 'sent');
    assert.equal(I.state.messages[0].blocks[0].text, 'hello user-1');
  });
}

test('deadline cancels hung security lookup and releases concurrency slots without a late fetch', async () => {
  const { I, window, calls, expire } = fixture();
  let unlock;
  window.nekoLocalMutationSecurity.getMutationHeaders = () => new Promise(resolve => { unlock = resolve; });
  I.appendMessage(message());
  expire();
  await flush();
  unlock({});
  await flush();
  assert.equal(calls.length, 0);
  window.nekoLocalMutationSecurity.getMutationHeaders = async () => ({});
  I.appendMessage(message('next'));
  await flush();
  assert.equal(calls.length, 1);
  deliver(calls[0]);
  await flush();
  assert.equal(I.state.messages[1].reaction.emoji, '❤️');
});

// Every configured emoji must survive the host validation and message update.
for (const emoji of ['😊', '😄', '😃', '🙂', '😌', '🤔', '🧐', '💭', '❓', '👍', '✅', '🙌', '💪', '🎉', '🙏', '🤝', '😮', '👀', '⚠️', '💡', '😔', '😢', '😅', '🙇', '🥳', '✨', '🌟', '💻', '🤖', '📚', '🔧', '❤️', '⭐', '🔥', '🚀', '📌', '😂', '🤗']) {
  test(`supports configured reaction ${emoji}`, async () => {
    const { I, calls } = fixture();
    I.appendMessage(message());
    await flush();
    deliver(calls[0], { emoji, author: 'Neko' });
    await flush();
    assert.equal(I.state.messages[0].reaction.emoji, emoji);
    assert.equal(I.state.messages[0].blocks[0].text, 'hello user-1');
  });
}
