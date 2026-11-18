// Renderer entry: top bar, run lifecycle and backend event dispatch.

import { h, clear, call, fmtInt, fmtElapsed, loadLocal, saveLocal } from './dom.js';
import { ResultsView } from './results.js';
import { ReaderView } from './reader-view.js';
import { SettingsDrawer } from './settings.js';
import { GroupsView } from './groups.js';
import { DocumentsPanel } from './files.js';
import { ChatPanel } from './chat.js';

const TASK_NAMES = ['catalog_summarize', 'query_route', 'extract', 'synthesize'];
const { OTHER, modelOptions } = window.CorpusModelOptions;
const { EtaEstimator, fmtRemaining } = window.CorpusEta;
const $ = (id) => document.getElementById(id);

const els = {
  folder: $('folder'), browse: $('browse'), openResults: $('open-results'),
  resultsTools: $('results-tools'), tabHits: $('tab-hits'), tabGroups: $('tab-groups'),
  groupAnswers: $('group-answers'), groups: $('groups'), resultsList: $('results'),
  resultsSummary: $('results-summary'), question: $('question'), definition: $('definition'),
  provider: $('provider'), model: $('model'), modelCustom: $('model-custom'), modelRefresh: $('model-refresh'), mixed: $('mixed'),
  modelError: $('model-error'), run: $('run'), cancel: $('cancel'),
  statusText: $('status-text'), barFill: $('bar-fill'),
  errorPanel: $('error-panel'), errorMessage: $('error-message'), errorTail: $('error-tail'),
  restart: $('restart'), errorDismiss: $('error-dismiss'), notices: $('notices'),
  costDialog: $('cost-dialog'), costTotal: $('cost-total'), costLines: $('cost-lines'),
  costProceed: $('cost-proceed'), costCancel: $('cost-cancel'),
  docsToggle: $('docs-toggle'), chatToggle: $('chat-toggle'),
};

const state = {
  config: null,
  running: false,
  runFolder: '',
  startedAt: 0,
  eta: new EtaEstimator(50), // time left, averaged over the last 50 responses
  timer: null,
  phase: '',
  models: new Map(), // provider -> Promise<{models, error}>
  pendingCost: null,
  selectionDirty: false, // top-bar provider/model edited but not yet saved
  runError: false,
  job: 'run', // what the running backend task is: 'run' or 'group'
  resultsPath: null, // results file of the run on screen, once saved
};

const results = new ResultsView({
  list: $('results'), summary: $('results-summary'), empty: $('results-empty'),
  problems: $('problems'), problemList: $('problem-list'), problemCount: $('problem-count'),
  answer: $('answer'),
}, (row) => reader.open(row));

const reader = new ReaderView({
  head: $('reader-head'), file: $('reader-file'), page: $('reader-page'),
  notice: $('reader-notice'), scroll: $('reader-scroll'), doc: $('reader-doc'),
}, () => state.runFolder || els.folder.value.trim());

const groupsView = new GroupsView(els.groups, (row) => reader.open(row));

const docs = new DocumentsPanel({ toggle: els.docsToggle, panel: $('docs-panel') }, () => {
  if (!state.running && docs.noneChosen) setStatus('No documents selected');
});

/** " · 12 of 60 documents" when the run reads a subset, else ''. */
function subsetNote(selection, total) {
  if (!selection) return '';
  return total ? ` · ${fmtInt(selection.length)} of ${fmtInt(total)} documents` : ` · ${fmtInt(selection.length)} documents`;
}

function useSuggestion(kind, text) {
  const field = kind === 'question' ? els.question : els.definition;
  field.value = text;
  field.dispatchEvent(new Event('change', { bubbles: true }));
  field.classList.remove('field-flash');
  void field.offsetWidth; // restart the animation
  field.classList.add('field-flash');
}

const drawer = new SettingsDrawer($('drawer'), $('drawer-toggle'), {
  getModels,
  onConfig: (config) => applyConfig(config),
});

const chat = new ChatPanel($('chat'), els.chatToggle, {
  getModels,
  getConfig: () => state.config,
  getContext: () => {
    const { relevance, mode, synthesize } = drawer.queryOptions();
    const { chosen, total } = docs.counts();
    return {
      question: els.question.value,
      definition: els.definition.value,
      relevance,
      mode,
      synthesize,
      documents_chosen: chosen,
      documents_total: total,
      model: selectedModel(),
    };
  },
  onUse: useSuggestion,
});

// ---- config, provider and model ------------------------------------------

function getModels(provider, { refresh = false } = {}) {
  if (refresh || !state.models.has(provider)) {
    state.models.set(provider, call('models.list', { provider }).catch((e) => ({ models: [], error: e.message })));
  }
  return state.models.get(provider);
}

function applyConfig(config) {
  state.config = config;
  const tasks = TASK_NAMES.map((t) => config.tasks[t]);
  const extract = config.tasks.extract;
  const mixed = tasks.some((t) => t.model !== extract.model || t.provider !== extract.provider);
  clear(els.provider);
  for (const name of Object.keys(config.providers)) {
    els.provider.append(h('option', { value: name, text: name, selected: name === extract.provider }));
  }
  els.mixed.hidden = !mixed;
  showCustom(false);
  fillModelList(extract.provider, extract.model);
}

function setModelOptions(options) {
  clear(els.model);
  for (const o of options) {
    els.model.append(h('option', { value: o.value, text: o.text, selected: o.selected, disabled: o.disabled }));
  }
}

async function fillModelList(provider, wanted, refresh = false) {
  // Show the configured model at once; the provider's list replaces it.
  setModelOptions([{ value: wanted, text: wanted ? `${wanted} — loading list…` : 'Loading models…',
    selected: true, disabled: !wanted }]);
  const listing = await getModels(provider, { refresh });
  if (els.provider.value !== provider) return;
  const { options, listed } = modelOptions(listing, wanted);
  setModelOptions(options);
  if (listing.error) {
    showModelError(`Model list unavailable: ${listing.error}. Choose "Other" to type a model name.`);
  } else if (!listed) {
    showModelError(`${wanted} is not in ${provider}'s model list. Choose a model from the list.`);
  } else {
    showModelError(null);
  }
}

/** Re-reads the provider's model list, keeping the current choice. */
function refreshModels() {
  if (state.running || !state.config || els.model.value === OTHER) return;
  fillModelList(els.provider.value, els.model.value, true);
}

function showCustom(show) {
  els.modelCustom.hidden = !show;
  if (!show) els.modelCustom.value = '';
}

/** The model the top bar currently names ('' if none). */
function selectedModel() {
  return els.model.value === OTHER ? els.modelCustom.value.trim() : els.model.value;
}

function showModelError(message) {
  els.modelError.textContent = message || '';
  els.modelError.hidden = !message;
}

/**
 * Saves the top-bar provider/model to all four tasks. Only a choice the user
 * made in the top bar is saved, so a mixed per-task setup is left alone.
 */
async function saveSelection() {
  if (!state.config) return false;
  if (!state.selectionDirty) return true;
  const provider = els.provider.value;
  const model = selectedModel();
  if (!model) {
    showModelError('Choose or type a model.');
    return false;
  }
  const changes = [];
  for (const t of TASK_NAMES) {
    const cfg = state.config.tasks[t];
    if (cfg.model !== model) changes.push([['tasks', t, 'model'], model]);
    if (cfg.provider !== provider) changes.push([['tasks', t, 'provider'], provider]);
  }
  if (!changes.length) {
    state.selectionDirty = false;
    return true;
  }
  try {
    const res = await call('config.save', { changes });
    state.selectionDirty = false;
    applyConfig(res.config);
    drawer.setConfig(res.config);
    showModelError(null);
    return true;
  } catch (e) {
    const detail = Object.values(e.fields || {}).join('; ');
    showModelError(`Could not save the model choice: ${detail || e.message}`);
    return false;
  }
}

els.provider.addEventListener('change', () => {
  state.selectionDirty = true;
  showCustom(false);
  fillModelList(els.provider.value, '', true);
  els.model.focus();
});
els.model.addEventListener('change', () => {
  state.selectionDirty = true;
  if (els.model.value === OTHER) {
    showCustom(true);
    els.modelCustom.focus();
    return;
  }
  showCustom(false);
  if (els.model.value) saveSelection();
});
els.modelRefresh.addEventListener('click', refreshModels);
// Loading a model in LM Studio happens in another window; pick it up on return.
window.addEventListener('focus', refreshModels);
els.modelCustom.addEventListener('input', () => {
  state.selectionDirty = true;
});
els.modelCustom.addEventListener('change', () => {
  state.selectionDirty = true;
  if (els.modelCustom.value.trim()) saveSelection();
});

async function loadConfig() {
  try {
    const { config } = await call('config.get');
    applyConfig(config);
    drawer.setConfig(config);
    return true;
  } catch (e) {
    const status = await window.corpusqa.backendStatus();
    if (!status.running) {
      // It may have exited before this window subscribed to its events.
      const exit = status.lastExit;
      showError(`The backend is not running (exit code ${exit?.code ?? 'none'}).`,
        exit ? exit.stderrTail : null, true);
    } else {
      showError(`The configuration could not be loaded: ${e.message}`);
    }
    return false;
  }
}

// ---- folder and query options --------------------------------------------

els.folder.value = loadLocal('corpusqa.folder', '');
els.definition.value = loadLocal('corpusqa.definition', '');
els.folder.addEventListener('change', () => {
  saveLocal('corpusqa.folder', els.folder.value.trim());
  docs.load(els.folder.value.trim());
});
els.definition.addEventListener('change', () => saveLocal('corpusqa.definition', els.definition.value));
els.browse.addEventListener('click', async () => {
  const picked = await window.corpusqa.pickFolder(els.folder.value.trim());
  if (picked) {
    els.folder.value = picked;
    saveLocal('corpusqa.folder', picked);
    docs.load(picked);
  }
});
els.question.addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !state.running) startRun();
});

// ---- saved results ----------------------------------------------------------

async function openResults() {
  if (state.running) return;
  const folder = els.folder.value.trim();
  const picked = await window.corpusqa.pickResultsFile(folder ? `${folder}/.corpusqa/results` : '');
  if (!picked) return;
  let loaded;
  try {
    loaded = await call('results.load', { path: picked });
  } catch (e) {
    showError(`Could not open ${picked}: ${e.message}`);
    return;
  }
  showSavedRun(loaded.run, loaded.sources || {}, picked);
}

/** Shows a saved run as if it had just finished; nothing is re-read. */
function showSavedRun(run, sources, path) {
  hideError();
  clear(els.notices);
  results.reset();
  reader.clearCache();
  state.runFolder = run.corpus;
  els.folder.value = run.corpus;
  els.question.value = run.question;
  els.definition.value = run.definition || '';
  const failures = [];
  for (const f of run.files) {
    if (f.status === 'failed' || f.status === 'partial') failures.push([f.rel_path, f.error || f.status]);
    if (f.status === 'failed') continue;
    const source = sources[f.rel_path] || 'same';
    results.addFileResult({
      rel_path: f.rel_path,
      relevant: f.status === 'relevant' || (f.status === 'partial' && f.findings.length > 0),
      findings: f.findings.map((x) => ({ ...x, source })),
    });
  }
  results.setFailures(failures);
  if (run.answer) results.setAnswer(run.answer.text);
  state.resultsPath = path;
  docs.load(run.corpus, run.selection ?? null).then((missing) => {
    if (missing.length) {
      addNotice(`${fmtInt(missing.length)} of the documents this run read ${missing.length === 1 ? 'is' : 'are'} no longer in the folder: ${missing.slice(0, 3).join(', ')}${missing.length > 3 ? '…' : ''}`);
    }
  });
  groupsView.render(run, sources);
  updateTools();
  showTab(run.grouping ? 'groups' : 'hits');
  for (const problem of run.grouping?.problems || []) addNotice(problem);
  const when = new Date(run.started_at).toLocaleString();
  const outcome = { finished: '', cancelled: ' (cancelled)', failed: ' (stopped by an error)', running: ' (did not finish)' }[run.outcome] ?? '';
  const n = results.rows.length;
  setStatus(`Saved results from ${when}${outcome} · ${fmtInt(n)} finding${n === 1 ? '' : 's'}${subsetNote(run.selection, 0)}`, run.outcome === 'finished' ? 1 : null);
  const stale = Object.values(sources).filter((v) => v !== 'same').length;
  if (stale) {
    addNotice(`${fmtInt(stale)} document${stale === 1 ? ' has' : 's have'} changed or gone since these results were saved; their quotes cannot be shown in context.`);
  }
}

els.openResults.addEventListener('click', openResults);

// ---- grouped answers ---------------------------------------------------------

function showTab(name) {
  const groups = name === 'groups' && !els.tabGroups.hidden;
  els.tabHits.setAttribute('aria-selected', String(!groups));
  els.tabGroups.setAttribute('aria-selected', String(groups));
  els.resultsList.hidden = groups;
  els.resultsSummary.hidden = groups;
  els.groups.hidden = !groups;
}

function updateTools() {
  els.tabGroups.hidden = !els.groups.childElementCount;
  els.resultsTools.hidden = !(state.resultsPath && results.rows.length);
  els.groupAnswers.disabled = state.running;
  els.groupAnswers.textContent = els.tabGroups.hidden ? 'Group answers' : 'Group again';
}

async function startGroup() {
  if (state.running || !state.resultsPath) return;
  hideError();
  state.job = 'group';
  state.startedAt = Date.now();
  state.eta.reset(null);
  state.phase = 'Grouping answers';
  state.fraction = null;
  state.runError = false;
  setRunning(true);
  setStatus('Grouping answers');
  try {
    await call('group.start', { path: state.resultsPath });
  } catch (e) {
    setRunning(false);
    state.startedAt = 0;
    setStatus('Not started');
    showError(e.message);
  }
}

async function onGroupDone(ev, elapsed) {
  state.job = 'run';
  if (ev.cancelled) {
    setStatus(`Grouping cancelled after ${elapsed}`);
  } else if (state.runError) {
    setStatus(`Grouping stopped with an error after ${elapsed}`);
  } else {
    try {
      const loaded = await call('results.load', { path: ev.results_path });
      showSavedRun(loaded.run, loaded.sources || {}, ev.results_path);
      const n = loaded.run.grouping ? loaded.run.grouping.groups.length : 0;
      setStatus(`Grouped into ${fmtInt(n)} distinct answer${n === 1 ? '' : 's'} in ${elapsed}`, 1);
    } catch (e) {
      showError(`Grouping finished but the results could not be reloaded: ${e.message}`);
    }
  }
  updateTools();
}

els.groupAnswers.addEventListener('click', startGroup);
els.tabHits.addEventListener('click', () => showTab('hits'));
els.tabGroups.addEventListener('click', () => showTab('groups'));

// ---- status, errors and notices -------------------------------------------

function setStatus(text, fraction = null) {
  let timing = '';
  if (state.running && state.startedAt) {
    const now = Date.now();
    timing = ` · ${fmtElapsed(now - state.startedAt)} elapsed`;
    const left = state.phase === 'Cancelling' ? null : state.eta.remainingMs(now);
    if (left !== null) timing += ` · about ${fmtRemaining(left)} left`;
  }
  els.statusText.textContent = text + timing;
  els.barFill.style.width = fraction === null ? '0%' : `${Math.round(fraction * 1000) / 10}%`;
  els.barFill.parentElement.classList.toggle('busy', state.running && fraction === null);
}

function showError(message, stderrTail = null, canRestart = false) {
  els.errorMessage.textContent = message;
  els.errorTail.textContent = stderrTail && stderrTail.length ? stderrTail.join('\n') : '';
  els.errorTail.hidden = !els.errorTail.textContent;
  els.restart.hidden = !canRestart;
  els.errorPanel.hidden = false;
  els.errorTail.scrollTop = els.errorTail.scrollHeight; // the last lines name the error
}

function hideError() {
  els.errorPanel.hidden = true;
}

els.errorDismiss.addEventListener('click', hideError);
els.restart.addEventListener('click', async () => {
  els.restart.disabled = true;
  try {
    await window.corpusqa.restartBackend();
    state.models.clear();
    hideError();
    await loadConfig();
    setStatus('Backend restarted');
  } finally {
    els.restart.disabled = false;
  }
});

function addNotice(message) {
  for (const n of els.notices.children) if (n.dataset.message === message) return;
  const note = h('div', { class: 'notice', role: 'status', dataset: { message } },
    h('p', { text: message }),
    h('button', { type: 'button', class: 'icon-btn', 'aria-label': 'Dismiss', text: '×', onclick: () => note.remove() }));
  els.notices.append(note);
}

// ---- run lifecycle ---------------------------------------------------------

function setRunning(running) {
  state.running = running;
  document.body.dataset.runState = running ? 'running' : 'done';
  els.run.hidden = running;
  els.cancel.hidden = !running;
  els.cancel.disabled = false;
  els.folder.disabled = running;
  els.browse.disabled = running;
  els.docsToggle.disabled = running || !docs.folder;
  if (running) docs.close();
  els.openResults.disabled = running;
  els.groupAnswers.disabled = running;
  els.provider.disabled = running;
  els.model.disabled = running;
  els.modelCustom.disabled = running;
  els.modelRefresh.disabled = running;
  clearInterval(state.timer);
  state.timer = running ? setInterval(() => setStatus(state.phase, state.fraction), 1000) : null;
}

async function startRun() {
  // Enter can arrive while a start is still saving the model selection;
  // a second start would be refused by the backend and must not run.
  if (state.running || els.run.disabled) return;
  const folder = els.folder.value.trim();
  const question = els.question.value.trim();
  if (!folder) {
    els.folder.focus();
    setStatus('Choose a folder first');
    return;
  }
  if (!question) {
    els.question.focus();
    setStatus('Type a question first');
    return;
  }
  if (docs.folder !== folder) await docs.load(folder);
  if (docs.noneChosen) {
    setStatus('No documents selected');
    docs.open();
    return;
  }
  els.run.disabled = true;
  try {
    if (!(await saveSelection())) return;
    saveLocal('corpusqa.folder', folder);
    saveLocal('corpusqa.definition', els.definition.value);
    hideError();
    clear(els.notices);
    results.reset();
    state.job = 'run';
    state.resultsPath = null;
    groupsView.clear();
    showTab('hits');
    updateTools();
    // Files may have changed since the last run (it reconverts them), so
    // text cached by the reader is dropped at every start.
    reader.clearCache();
    state.runFolder = folder;
    const definition = els.definition.value.trim();
    state.startedAt = Date.now();
    state.eta.reset(null);
    state.phase = 'Starting';
    state.fraction = null;
    state.runError = false;
    setRunning(true);
    setStatus('Starting');
    try {
      const files = docs.chosenList();
      state.runSubset = files ? subsetNote(files, docs.counts().total) : '';
      await call('run.start', { folder, question, definition: definition || null, ...drawer.queryOptions(), files });
    } catch (e) {
      // "already in progress": the backend is running an earlier start, so
      // the running state (and its Cancel button) stays.
      if (!/already in progress/.test(e.message)) {
        setRunning(false);
        state.startedAt = 0;
        setStatus('Not started');
      }
      showError(e.message);
    }
  } finally {
    els.run.disabled = false;
  }
}

els.run.addEventListener('click', startRun);
els.cancel.addEventListener('click', async () => {
  els.cancel.disabled = true;
  state.phase = 'Cancelling';
  setStatus('Cancelling', state.fraction);
  try {
    await call('run.cancel');
  } catch (e) {
    showError(e.message);
  }
});

function onProgress(ev) {
  const total = ev.total || 0;
  state.eta.record(ev.phase, ev.done, total, Date.now());
  if (ev.phase === 'converting') {
    state.phase = `Converting documents ${fmtInt(ev.done)}/${fmtInt(total)}`;
  } else if (ev.phase === 'grouping') {
    state.phase = `Grouping answers ${fmtInt(ev.done)}/${fmtInt(total)} calls`;
  } else {
    state.phase = `Reading ${fmtInt(ev.done)}/${fmtInt(total)} sections`;
  }
  state.fraction = total ? ev.done / total : null;
  if (!els.cancel.disabled) setStatus(state.phase, state.fraction);
}

function onDone(ev) {
  const elapsed = fmtElapsed(Date.now() - state.startedAt);
  state.startedAt = 0;
  setRunning(false);
  if (state.job === 'group') {
    onGroupDone(ev, elapsed);
    return;
  }
  if (ev.results_path) state.resultsPath = ev.results_path;
  updateTools();
  results.setFailures(ev.failures);
  if (ev.answer) results.setAnswer(ev.answer);
  const n = results.rows.length;
  if (ev.cancelled) setStatus(`Cancelled after ${elapsed}`);
  else if (state.runError) setStatus(`Stopped with an error after ${elapsed}`);
  else setStatus(`Finished in ${elapsed} · ${fmtInt(n)} finding${n === 1 ? '' : 's'}${state.runSubset || ''}`, 1);
}

function onCostConfirm(ev) {
  state.pendingCost = ev.run_id;
  els.costTotal.textContent = `Estimated total: $${ev.estimate.total_usd.toFixed(2)}`;
  clear(els.costLines);
  for (const line of ev.estimate.lines) els.costLines.append(h('li', { text: line }));
  els.costDialog.showModal();
}

async function answerCost(proceed) {
  const runId = state.pendingCost;
  state.pendingCost = null;
  if (els.costDialog.open) els.costDialog.close();
  if (runId === null) return;
  try {
    await call('cost.reply', { run_id: runId, proceed });
  } catch (e) {
    showError(e.message);
  }
}

els.costProceed.addEventListener('click', () => answerCost(true));
els.costCancel.addEventListener('click', () => answerCost(false));
els.costDialog.addEventListener('cancel', (e) => {
  e.preventDefault();
  answerCost(false);
});

window.corpusqa.onEvent((ev) => {
  switch (ev.event) {
    case 'progress': onProgress(ev); break;
    case 'flags': results.setFlags(ev.items); break;
    case 'file_result': results.addFileResult(ev); break;
    case 'notice': addNotice(ev.message); break;
    case 'cost.confirm': onCostConfirm(ev); break;
    case 'error':
      if (state.running) state.runError = true;
      showError(ev.message);
      break;
    case 'done': onDone(ev); break;
    case 'backend.exit':
      if (els.costDialog.open) els.costDialog.close();
      state.pendingCost = null;
      if (state.running) {
        state.startedAt = 0;
        setRunning(false);
      }
      setStatus('Backend stopped');
      showError(`The backend stopped unexpectedly (exit code ${ev.code ?? 'none'}).`, ev.stderrTail, true);
      break;
    default: break;
  }
});

// ---- start -------------------------------------------------------------------

document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && drawer.isOpen) drawer.close();
});

docs.load(els.folder.value.trim());

loadConfig().finally(() => {
  document.body.dataset.ready = '1';
  document.body.dataset.runState = 'idle';
});
