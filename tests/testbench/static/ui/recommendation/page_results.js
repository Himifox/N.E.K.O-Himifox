import { api } from '../../core/api.js';
import { el } from '../_dom.js';
import { renderSourceScoreTable } from './score_table.js';

const fmt = value => value == null ? '未评估' : Number(value).toFixed(3);

function statusPanel(run) {
  const selection = run.selection || {};
  return el('section', { className: 'card' }, el('h4', {}, '准入状态'),
    el('div', {}, `Execution: ${run.execution_status}`), el('div', {}, `Contracts: ${run.contract_status}`),
    el('div', {}, `Data quality: ${run.data_quality_status}`), el('div', {}, `Quality gate: ${run.quality_gate}`),
    el('pre', { className: 'recommendation-json' }, JSON.stringify(run.status_reasons || {}, null, 2)),
    el('div', { className: 'hint' },
      `Builtin ${selection.builtin_selected} · User ${selection.user_selected} · Ranking ${selection.ranking_eligible} · Labeled ${selection.relevance_labeled} · Contract ${selection.contract_only} · Sequence ${selection.sequence_cases}`));
}

function metricTable(run) {
  const table = el('table', { className: 'recommendation-score-table' });
  table.append(el('thead', {}, el('tr', {}, ...['Variant','Hit@1','nDCG@3','硬约束','执行错误','最大曝光','重复率'].map(x => el('th', {}, x)))));
  const body = el('tbody');
  for (const [variant, metric] of Object.entries(run.metrics || {})) {
    const transparent = metric.transparent_metrics || {}; const hit = transparent.hit_at_1 || {};
    const ndcg = transparent.ndcg_at_3 || {}; const hard = transparent.hard_constraints || {}; const errors = transparent.execution_errors || {};
    body.append(el('tr', {}, el('td', {}, variant), el('td', {}, `${hit.numerator}/${hit.denominator} = ${fmt(hit.value)}`),
      el('td', {}, `${fmt(ndcg.value)} / ${ndcg.denominator}`), el('td', {}, `${hard.violations}/${hard.evaluated_cases}`),
      el('td', {}, `${errors.errors}/${errors.executed_cases}`), el('td', {}, fmt(metric.max_source_exposure)),
      el('td', {}, fmt(metric.candidate_repeat_rate))));
  }
  table.append(body); return table;
}

function pairedPanel(run) {
  const section = el('section', {}, el('h4', {}, '逐场景配对变化'));
  for (const [variant, result] of Object.entries(run.comparisons || {})) {
    section.append(el('h5', {}, `${variant}: ${result.wins}胜 / ${result.losses}负 / ${result.ties}平 / ${result.not_comparable}不可比较`));
    for (const [label, key] of [['Wins','win_details'],['Losses','loss_details'],['Ties','tie_details']]) {
      const details = el('details', {}, el('summary', {}, `${label} (${(result[key] || []).length})`));
      for (const row of result[key] || []) details.append(el('div', { className: key === 'loss_details' ? 'empty-state' : 'hint' },
        `${row.scenario_id}: ${row.baseline_top1} → ${row.candidate_top1}; acceptable ${row.baseline_acceptable} → ${row.candidate_acceptable}; ${row.reason}`));
      section.append(details);
    }
  }
  return section;
}

function weightPanel(run) {
  const section = el('section', {}, el('h4', {}, '资源权重变化轨迹'),
    el('div', { className: 'hint' }, '离线模拟；未写入生产配置。'));
  for (const [variant, rows] of Object.entries(run.weight_changes || {})) {
    const table = el('table', { className: 'recommendation-score-table' });
    table.append(el('thead', {}, el('tr', {}, ...['资源','有效权重','调参调整','平均分','Top-1曝光'].map(x => el('th', {}, x)))));
    const body = el('tbody');
    for (const row of rows) { const a=row.baseline||{}, b=row.current||{};
      body.append(el('tr', {}, el('td', {}, row.source), el('td', {}, `${fmt(a.source_weight)} → ${fmt(b.source_weight)}`),
        el('td', {}, `${fmt(a.tuning_adjustment)} → ${fmt(b.tuning_adjustment)}`),
        el('td', {}, `${fmt(a.score)} → ${fmt(b.score)}`), el('td', {}, `${fmt(a.top1_exposure)} → ${fmt(b.top1_exposure)}`)));
    }
    table.append(body); section.append(el('h5', {}, `${run.baseline_variant} → ${variant}`), table);
  }
  return section;
}

function scenarioPanel(run) {
  const section = el('section', {}, el('h4', {}, '逐场景资源分数'));
  for (const [variant, cases] of Object.entries(run.cases_by_variant || {})) for (const row of cases) {
    const card = el('details', { className: 'card' }, el('summary', {}, `${row.scenario_id} · ${variant}`));
    if (row.error) card.append(el('div', { className: 'empty-state' }, row.error));
    else card.append(renderSourceScoreTable(row.snapshot, variant));
    section.append(card);
  }
  return section;
}

export async function renderRecommendationResults(host) {
  host.append(el('h2', {}, 'Recommendation Results')); const list = el('div', { className: 'recommendation-list' }); host.append(list);
  const response = await api.get('/api/recommendation-testbench/runs');
  if (!response.ok) return list.append(el('div', { className: 'empty-state' }, response.error.message));
  for (const summary of response.data.runs || []) {
    const detail = el('div', { className: 'recommendation-result-detail' });
    const view = el('button', { onClick: async () => { const out = await api.get(`/api/recommendation-testbench/runs/${summary.id}`);
      detail.innerHTML=''; if (out.ok) detail.append(statusPanel(out.data), metricTable(out.data), pairedPanel(out.data), weightPanel(out.data), scenarioPanel(out.data));
      else detail.textContent=out.error.message; } }, '查看数据表');
    const exp = el('button', { onClick: () => window.open(`/api/recommendation-testbench/runs/${summary.id}/export?format=markdown`, '_blank') }, 'Export Markdown');
    list.append(el('article', { className: `card recommendation-status-${summary.status}` }, el('h3', {}, summary.name),
      el('div', { className: 'hint' }, `${summary.execution_status}/${summary.contract_status}/${summary.data_quality_status} · gate ${summary.quality_gate}`), view, exp, detail));
  }
}
