import { api } from '../../core/api.js';
import { el } from '../_dom.js';
import { renderSourceScoreTable } from './score_table.js';

function renderSequence(result) {
  const section = el('section', {}, el('h4', {}, '序列执行轨迹'));
  const table = el('table', { className: 'recommendation-score-table' });
  table.append(el('thead', {}, el('tr', {},
    ...['Step', '资源分数', 'Top-1', '分差', '判定'].map(x => el('th', {}, x)))));
  const body = el('tbody');
  for (const step of result.sequence_steps || []) {
    const snapshot = step.snapshot || {};
    const scores = Object.entries(snapshot.source_scores || {})
      .map(([source, info]) => `${source}: ${Number(info.score).toFixed(3)}`).join(' · ');
    body.append(el('tr', {}, el('td', {}, step.step_id), el('td', {}, scores || '无候选'),
      el('td', {}, snapshot.top1_source_type || '无'),
      el('td', {}, snapshot.score_gap == null ? '—' : Number(snapshot.score_gap).toFixed(3)),
      el('td', {}, step.quality_failures?.length ? `FAIL: ${step.quality_failures.map(x => x.code).join(', ')}` : 'PASS')));
  }
  table.append(body); section.append(table);
  for (const transition of result.transitions || []) section.append(el('div', { className: 'hint' },
    `${transition.from} → ${transition.to}: ${JSON.stringify(transition.source_score_deltas)}${transition.top1_changed ? ' · Top-1翻转' : ''}`));
  return section;
}

function renderCoverage(data) {
  return el('section', { className: 'card' }, el('h3', {}, '场景覆盖审计'),
    el('div', {}, `场景 ${data.scenario_count} · 唯一输入 ${data.unique_input_count}`),
    el('div', {}, `因素覆盖: ${JSON.stringify(data.factor_coverage || {})}`),
    el('div', { className: data.missing_factors?.length ? 'empty-state' : 'hint' },
      `待补因素: ${(data.missing_factors || []).join(', ') || '无'}`),
    el('div', {}, `目标来源分布: ${JSON.stringify(data.source_target_distribution || {})}`));
}

export async function renderRecommendationScenarios(host) {
  host.append(el('h2', {}, 'Recommendation Scenarios'),
    el('p', { className: 'hint' }, '冻结输入、离线确定性运行；内置场景需 Duplicate 后编辑。'));
  const coverage = await api.get('/api/recommendation-testbench/coverage');
  if (coverage.ok) host.append(renderCoverage(coverage.data));
  const filter = el('input', { placeholder: '按 ID、标签、stage、kind 或 factor 筛选' });
  const list = el('div', { className: 'recommendation-list' }); host.append(filter, list);
  const response = await api.get('/api/recommendation-testbench/scenarios');
  if (!response.ok) return list.append(el('div', { className: 'empty-state' }, response.error.message));
  const rows = response.data.scenarios || [];
  const render = () => {
    list.innerHTML = ''; const q = filter.value.toLowerCase();
    for (const row of rows.filter(item => JSON.stringify(item).toLowerCase().includes(q))) {
      const editor = el('textarea', { className: 'recommendation-editor', disabled: row.source === 'builtin' });
      const detail = el('div', { className: 'recommendation-result-detail' });
      const load = async () => { const out = await api.get(`/api/recommendation-testbench/scenarios/${row.id}`); if (out.ok) {
        const data = { ...out.data }; for (const key of ['_source','has_builtin','has_user','overriding_builtin']) delete data[key];
        editor.value = JSON.stringify(data, null, 2);
      } };
      const preview = el('button', { onClick: async () => {
        const out = await api.post('/api/recommendation-testbench/preview', { scenario_id: row.id, variant: { id: 'production_default' } });
        detail.innerHTML = '';
        if (!out.ok) detail.textContent = out.error.message;
        else if (out.data.sequence_steps) detail.append(renderSequence(out.data));
        else detail.append(renderSourceScoreTable(out.data.snapshot));
      } }, 'Preview');
      const validate = el('button', { onClick: async () => { try {
        const out = await api.post('/api/recommendation-testbench/scenarios/validate', { scenario: JSON.parse(editor.value) });
        detail.textContent = JSON.stringify(out.ok ? out.data : out.error, null, 2);
      } catch (err) { detail.textContent = err.message; } } }, 'Validate');
      const save = el('button', { disabled: row.source === 'builtin', onClick: async () => { try {
        const out = await api.post('/api/recommendation-testbench/scenarios', { scenario: JSON.parse(editor.value) });
        detail.textContent = JSON.stringify(out.ok ? out.data : out.error, null, 2);
      } catch (err) { detail.textContent = err.message; } } }, 'Save');
      const duplicate = el('button', { onClick: async () => {
        const target = `${row.id}_copy`; const out = await api.post('/api/recommendation-testbench/scenarios/duplicate',
          { source_id: row.id, target_id: target, overwrite: false });
        detail.textContent = out.ok ? `已复制为 ${target}，刷新后编辑` : out.error.message;
      } }, 'Duplicate');
      const remove = el('button', { disabled: !row.has_user, onClick: async () => {
        const out = await api.delete(`/api/recommendation-testbench/scenarios/${row.id}`);
        detail.textContent = out.ok ? '已删除，刷新后生效' : out.error.message;
      } }, 'Delete user copy');
      list.append(el('article', { className: 'card' }, el('h3', {}, row.name),
        el('div', { className: 'hint' }, `${row.id} · ${row.stage} · ${row.kind} · ${row.factor_under_test || 'legacy'} · ${row.source}`),
        preview, validate, save, duplicate, remove, editor, detail)); load();
    }
  };
  filter.addEventListener('input', render); render();
}
