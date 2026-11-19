'use strict';

// Opt-in end-to-end smoke driver, loaded only when CORPUSQA_SMOKE=1.
// It drives the real renderer through the main workflow, writes a JSON
// summary to CORPUSQA_SMOKE_OUT and quits. Inputs: CORPUSQA_SMOKE_FOLDER,
// CORPUSQA_SMOKE_QUESTION, CORPUSQA_SMOKE_MODEL; optional CORPUSQA_SMOKE_SHOTS
// (a directory for screenshots) and CORPUSQA_SMOKE_MODEL_ONLY=1 (stop after
// choosing the model, before any run).

const fs = require('node:fs');
const path = require('node:path');

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function run(win, quit) {
  const env = process.env;
  const wc = win.webContents;
  const js = (code) => wc.executeJavaScript(code, true);
  const summary = { ok: false, steps: {} };
  const wait = async (expr, timeoutMs, label) => {
    const end = Date.now() + timeoutMs;
    while (Date.now() < end) {
      if (await js(expr)) return;
      await sleep(200);
    }
    throw new Error(`timed out waiting for ${label}`);
  };
  const shot = async (name) => {
    if (!env.CORPUSQA_SMOKE_SHOTS) return;
    const img = await wc.capturePage();
    fs.writeFileSync(path.join(env.CORPUSQA_SMOKE_SHOTS, `${name}.png`), img.toPNG());
  };
  const status = () => js(`document.getElementById('status-text').textContent`);
  const setInput = (sel, value) => js(`(() => { const el = document.querySelector(${JSON.stringify(sel)});
    el.value = ${JSON.stringify(value)}; el.dispatchEvent(new Event('input', {bubbles: true}));
    el.dispatchEvent(new Event('change', {bubbles: true})); return true; })()`);
  const click = (sel) => js(`(() => { const el = document.querySelector(${JSON.stringify(sel)});
    if (!el) return false; el.click(); return true; })()`);

  summary.console = [];
  wc.on('console-message', (...args) => {
    // Electron passes either (event) with fields or (event, level, message).
    const e = args[0];
    const level = typeof args[1] === 'number' ? args[1] : e.level;
    const message = typeof args[2] === 'string' ? args[2] : e.message;
    if (level === 2 || level === 3 || level === 'warning' || level === 'error') {
      summary.console.push(`${level}: ${message}`);
    }
  });
  // The smoke test runs as the real app and shares its remembered settings
  // (cache option, folder, document choices, chat model). Snapshot them and
  // put them back at the end, so a test never changes how the user's next
  // run behaves (it turns the cache off, for one).
  let storedSettings = null;
  try {
    await new Promise((r) => wc.once('did-finish-load', r));
    storedSettings = await js(`JSON.stringify(Object.entries(localStorage))`);
    await wait(`document.body.dataset.ready === '1'`, 30000, 'renderer ready');
    summary.steps.ready = await js(`({
      providers: [...document.querySelectorAll('#provider option')].map(o => o.value),
      provider: document.getElementById('provider').value,
      model: document.getElementById('model').value,
      mixed: !document.getElementById('mixed').hidden,
      errorPanel: document.getElementById('error-panel').hidden ? null : document.getElementById('error-message').textContent,
      errorTail: document.getElementById('error-tail').textContent,
      restartShown: !document.getElementById('restart').hidden,
    })`);
    await shot('0-ready');

    // Top bar: model applies to all four tasks. Pick it from the dropdown
    // when the provider lists it, else type it via "Other".
    await wait(`[...document.querySelectorAll('#model option')].some(o => o.value === '__other__')`, 15000, 'model list');
    summary.steps.modelList = await js(`({
      options: document.querySelectorAll('#model option').length,
      first: [...document.querySelectorAll('#model option')].slice(0, 4).map(o => o.textContent),
      selected: document.getElementById('model').selectedOptions[0]?.textContent ?? null,
      error: document.getElementById('model-error').hidden ? null : document.getElementById('model-error').textContent,
    })`);
    await shot('0b-model-list');
    const want = env.CORPUSQA_SMOKE_MODEL;
    const listed = await js(`[...document.querySelectorAll('#model option')].some(o => o.value === ${JSON.stringify(want)})`);
    if (listed) {
      await setInput('#model', want);
    } else {
      await setInput('#model', '__other__');
      await setInput('#model-custom', want);
    }
    await sleep(1500);
    summary.steps.model = await js(`({
      viaList: ${listed},
      value: document.getElementById('model').value,
      custom: document.getElementById('model-custom').value,
      mixed: !document.getElementById('mixed').hidden,
      error: document.getElementById('model-error').hidden ? null : document.getElementById('model-error').textContent,
    })`);
    if (env.CORPUSQA_SMOKE_MODEL_ONLY === '1') {
      summary.ok = true;
      return;
    }

    // Run options: synthesize on, cache off (so the run really reads).
    await js(`(() => { const boxes = document.querySelectorAll('#drawer input[type=checkbox]');
      boxes[0].checked = true; boxes[0].dispatchEvent(new Event('change'));
      boxes[1].checked = false; boxes[1].dispatchEvent(new Event('change')); return true; })()`);

    await setInput('#folder', env.CORPUSQA_SMOKE_FOLDER);
    await setInput('#question', env.CORPUSQA_SMOKE_QUESTION);
    await setInput('#definition', ''); // not a remembered one from real use

    // Documents: choose two of the folder's documents for this run, using
    // the filter, Select none / Select all on the shown rows, and one tick.
    const docText = () => js(`document.getElementById('docs-toggle').textContent`);
    await wait(`/documents/i.test(document.getElementById('docs-toggle').textContent)`, 10000, 'documents listed');
    const docsBefore = await docText();
    await click('#docs-toggle');
    await wait(`!document.getElementById('docs-panel').hidden && document.querySelectorAll('#docs-panel .doc').length > 0`, 10000, 'documents panel');
    const docButton = (label) => `[...document.querySelectorAll('#docs-panel .docs-actions button')].find(b => b.textContent === ${JSON.stringify(label)}).click()`;
    await js(docButton('Select none'));
    await setInput('#docs-panel .docs-filter', 'joy');
    const shownWhenFiltered = await js(`document.querySelectorAll('#docs-panel .doc').length`);
    await js(docButton('Select all'));
    await setInput('#docs-panel .docs-filter', '');
    await js(`[...document.querySelectorAll('#docs-panel .doc')].find(d => d.title === 'b_middle.md').querySelector('input').click()`);
    summary.steps.documents = {
      before: docsBefore,
      rows: await js(`[...document.querySelectorAll('#docs-panel .doc')].map(d => d.title + (d.querySelector('input').checked ? ' ✓' : ''))`),
      shownWhenFiltered,
      count: await js(`document.querySelector('#docs-panel .docs-count').textContent`),
    };
    await shot('0c-documents');
    await js(`[...document.querySelectorAll('#docs-panel .docs-foot button')].pop().click()`);
    summary.steps.documents.after = await docText();
    summary.steps.documents.closed = await js(`document.getElementById('docs-panel').hidden`);

    await click('#run');
    const statuses = [];
    const rowCounts = [];
    const etas = [];
    // Override with CORPUSQA_SMOKE_MAX_MIN; a timeout still records what was seen.
    const maxMs = Number(env.CORPUSQA_SMOKE_MAX_MIN || 20) * 60 * 1000;
    let timedOut = false;
    const t0 = Date.now();
    await wait(`document.body.dataset.runState === 'running'`, 10000, 'run start');
    while ((await js(`document.body.dataset.runState`)) === 'running') {
      const full = await status();
      const m = / · about (.+) left$/.exec(full);
      if (m && etas[etas.length - 1] !== m[1]) etas.push(m[1]);
      const s = full.replace(/ · [\d:]+ elapsed.*$/, '');
      if (statuses[statuses.length - 1] !== s) statuses.push(s);
      const n = await js(`document.querySelectorAll('#results li').length`);
      if (rowCounts[rowCounts.length - 1] !== n) rowCounts.push(n);
      if (Date.now() - t0 > maxMs) { timedOut = true; break; }
      await sleep(250);
    }
    summary.steps.run = {
      seconds: Math.round((Date.now() - t0) / 1000),
      statusSamples: statuses.length,
      etaSamples: etas.length,
      firstEtas: etas.slice(0, 5),
      lastEtas: etas.slice(-5),
      firstStatuses: statuses.slice(0, 6),
      lastStatuses: statuses.slice(-4),
      rowCountsDuringRun: rowCounts,
      final: await js(`({
        status: document.getElementById('status-text').textContent,
        summary: document.getElementById('results-summary').textContent,
        rows: document.querySelectorAll('#results li').length,
        unverified: document.querySelectorAll('#results .badge-warn').length,
        problems: document.getElementById('problems').hidden ? null : document.getElementById('problem-list').innerText,
        answer: document.getElementById('answer').hidden ? null : document.getElementById('answer').innerText.slice(0, 400),
        cites: document.querySelectorAll('#answer button.cite').length,
        citesLinked: document.querySelectorAll('#answer button.cite:not([disabled])').length,
        notices: [...document.querySelectorAll('#notices .notice p')].map(p => p.textContent),
        error: document.getElementById('error-panel').hidden ? null : document.getElementById('error-message').textContent,
      })`),
    };
    await shot('1-results');
    if (timedOut) throw new Error(`run took over ${maxMs / 60000} minutes`);

    // Click the first result: the reader opens with the quote highlighted.
    await click('#results li button.hit');
    await wait(`document.querySelector('#reader-doc mark') || !document.getElementById('reader-notice').hidden`, 10000, 'reader');
    await sleep(1500);
    summary.steps.reader = await js(`(() => {
      const mark = document.querySelector('#reader-doc mark');
      const box = document.getElementById('reader-scroll').getBoundingClientRect();
      const r = mark ? mark.getBoundingClientRect() : null;
      return {
        rowQuote: document.querySelector('#results li .row-quote').textContent,
        marked: mark ? [...document.querySelectorAll('#reader-doc mark')].map(m => m.textContent).join(' ') : null,
        markInView: r ? r.top >= box.top && r.bottom <= box.bottom : false,
        notice: document.getElementById('reader-notice').hidden ? null : document.getElementById('reader-notice').textContent,
        head: document.getElementById('reader-head').innerText,
        paragraphs: document.querySelectorAll('#reader-doc p').length,
        pageMarks: document.querySelectorAll('#reader-doc .page-mark, #reader-doc .page-inline').length,
        italic: [...document.querySelectorAll('#reader-doc em')].slice(0, 5).map(e => e.textContent),
        bold: [...document.querySelectorAll('#reader-doc strong')].slice(0, 3).map(e => e.textContent),
        rawAsterisks: (document.getElementById('reader-doc').textContent.match(/\\*\\S[^*]{0,30}/g) || []).slice(0, 5),
      };
    })()`);
    await shot('2-reader');

    // Scrolling near either edge grows the rendered window.
    const paras = () => js(`document.querySelectorAll('#reader-doc p, #reader-doc .doc-heading').length`);
    const p0 = await paras();
    await js(`document.getElementById('reader-scroll').scrollTop = 0`);
    await sleep(600);
    const p1 = await paras();
    await js(`(() => { const s = document.getElementById('reader-scroll'); s.scrollTop = s.scrollHeight; })()`);
    await sleep(600);
    const p2 = await paras();
    summary.steps.readerScroll = {
      blocksInitial: p0, afterScrollTop: p1, afterScrollBottom: p2,
      markStillPresent: await js(`!!document.querySelector('#reader-doc mark')`),
    };

    // A citation in the answer selects a row.
    if (await click('#answer button.cite:not([disabled])')) {
      await sleep(800);
      summary.steps.citation = await js(`({
        selectedFile: document.querySelector('#results li.selected .row-file')?.title || null,
        readerFile: document.getElementById('reader-file').textContent,
      })`);
    }

    // Drawer: open, an invalid value is reported inline, a valid one saves.
    await click('#drawer-toggle');
    await sleep(400);
    const temp = '#drawer input[data-path="tasks.extract.temperature"]';
    const before = await js(`document.querySelector('${temp}').value`);
    await setInput(temp, '5');
    await click('#drawer .drawer-foot button.primary');
    await wait(`/Not saved|Fix/.test(document.querySelector('#drawer .drawer-status').textContent)`, 10000, 'invalid save');
    const invalid = await js(`({
      status: document.querySelector('#drawer .drawer-status').textContent,
      fieldError: document.querySelector('${temp}').parentElement.querySelector('.field-error').textContent,
    })`);
    await shot('3-drawer-error');
    // A value that differs from the saved one, so there is something to save.
    await setInput(temp, before === '0.3' ? '0.4' : '0.3');
    await click('#drawer .drawer-foot button.primary');
    await wait(`/Saved/.test(document.querySelector('#drawer .drawer-status').textContent)`, 10000, 'save');
    summary.steps.drawer = {
      opened: await js(`document.getElementById('drawer').classList.contains('open')`),
      temperatureBefore: before,
      invalid,
      savedStatus: await js(`document.querySelector('#drawer .drawer-status').textContent`),
      temperatureAfter: await js(`document.querySelector('${temp}').value`),
    };
    await shot('4-drawer');
    await click('#drawer .drawer-head button');
    await sleep(400);
    summary.steps.drawer.closed = !(await js(`document.getElementById('drawer').classList.contains('open')`));

    // Open results: the run just saved a file; reopen it into a fresh view.
    const resultsDir = path.join(env.CORPUSQA_SMOKE_FOLDER, '.corpusqa', 'results');
    const saved = fs.readdirSync(resultsDir).filter((n) => n.endsWith('.json')).sort();
    const savedRun = JSON.parse(fs.readFileSync(path.join(resultsDir, saved[saved.length - 1]), 'utf8'));
    summary.steps.documents.savedSelection = savedRun.selection;
    summary.steps.documents.filesRead = savedRun.files.map((f) => f.rel_path).sort();
    // Choose all again, so reopening the results has something to restore.
    await click('#docs-toggle');
    await wait(`!document.getElementById('docs-panel').hidden`, 5000, 'documents panel again');
    await js(docButton('Select all'));
    await js(`[...document.querySelectorAll('#docs-panel .docs-foot button')].pop().click()`);
    summary.steps.documents.beforeReopen = await docText();
    process.env.CORPUSQA_SMOKE_PICK = path.join(resultsDir, saved[saved.length - 1]);
    const rowsBefore = await js(`document.querySelectorAll('#results li').length`);
    await click('#open-results');
    await wait(`document.getElementById('status-text').textContent.startsWith('Saved results from')`, 10000, 'saved results');
    await click('#results li button.hit');
    await wait(`document.querySelector('#reader-doc mark') || !document.getElementById('reader-notice').hidden`, 10000, 'reader on saved results');
    summary.steps.openResults = await js(`({
      file: ${JSON.stringify(path.basename(process.env.CORPUSQA_SMOKE_PICK))},
      status: document.getElementById('status-text').textContent,
      rowsBefore: ${rowsBefore},
      rows: document.querySelectorAll('#results li').length,
      question: document.getElementById('question').value,
      marked: !!document.querySelector('#reader-doc mark'),
      notice: document.getElementById('reader-notice').hidden ? null : document.getElementById('reader-notice').textContent,
    })`);
    await wait(`/of 3/.test(document.getElementById('docs-toggle').textContent)`, 5000, 'selection restored');
    summary.steps.openResults.documents = await docText();
    await shot('3-opened-results');
    delete process.env.CORPUSQA_SMOKE_PICK;

    // Choosing the folder again with empty fields brings back its last
    // run's question and definition.
    await setInput('#question', '');
    await setInput('#definition', '');
    await setInput('#folder', '');
    await setInput('#folder', env.CORPUSQA_SMOKE_FOLDER);
    await wait(`document.getElementById('question').value !== ''`, 10000, 'question restored');
    summary.steps.restoredQuestion = await js(`({
      question: document.getElementById('question').value,
      definition: document.getElementById('definition').value,
      status: document.getElementById('status-text').textContent,
      remembered: localStorage.getItem('corpusqa.question'),
    })`);
    summary.steps.restoredQuestion.matches = summary.steps.restoredQuestion.question === savedRun.question
      && summary.steps.restoredQuestion.definition === (savedRun.definition || '');

    // Group answers on the opened results; the Answers tab shows the groups.
    const tg = Date.now();
    await click('#group-answers');
    await wait(`/^(Grouped into|Grouping cancelled|Grouping stopped|Not started)/.test(document.getElementById('status-text').textContent)`, maxMs, 'grouping');
    await sleep(500);
    await click('#groups .group details summary');
    await sleep(300);
    const groupQuote = await js(`document.querySelector('#groups .group .hit .row-quote').textContent`);
    await click('#groups .group .hit');
    // Wait for the reader to show THIS hit (an earlier highlight may still be up).
    const norm = (t) => t.replace(/\W+/g, ' ').trim().toLowerCase();
    let readerMatch = false;
    for (let i = 0; i < 50 && !readerMatch; i++) {
      const marked = await js(`[...document.querySelectorAll('#reader-doc mark')].map(m => m.textContent).join(' ')`);
      readerMatch = !!marked && norm(marked).includes(norm(groupQuote).slice(0, 40));
      if (!readerMatch) await sleep(200);
    }
    summary.steps.group = await js(`({
      status: document.getElementById('status-text').textContent,
      tabShown: !document.getElementById('tab-groups').hidden,
      groupsVisible: !document.getElementById('groups').hidden,
      groups: [...document.querySelectorAll('#groups .group:not(.group-ungrouped)')].map(g => ({
        answer: g.querySelector('.group-answer').textContent,
        meta: g.querySelector('.group-meta').textContent,
      })),
      ungrouped: document.querySelector('#groups .group-ungrouped .group-meta')?.textContent ?? null,
      notices: [...document.querySelectorAll('#notices .notice p')].map(p => p.textContent),
      error: document.getElementById('error-panel').hidden ? null : document.getElementById('error-message').textContent,
      readerMarked: !!document.querySelector('#reader-doc mark'),
    })`);
    summary.steps.group.readerMatchesClickedHit = readerMatch;
    summary.steps.group.seconds = Math.round((Date.now() - tg) / 1000);
    await shot('3b-grouped');

    // Cancel: start again (cache off) and cancel once reading starts.
    await click('#run');
    await wait(`document.body.dataset.runState === 'running'`, 10000, 'second run');
    await wait(`/^Reading|^Converting/.test(document.getElementById('status-text').textContent) || document.body.dataset.runState !== 'running'`, 120000, 'progress');
    const statusAtCancel = await status();
    await click('#cancel');
    const tc = Date.now();
    await wait(`document.body.dataset.runState !== 'running'`, 120000, 'cancel');
    summary.steps.cancel = {
      statusAtCancel,
      secondsToStop: Math.round((Date.now() - tc) / 100) / 10,
      final: await status(),
    };

    // Wording help: ask the chat model, then use its suggested question.
    await click('#chat-toggle');
    await wait(`document.getElementById('chat').classList.contains('open') && document.querySelectorAll('#chat select')[1].value`, 15000, 'chat panel');
    await setInput('#chat .chat-input', 'Does the text say that the firstborn is the beginning of a man\'s strength because he is the firstborn, or that the child who is the beginning of a man\'s strength is accounted his firstborn? How should I phrase this?');
    await click('#chat .chat-actions button.primary');
    const tchat = Date.now();
    await wait(`document.querySelector('#chat .msg-assistant:not(.msg-pending)') || document.querySelector('#chat .field-error')`, 300000, 'chat reply');
    const useButtons = await js(`document.querySelectorAll('#chat .suggestion .use').length`);
    if (useButtons) await click('#chat .suggestion .use');
    await sleep(300);
    summary.steps.chat = await js(`({
      model: document.querySelectorAll('#chat select')[1].value,
      reply: document.querySelector('#chat .msg-assistant:not(.msg-pending)')?.innerText ?? null,
      error: document.querySelector('#chat .field-error')?.textContent ?? null,
      suggestions: [...document.querySelectorAll('#chat .suggestion')].map(s => s.innerText),
      question: document.getElementById('question').value,
      definition: document.getElementById('definition').value,
    })`);
    summary.steps.chat.seconds = Math.round((Date.now() - tchat) / 1000);
    summary.steps.chat.used = useButtons > 0;
    await shot('5-chat');
    summary.ok = true;
  } catch (err) {
    summary.error = String(err && err.stack ? err.stack : err);
  } finally {
    if (storedSettings !== null) {
      try {
        summary.settingsRestored = await js(`(() => { localStorage.clear();
          for (const [k, v] of JSON.parse(${JSON.stringify(storedSettings)})) localStorage.setItem(k, v);
          return localStorage.length; })()`);
      } catch (err) {
        summary.settingsRestoreError = String(err);
      }
    }
    if (env.CORPUSQA_SMOKE_OUT) fs.writeFileSync(env.CORPUSQA_SMOKE_OUT, JSON.stringify(summary, null, 2));
    quit();
  }
}

module.exports = { run };
