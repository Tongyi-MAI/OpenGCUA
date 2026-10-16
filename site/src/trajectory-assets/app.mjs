import {defaultRun, matchesTask, stepDetails} from './core.mjs';
import {FramePlayer} from './video.mjs';

const $ = id => document.getElementById(id);
let language = 'en';
try { language = localStorage.getItem('ogcua-language') === 'zh' ? 'zh' : 'en'; } catch {}
const t = (en, zh) => language === 'zh' ? zh : en;
const state = {catalog: null, model: null, tasks: [], task: null, run: null, record: null,
  player: null, step: 0, tab: 'action', playing: false, request: 0, frameRequest: 0, route: 0};
const worker = new Worker(new URL('./trace-worker.mjs', import.meta.url), {type: 'module'});
const indexCache = new Map();
let base;
const score = value => value == null ? '—' : `${(value * 100).toFixed(1).replace(/\.0$/, '')}%`;
const duration = seconds => seconds == null ? '—' : `${Math.floor(seconds / 60)}m ${Math.round(seconds % 60)}s`;
const tokens = value => value == null ? '—' : `${(value / 1000).toFixed(1)}K`;
const outcome = value => value == null ? t('Unscored', '未评分') : value >= 1 ? t('Full success', '完整成功') : value > 0 ? t('Partial', '部分成功') : t('Zero score', '零分');
function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text != null) node.textContent = text;
  if (className) node.className = className;
  return node;
}
function setLanguage(next) {
  language = next;
  document.documentElement.lang = next === 'zh' ? 'zh-CN' : 'en';
  document.querySelectorAll('[data-en]').forEach(node => { node.textContent = node.dataset[next]; });
  $('en').setAttribute('aria-pressed', next === 'en');
  $('zh').setAttribute('aria-pressed', next === 'zh');
  $('search').placeholder = t('Search by task or instruction…', '搜索任务编号或指令…');
  try { localStorage.setItem('ogcua-language', next); } catch {}
  if (state.catalog) renderLeaderboard();
  if (state.model) { renderTasks(); renderAttempt(); }
  if (state.record) renderStep(false);
}
function renderLeaderboard() {
  const sort = $('sort').value;
  const ordered = [...state.catalog.models].sort((a, b) => ['steps', 'tokens'].includes(sort) ? a[sort] - b[sort] : b[sort] - a[sort]);
  $('leaderboard').replaceChildren(...ordered.map((model, i) => {
    const row = element('tr');
    row.append(element('td', String(i + 1).padStart(2, '0')));
    const cell = element('td'), wrap = element('div', null, 'model-cell');
    const mark = element('span', model.name.slice(0, 1), 'model-mark');
    mark.dataset.tone = state.catalog.models.indexOf(model) % 3;
    const name = element('a', model.name, 'model-name');
    name.href = `#model=${encodeURIComponent(model.id)}`;
    wrap.append(mark, name); cell.append(wrap); row.append(cell);
    const partial = element('td', null, 'score-cell'), barRow = element('div');
    const track = element('span', null, 'mini-track'), fill = element('i');
    fill.style.width = `${model.score}%`; track.append(fill);
    barRow.append(element('span', model.score.toFixed(1), 'score-value'), track); partial.append(barRow); row.append(partial);
    row.append(element('td', `${model.success.toFixed(1)}%`), element('td', model.steps.toFixed(1)), element('td', `${model.tokens.toFixed(1)}K`));
    const more = element('td'), link = element('a', t('Explore →', '查看轨迹 →'), 'explore-link');
    link.href = name.href; more.append(link); row.append(more);
    return row;
  }));
}
function mediaState(message, error = false, retry = false) {
  $('frame-state').hidden = false;
  $('frame-state').dataset.error = error;
  $('frame-state').querySelector('p').textContent = message;
  $('retry').hidden = !retry;
  $('frame').hidden = true;
  $('zoom').hidden = true;
  $('screen').setAttribute('aria-busy', !error);
}
function writeRoute(push = false) {
  const hash = new URLSearchParams({model: state.model.id, task: state.task.id, run: state.run.key, step: String(state.step + 1)});
  history[push ? 'pushState' : 'replaceState'](null, '', `#${hash}`);
}
function renderTasks() {
  const tasks = state.tasks.filter(task => matchesTask(task, $('search').value, $('filter').value));
  $('task-count').textContent = `${tasks.length} / ${state.tasks.length}`;
  $('task-list').replaceChildren(...tasks.map(task => {
    const run = defaultRun(task), button = element('button', null, 'task-option');
    button.setAttribute('aria-current', task.id === state.task?.id);
    const head = element('div');
    head.append(element('span', `Task ${task.id}`), element('span', score(run?.score), 'task-percent'));
    button.append(head, element('p', task.instruction || t('No instruction recorded.', '无任务指令记录。')),
      element('small', `${run.steps} ${t('steps', '步')} · ${task.runs.length} ${t(task.runs.length === 1 ? 'attempt' : 'attempts', '次运行')}`));
    button.onclick = () => selectTask(task, undefined, 0, true);
    return button;
  }));
  if (!tasks.length) $('task-list').append(element('p', t('No matching tasks.', '没有匹配的任务。'), 'empty-list'));
}
function renderAttempt() {
  if (!state.run) return;
  const run = state.run;
  $('attempt').disabled = false;
  $('attempt').replaceChildren(...state.task.runs.map((r, i) => {
    const option = element('option', `${i + 1} · ${r.steps} ${t('steps', '步')} · ${outcome(r.score)}`);
    option.value = r.key;
    return option;
  }));
  $('attempt').value = run.key;
  $('task-score').textContent = score(run.score);
  $('task-outcome').textContent = outcome(run.score);
  $('score-fill').style.width = `${Math.max(0, Math.min(1, run.score ?? 0)) * 100}%`;
  $('run-steps').textContent = run.steps;
  $('run-time').textContent = duration(run.seconds);
  $('run-tokens').textContent = tokens(run.output_tokens);
  $('task-id').textContent = `TASK ${state.task.id}`;
  $('instruction').textContent = state.task.instruction || t('No instruction recorded.', '无任务指令记录。');
  $('screen-label').textContent = `Task ${state.task.id}`;
}
function stopPlaying() {
  state.playing = false;
  clearTimeout(state.timer);
  $('play').textContent = '▶';
  $('play').setAttribute('aria-label', t('Play trajectory', '播放轨迹'));
}
function selectTask(task, requestedRun, requestedStep = 0, push = false) {
  stopPlaying();
  state.frameRequest++;
  state.player?.cancelPending();
  state.task = task;
  state.run = task.runs.find(run => run.key === requestedRun) || defaultRun(task);
  state.record = null;
  state.step = Math.max(0, Math.min(state.run.steps - 1, requestedStep));
  renderTasks(); renderAttempt(); writeRoute(push);
  $('scrubber').max = Math.max(0, state.run.steps - 1);
  $('scrubber').value = state.step;
  $('step-label').textContent = state.run.steps ? `${t('Step', '步骤')} ${state.step + 1} / ${state.run.steps}` : t('Empty attempt', '空运行记录');
  $('trace-content').textContent = '';
  $('frame-status').textContent = '';
  ['previous', 'next', 'play', 'scrubber'].forEach(id => { $(id).disabled = true; });
  const id = ++state.request;
  if (!state.run.steps) {
    mediaState(t('This attempt has no recorded steps.', '本次运行没有记录步骤。'), true);
    return;
  }
  mediaState(t('Loading model traces…', '正在加载模型轨迹…'));
  worker.postMessage({id, url: new URL(state.model.zip, base).href, key: state.run.key, sha256: state.model.zip_sha256});
}
worker.onmessage = async ({data}) => {
  if (data.id !== state.request) return;
  if (data.error) {
    mediaState(t('Trace files are not available yet. Retry when the archive has been published.', '轨迹资源暂未上线，请在资源发布后重试。'), true, true);
    return;
  }
  state.record = data.record;
  if (!state.player) state.player = new FramePlayer(data.meta, new URL(`${state.model.id}/`, base));
  $('download-video').href = new URL(data.meta.video_file || data.meta.video_parts[0].file, new URL(`${state.model.id}/`, base)).href;
  await renderStep();
};
worker.onerror = () => mediaState(t('Unable to read this archive. Please retry in a current browser.', '无法读取轨迹包，请使用新版浏览器重试。'), true, true);
async function renderStep(loadFrame = true) {
  if (!state.record) return false;
  const steps = state.record.traj || [], step = steps[state.step];
  $('step-label').textContent = `${t('Step', '步骤')} ${state.step + 1} / ${steps.length}`;
  $('scrubber').value = state.step;
  $('previous').disabled = state.step <= 0;
  $('next').disabled = state.step >= steps.length - 1;
  $('play').disabled = steps.length === 0;
  $('scrubber').disabled = steps.length === 0;
  $('trace-content').textContent = stepDetails(step, state.tab) || t('No content recorded for this field.', '此字段没有记录内容。');
  $('trace-content').setAttribute('aria-labelledby', `tab-${state.tab}`);
  if (!loadFrame) return true;
  const request = ++state.frameRequest;
  writeRoute();
  if (!Number.isInteger(step?.frame_index)) {
    mediaState(t('No observation recorded for this step.', '此步骤没有观察画面。'), true);
    return true;
  }
  mediaState(t('Loading observation…', '正在加载观察画面…'));
  $('frame-status').textContent = t('Loading frame…', '正在加载画面…');
  try {
    const painted = await state.player.draw($('frame'), step.frame_index);
    if (request !== state.frameRequest || !painted) return false;
    // Paint each selected frame as soon as its media range has decoded.
    $('frame').hidden = false;
    $('frame-state').hidden = true;
    $('zoom').hidden = false;
    $('screen').setAttribute('aria-busy', 'false');
    $('frame-status').textContent = `1280 × 720 · ${t('Frame', '帧')} ${step.frame_index}`;
    return true;
  } catch (error) {
    if (request !== state.frameRequest || error.name === 'AbortError') return false;
    stopPlaying();
    $('frame-status').textContent = t('Observation unavailable', '画面暂不可用');
    mediaState(error.message, true, true);
    return false;
  }
}
async function moveStep(delta) {
  if (!state.record) return false;
  state.step = Math.max(0, Math.min(state.record.traj.length - 1, state.step + delta));
  return renderStep();
}
async function playNext() {
  if (!state.playing) return;
  if (state.step >= state.record.traj.length - 1) { stopPlaying(); return; }
  const ready = await moveStep(1);
  if (ready && state.playing) state.timer = setTimeout(playNext, 1000 / Number($('speed').value));
}
async function route() {
  const version = ++state.route;
  const params = new URLSearchParams(location.hash.slice(1));
  const model = state.catalog.models.find(m => m.id === params.get('model'));
  if (!model) {
    state.request++; state.frameRequest++; stopPlaying();
    state.player?.dispose(); state.player = null;
    $('leaderboard-view').hidden = false; $('explorer-view').hidden = true;
    return;
  }
  const enteringExplorer = $('explorer-view').hidden;
  $('page-error').hidden = true;
  $('leaderboard-view').hidden = true; $('explorer-view').hidden = false;
  if (enteringExplorer) window.scrollTo({top: 0, behavior: 'instant'});
  if (state.model?.id !== model.id || !state.tasks.length) {
    state.request++; state.frameRequest++; stopPlaying();
    state.player?.dispose(); state.player = null; state.record = null;
    state.model = model; state.task = null; state.run = null;
    state.tasks = [];
    $('attempt').replaceChildren(); $('attempt').disabled = true;
    ['task-score', 'task-outcome', 'run-steps', 'run-time', 'run-tokens'].forEach(id => { $(id).textContent = '—'; });
    $('instruction').textContent = ''; $('step-label').textContent = ''; $('frame-status').textContent = '';
    ['previous', 'next', 'play', 'scrubber'].forEach(id => { $(id).disabled = true; });
    $('task-list').replaceChildren(element('p', t('Loading tasks…', '正在加载任务…'), 'empty-list'));
    $('trace-content').textContent = '';
    mediaState(t('Loading tasks…', '正在加载任务…'));
    $('model-title').textContent = model.name;
    $('model-mark').textContent = model.name[0];
    $('model-select').value = model.id;
    $('download-json').href = new URL(model.zip, base).href;
    $('download-video').href = new URL(`${model.id}/traj.mp4`, base).href;
    $('search').value = ''; $('filter').value = 'all';
    try {
      if (!indexCache.has(model.id)) {
        const response = await fetch(model.index);
        if (!response.ok) throw new Error('Task index unavailable');
        indexCache.set(model.id, await response.json());
      }
      if (version !== state.route) return;
      state.tasks = indexCache.get(model.id).tasks;
    } catch {
      if (version !== state.route) return;
      mediaState(t('The task list could not be loaded. Please reload this page.', '任务列表加载失败，请刷新页面。'), true);
      return;
    }
  }
  if (version !== state.route) return;
  const task = state.tasks.find(t => t.id === params.get('task')) || state.tasks[0];
  const step = Number(params.get('step') || 1);
  selectTask(task, params.get('run'), Number.isFinite(step) ? Math.max(0, Math.floor(step) - 1) : 0);
}
$('en').onclick = () => setLanguage('en'); $('zh').onclick = () => setLanguage('zh');
$('sort').onchange = renderLeaderboard;
$('search').oninput = renderTasks; $('filter').onchange = renderTasks;
$('back').onclick = () => { location.hash = ''; };
$('model-select').onchange = () => { location.hash = new URLSearchParams({model: $('model-select').value}); };
$('attempt').onchange = () => selectTask(state.task, $('attempt').value, 0, true);
$('previous').onclick = () => { stopPlaying(); moveStep(-1); };
$('next').onclick = () => { stopPlaying(); moveStep(1); };
$('scrubber').oninput = () => { stopPlaying(); state.step = Number($('scrubber').value); renderStep(); };
$('play').onclick = () => {
  if (state.playing) { stopPlaying(); return; }
  state.playing = true;
  $('play').textContent = 'Ⅱ'; $('play').setAttribute('aria-label', t('Pause trajectory', '暂停轨迹'));
  if (state.step === state.record.traj.length - 1) { state.step = -1; }
  playNext();
};
$('retry').onclick = () => {
  if (state.record) { state.player?.dispose(); state.player = null; selectTask(state.task, state.run.key, state.step); }
  else { selectTask(state.task, state.run.key, state.step); }
};
$('share').onclick = async () => {
  try { await navigator.clipboard.writeText(location.href); $('share').textContent = t('Copied ✓', '已复制 ✓'); }
  catch { $('share').textContent = t('Copy the address bar URL', '请复制地址栏链接'); }
  setTimeout(() => { $('share').textContent = t('Copy link', '复制链接'); }, 2000);
};
document.querySelectorAll('[data-tab]').forEach(button => {
  button.onclick = () => {
    state.tab = button.dataset.tab;
    document.querySelectorAll('[data-tab]').forEach(b => b.setAttribute('aria-selected', b === button));
    renderStep(false);
  };
  button.onkeydown = event => {
    if (!['ArrowLeft', 'ArrowRight'].includes(event.key)) return;
    event.preventDefault();
    const tabs = [...document.querySelectorAll('[data-tab]')], i = tabs.indexOf(button);
    const next = tabs[(i + (event.key === 'ArrowRight' ? 1 : tabs.length - 1)) % tabs.length];
    next.click(); next.focus();
  };
});
$('zoom').onclick = () => {
  $('zoom-frame').getContext('2d').drawImage($('frame'), 0, 0);
  $('zoom-label').textContent = `${state.model.name} · Task ${state.task.id} · ${t('Step', '步骤')} ${state.step + 1}`;
  $('image-dialog').showModal();
};
$('close-zoom').onclick = () => $('image-dialog').close();
document.addEventListener('keydown', event => {
  if (event.target.closest('input,select,textarea,button,[role=tab],a') || event.altKey || event.ctrlKey || event.metaKey || $('image-dialog').open || $('explorer-view').hidden || !state.record) return;
  if (event.key === 'ArrowLeft' || event.key === 'ArrowRight') { event.preventDefault(); stopPlaying(); moveStep(event.key === 'ArrowRight' ? 1 : -1); }
  if (event.code === 'Space') { event.preventDefault(); $('play').click(); }
});
document.addEventListener('visibilitychange', () => { if (document.hidden) stopPlaying(); });
window.addEventListener('hashchange', route);
document.querySelector('.skip').onclick = event => {
  event.preventDefault();
  $('main').setAttribute('tabindex', '-1');
  $('main').focus();
};
setLanguage(language);
try {
  const response = await fetch('trajectory-data/catalog.json');
  if (!response.ok) throw new Error('Catalog unavailable');
  state.catalog = await response.json();
  base = state.catalog.asset_base;
  // Local preview can serve the separate media repository without changing publication URLs.
  const override = new URLSearchParams(location.search).get('assets');
  if (['localhost', '127.0.0.1', '[::1]'].includes(location.hostname) && override) {
    const candidate = new URL(override, location.href);
    if (['localhost', '127.0.0.1', '[::1]'].includes(candidate.hostname)) base = candidate.href;
  }
  $('model-select').replaceChildren(...state.catalog.models.map(model => {
    const option = element('option', model.name); option.value = model.id; return option;
  }));
  renderLeaderboard(); await route();
} catch {
  $('page-error').textContent = t('Evaluation data could not be loaded. Please reload this page.', '评测数据加载失败，请刷新页面。');
  $('page-error').hidden = false;
}
