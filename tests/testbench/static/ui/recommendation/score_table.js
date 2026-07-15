import { el } from '../_dom.js';

export function renderSourceScoreTable(snapshot, title = '资源分数') {
  const scores = snapshot?.source_scores && Object.keys(snapshot.source_scores).length
    ? snapshot.source_scores
    : deriveSourceScores(snapshot?.ranked_candidates || []);
  const table = el('table', { className: 'recommendation-score-table' });
  table.append(el('thead', {}, el('tr', {},
    el('th', {}, '资源'),
    el('th', {}, '最高分'),
    el('th', {}, '候选数'),
    el('th', {}, '状态'),
  )));
  const body = el('tbody');
  const top1 = snapshot?.top1_source_type;
  for (const [source, info] of Object.entries(scores)) {
    const details = el('details', {}, el('summary', {}, Number(info.score).toFixed(3)));
    for (const candidate of info.candidates || []) {
      details.append(el('div', { className: 'hint' },
        `${candidate.id} · ${Number(candidate.score).toFixed(3)} · ${candidate.topic || ''}`));
    }
    body.append(el('tr', { className: source === top1 ? 'recommendation-score-top1' : '' },
      el('td', {}, source),
      el('td', {}, details),
      el('td', {}, info.candidate_count),
      el('td', {}, source === top1 ? 'Top-1' : ''),
    ));
  }
  for (const [candidateId, reason] of Object.entries(snapshot?.filtered_reasons || {})) {
    body.append(el('tr', { className: 'recommendation-score-filtered' },
      el('td', {}, candidateId.split(':')[0]),
      el('td', {}, '—'),
      el('td', {}, '0'),
      el('td', {}, `已过滤: ${reason}`),
    ));
  }
  table.append(body);
  return el('section', { className: 'recommendation-score-section' },
    el('h4', {}, title),
    table,
    el('div', { className: 'hint' },
      `Top-1: ${top1 || '无'} · 分差: ${snapshot?.score_gap ?? '—'}`),
  );
}

function deriveSourceScores(candidates) {
  const result = {};
  for (const candidate of candidates) {
    const source = candidate.source_type || 'unknown';
    if (!result[source]) result[source] = { score: null, candidate_count: 0, candidates: [] };
    const bucket = result[source];
    const score = Number(candidate.score || 0);
    bucket.score = bucket.score == null ? score : Math.max(bucket.score, score);
    bucket.candidate_count += 1;
    bucket.candidates.push({
      id: candidate.id,
      topic: candidate.topic,
      score,
      quality: candidate.quality,
      freshness: candidate.freshness,
    });
  }
  return Object.fromEntries(Object.entries(result).sort((a, b) => b[1].score - a[1].score));
}
