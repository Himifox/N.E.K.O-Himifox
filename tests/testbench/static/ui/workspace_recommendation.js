import { el } from './_dom.js';
import { renderRecommendationScenarios } from './recommendation/page_scenarios.js';
import { renderRecommendationRun } from './recommendation/page_run.js';
import { renderRecommendationResults } from './recommendation/page_results.js';
import { renderRecommendationCalibration } from './recommendation/page_calibration.js';
const PAGES = [['scenarios','Scenarios',renderRecommendationScenarios],['run','Run',renderRecommendationRun],['results','Results',renderRecommendationResults],['calibration','Calibration',renderRecommendationCalibration]];
const KEY='testbench:recommendation:active_subpage';
export function mountRecommendationWorkspace(host){host.classList.add('two-col');host.innerHTML='';const nav=el('div',{className:'subnav'}),pane=el('div');host.append(nav,pane);function select(id){const page=PAGES.find(p=>p[0]===id)||PAGES[0];localStorage.setItem(KEY,page[0]);[...nav.children].forEach(b=>b.classList.toggle('active',b.dataset.page===page[0]));pane.innerHTML='';const target=el('div',{className:'subpage active','data-subpage':page[0]});pane.append(target);Promise.resolve(page[2](target,select)).catch(err=>target.append(el('div',{className:'empty-state'},`加载失败: ${err.message}`)));}for(const [id,label] of PAGES)nav.append(el('button',{className:'subnav-item','data-page':id,onClick:()=>select(id)},label));select(localStorage.getItem(KEY)||'scenarios');}
