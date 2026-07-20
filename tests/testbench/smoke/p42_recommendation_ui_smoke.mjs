import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../..');
const read = relative => fs.readFileSync(path.join(root, relative), 'utf8');
const app = read('tests/testbench/static/app.js');
const workspace = read('tests/testbench/static/ui/workspace_recommendation.js');
const run = read('tests/testbench/static/ui/recommendation/page_run.js');
const calibration = read('tests/testbench/static/ui/recommendation/page_calibration.js');
const scenarios = read('tests/testbench/static/ui/recommendation/page_scenarios.js');
const scores = read('tests/testbench/static/ui/recommendation/score_table.js');
const results = read('tests/testbench/static/ui/recommendation/page_results.js');

if (!app.includes("id: 'recommendation'") || !workspace.includes("['scenarios','Scenarios'") || !workspace.includes("['calibration','Calibration'")) throw new Error('Recommendation workspace contract missing');
if (!run.includes('button.disabled=true') || run.includes('AbortController')) throw new Error('Run mutation safety contract missing');
if (!calibration.includes("accept: '.json,.jsonl'") || !calibration.includes('/datasets/import')) throw new Error('Calibration import contract missing');
if (!calibration.includes('/personalization/trace') || !calibration.includes('Run personalization trace') || !calibration.includes('不会写入生产')) throw new Error('Personalization trace contract missing');
if (!calibration.includes('Audit P44 readiness') || !calibration.includes('Load annotation tasks') || !calibration.includes('Promote to shadow_golden')) throw new Error('P44 annotation workspace contract missing');
if (!calibration.includes('/timing-audit') || !calibration.includes('Timing schema v3 审计') || !calibration.includes('不会进入 timing/fatigue 分析')) throw new Error('P44-F2 timing audit workspace contract missing');
if (!scores.includes('最高分') || !scores.includes('Top-1') || !scores.includes('source_scores')) throw new Error('Resource score table contract missing');
if (!results.includes('资源权重变化轨迹') || !results.includes('未写入生产配置')) throw new Error('Weight trace contract missing');
if (!results.includes('Quality gate') || !results.includes('Data quality') || !results.includes('逐场景配对变化')) throw new Error('Layered gate/paired detail contract missing');
if (!scenarios.includes('/coverage') || !scenarios.includes('序列执行轨迹') || !scenarios.includes('Top-1翻转')) throw new Error('Coverage/sequence UI contract missing');
console.log('P42 RECOMMENDATION UI SMOKE OK');
