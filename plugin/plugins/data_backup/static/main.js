const PLUGIN_ID = 'data_backup';
const RUNS_URL = '/runs';
const GROUP_COPY = {
  core: ['核心数据', '配置、角色卡与长期记忆'],
  assets: ['模型资源', '角色立绘、Live2D、VRM、MMD、PngTuber 与创意工坊资源'],
};

let activeGroup = 'core';
let state = null;

const $ = (selector) => document.querySelector(selector);
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

function setNotice(message = '', type = '') {
  const notice = $('#notice');
  notice.textContent = message;
  notice.className = `notice ${type}`.trim();
}

function setBusy(busy) {
  document.querySelectorAll('button').forEach((button) => { button.disabled = busy; });
}

function formatBytes(bytes) {
  if (!bytes) return '0 B';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  const index = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1);
  return `${(bytes / (1024 ** index)).toFixed(index ? 1 : 0)} ${units[index]}`;
}

async function callPlugin(entryId, args = {}) {
  const created = await fetch(RUNS_URL, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ plugin_id: PLUGIN_ID, entry_id: entryId, args }),
  });
  if (!created.ok) throw new Error(`创建任务失败（HTTP ${created.status}）`);
  const createdPayload = await created.json();
  const runId = createdPayload.run_id || createdPayload.id;
  if (!runId) throw new Error('任务 ID 缺失');

  const deadline = Date.now() + 15 * 60 * 1000;
  while (Date.now() < deadline) {
    await sleep(500);
    const response = await fetch(`${RUNS_URL}/${encodeURIComponent(runId)}`, { cache: 'no-store' });
    if (!response.ok) continue;
    const record = await response.json();
    if (record.status === 'succeeded') {
      const exported = await fetch(`${RUNS_URL}/${encodeURIComponent(runId)}/export`, { cache: 'no-store' });
      if (!exported.ok) throw new Error(`读取结果失败（HTTP ${exported.status}）`);
      const payload = await exported.json();
      const item = (payload.items || []).find((candidate) => candidate.type === 'json');
      const result = item?.json || {};
      if (result.success === false || result.error) {
        throw new Error(result.error?.message || result.message || '插件调用失败');
      }
      return result.data || {};
    }
    if (['failed', 'canceled', 'timeout'].includes(record.status)) {
      throw new Error(record.error?.message || record.message || record.status);
    }
  }
  throw new Error('操作超时');
}

function render() {
  if (!state) return;
  const group = state.groups[activeGroup];
  const [title, description] = GROUP_COPY[activeGroup];
  $('#data-root').textContent = state.data_root;
  $('#backup-root').textContent = state.backup_root;
  $('#retention').textContent = `${state.retention} 份`;
  $('#group-title').textContent = title;
  $('#group-description').textContent = description;
  $('#paths').replaceChildren(...group.paths.map((path) => {
    const chip = document.createElement('span');
    chip.textContent = path;
    return chip;
  }));
  $('#snapshot-count').textContent = `${group.snapshots.length} 份`;

  const list = $('#snapshots');
  list.replaceChildren();
  if (!group.snapshots.length) {
    const empty = document.createElement('div');
    empty.className = 'empty';
    const mark = document.createElement('span');
    mark.className = 'empty-mark';
    mark.textContent = '◇';
    const title = document.createElement('strong');
    title.textContent = '还没有快照';
    const hint = document.createElement('span');
    hint.textContent = '创建第一份快照，为重要数据留一个安心的还原点。';
    empty.append(mark, title, hint);
    list.append(empty);
    return;
  }

  group.snapshots.forEach((snapshot) => {
    const row = document.createElement('article');
    row.className = 'snapshot';
    const meta = document.createElement('div');
    meta.className = 'snapshot-meta';
    const id = document.createElement('code');
    id.className = 'snapshot-id';
    id.textContent = snapshot.id;
    const detail = document.createElement('div');
    detail.className = 'snapshot-detail';
    detail.textContent = `${new Date(snapshot.created_at).toLocaleString()} · ${snapshot.file_count} 个文件 · ${formatBytes(snapshot.total_bytes)}`;
    meta.append(id, detail);

    const actions = document.createElement('div');
    actions.className = 'snapshot-actions';
    const restore = document.createElement('button');
    restore.type = 'button';
    restore.className = 'secondary';
    restore.textContent = '恢复';
    restore.addEventListener('click', () => restoreSnapshot(snapshot.id));
    const remove = document.createElement('button');
    remove.type = 'button';
    remove.className = 'danger';
    remove.textContent = '删除';
    remove.addEventListener('click', () => deleteSnapshot(snapshot.id));
    actions.append(restore, remove);
    row.append(meta, actions);
    list.append(row);
  });
}

async function refresh() {
  setBusy(true);
  setNotice('正在读取快照…');
  try {
    state = await callPlugin('backup_status');
    render();
    setNotice();
  } catch (error) {
    setNotice(error.message || String(error), 'error');
  } finally {
    setBusy(false);
  }
}

async function createSnapshot() {
  setBusy(true);
  setNotice('正在创建快照，请勿关闭页面…');
  try {
    const snapshot = await callPlugin('backup_create', { group: activeGroup });
    setNotice(`快照 ${snapshot.id} 已创建。`, 'success');
    state = await callPlugin('backup_status');
    render();
  } catch (error) {
    setNotice(error.message || String(error), 'error');
  } finally {
    setBusy(false);
  }
}

async function restoreSnapshot(snapshotId) {
  const confirmation = window.prompt(`恢复会替换当前组数据，并在操作前自动备份。请输入快照 ID 继续：\n${snapshotId}`);
  if (confirmation !== snapshotId) {
    if (confirmation !== null) setNotice('快照 ID 不匹配，已取消恢复。', 'error');
    return;
  }
  setBusy(true);
  setNotice('正在校验并恢复快照，请勿关闭 N.E.K.O…');
  try {
    const result = await callPlugin('backup_restore', { group: activeGroup, snapshot_id: snapshotId, confirmation });
    setNotice(`恢复完成；安全快照为 ${result.safety_snapshot}。请立即重启 N.E.K.O。`, 'success');
    state = await callPlugin('backup_status');
    render();
  } catch (error) {
    setNotice(error.message || String(error), 'error');
  } finally {
    setBusy(false);
  }
}

async function deleteSnapshot(snapshotId) {
  const confirmation = window.prompt(`删除后无法从列表恢复。请输入快照 ID 继续：\n${snapshotId}`);
  if (confirmation !== snapshotId) {
    if (confirmation !== null) setNotice('快照 ID 不匹配，已取消删除。', 'error');
    return;
  }
  setBusy(true);
  setNotice('正在删除快照…');
  try {
    await callPlugin('backup_delete', { group: activeGroup, snapshot_id: snapshotId, confirmation });
    state = await callPlugin('backup_status');
    render();
    setNotice(`快照 ${snapshotId} 已删除。`, 'success');
  } catch (error) {
    setNotice(error.message || String(error), 'error');
  } finally {
    setBusy(false);
  }
}

document.querySelectorAll('.tab').forEach((tab) => {
  tab.addEventListener('click', () => {
    activeGroup = tab.dataset.group;
    document.querySelectorAll('.tab').forEach((item) => item.classList.toggle('active', item === tab));
    render();
  });
});
$('#refresh').addEventListener('click', refresh);
$('#create').addEventListener('click', createSnapshot);
refresh();
