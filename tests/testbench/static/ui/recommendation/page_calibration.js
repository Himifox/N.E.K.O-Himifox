import { api } from '../../core/api.js';
import { el } from '../_dom.js';

function renderTrace(target, data) {
  target.innerHTML = '';
  target.append(el('div', { className: 'empty-state' },
    '个性化轨迹运行在隔离 Testbench 沙盒中；调用生产 auto-safe tuning，但不会写入生产配置。'));
  for (const user of data.users || []) {
    target.append(el('h4', {}, `用户: ${user.user_id}`));
    const table = el('table', { className: 'recommendation-score-table' });
    table.append(el('thead', {}, el('tr', {},
      ...['轮次', '样本/反馈', '是否更新', '阻塞原因', '用户调整', '资源分数', 'Top-1'].map(x => el('th', {}, x)))));
    const body = el('tbody');
    for (const round of user.rounds || []) body.append(el('tr', {},
      el('td', {}, round.round), el('td', {}, `${round.observation_count}/${round.feedback_count}`),
      el('td', {}, round.applied ? '已更新' : '未更新'), el('td', {}, round.blocked_reason || '—'),
      el('td', {}, JSON.stringify(round.user_adjustments)), el('td', {}, JSON.stringify(round.resource_scores)),
      el('td', {}, round.top1_source || '无')));
    table.append(body); target.append(table);
  }
}

function feedbackRounds(dataset, size = 30) {
  const observations = dataset.observations || []; const feedback = dataset.feedback || [];
  const count = Math.max(observations.length, feedback.length); const rounds = [];
  for (let start = 0; start < count; start += size) rounds.push({
    observations: observations.slice(start, start + size), feedback: feedback.slice(start, start + size),
  });
  return rounds.length ? rounds : [{ observations: [], feedback: [] }];
}

export async function renderRecommendationCalibration(host) {
  host.append(el('h2', {}, 'Shadow Calibration & Personalization Trace'),
    el('p', { className: 'hint' }, '导入数据会经过生产 sanitizer；不会写入生产 tuning。'));
  const input = el('input', { type: 'file', accept: '.json,.jsonl' });
  const status = el('pre', { className: 'recommendation-json' });
  const datasets = el('select');
  const traceHost = el('section');
  const auditHost = el('pre', { className: 'recommendation-json' });
  const annotationEditor = el('textarea', { className: 'recommendation-json', rows: 16,
    placeholder: '加载标注任务后，在此编辑 annotations JSON 数组。' });
  const refresh = async () => {
    const res = await api.get('/api/recommendation-testbench/datasets'); datasets.innerHTML = '';
    for (const row of res.data?.datasets || []) datasets.append(el('option', { value: row.id },
      `${row.name} (${row.observation_count}/${row.feedback_count})`));
  };
  input.addEventListener('change', async () => {
    const file = input.files?.[0]; if (!file) return;
    const text = await file.text(); let parsed;
    try { parsed = file.name.endsWith('.jsonl') ? text.split(/\r?\n/).filter(Boolean).map(JSON.parse) : JSON.parse(text); }
    catch (err) { status.textContent = err.message; return; }
    const body = Array.isArray(parsed) ? { name: file.name, observations: parsed, feedback: [] }
      : { name: file.name, observations: parsed.observations || [], feedback: parsed.feedback || [] };
    const res = await api.post('/api/recommendation-testbench/datasets/import', body);
    status.textContent = res.ok ? JSON.stringify(res.data, null, 2) : res.error.message; await refresh();
  });
  const runCalibration = el('button', { onClick: async () => {
    if (!datasets.value) return;
    const res = await api.post('/api/recommendation-testbench/calibration', { dataset_id: datasets.value, variant: {} });
    status.textContent = res.ok ? JSON.stringify(res.data, null, 2) : res.error.message;
  } }, 'Run calibration');
  const runTrace = el('button', { onClick: async () => {
    if (!datasets.value) return;
    const dataset = await api.get(`/api/recommendation-testbench/datasets/${datasets.value}`);
    if (!dataset.ok) { status.textContent = dataset.error.message; return; }
    const res = await api.post('/api/recommendation-testbench/personalization/trace', {
      scenario_id: 'competition_15', users: [{ user_id: `dataset-${datasets.value.slice(0, 8)}`,
        rounds: feedbackRounds(dataset.data) }],
    });
    if (res.ok) renderTrace(traceHost, res.data); else status.textContent = res.error.message;
  } }, 'Run personalization trace');
  const audit = el('button', { onClick: async () => {
    if (!datasets.value) return;
    const res = await api.get(`/api/recommendation-testbench/datasets/${datasets.value}/quality`);
    auditHost.textContent = res.ok ? JSON.stringify(res.data, null, 2) : res.error.message;
  } }, 'Audit P44 readiness');
  const loadTasks = el('button', { onClick: async () => {
    if (!datasets.value) return;
    const res = await api.get(`/api/recommendation-testbench/datasets/${datasets.value}/annotations`);
    if (!res.ok) { auditHost.textContent = res.error.message; return; }
    const annotations = (res.data.tasks || []).map(task => task.annotation || ({
      turn_id: task.turn_id, should_recommend: true, acceptable_top1_sources: [], relevance: {},
      must_filter_candidate_ids: [], expected_filter_reasons: {}, interruption_level: 'acceptable',
      privacy_risk: 'none', score_diagnosis: 'reasonable', issue_layer: 'none', comment: '',
      annotator_id: 'annotator-1', reviewed: false, reviewer_id: '',
    }));
    annotationEditor.value = JSON.stringify(annotations, null, 2);
    auditHost.textContent = JSON.stringify(res.data.summary, null, 2);
  } }, 'Load annotation tasks');
  const saveAnnotations = el('button', { onClick: async () => {
    if (!datasets.value) return; let annotations;
    try { annotations = JSON.parse(annotationEditor.value); }
    catch (err) { auditHost.textContent = err.message; return; }
    const res = await api.post(`/api/recommendation-testbench/datasets/${datasets.value}/annotations`, { annotations });
    auditHost.textContent = res.ok ? JSON.stringify(res.data, null, 2) : JSON.stringify(res.error, null, 2);
  } }, 'Validate & save annotations');
  const promote = el('button', { onClick: async () => {
    if (!datasets.value) return;
    const res = await api.post(`/api/recommendation-testbench/datasets/${datasets.value}/golden`, {});
    auditHost.textContent = res.ok ? JSON.stringify(res.data, null, 2) : JSON.stringify(res.error, null, 2);
    if (res.ok) await refresh();
  } }, 'Promote to shadow_golden');
  host.append(input, datasets, runCalibration, runTrace, audit, loadTasks, saveAnnotations, promote,
    status, traceHost, el('h3', {}, '人工标注复核'), annotationEditor, auditHost); await refresh();
}
