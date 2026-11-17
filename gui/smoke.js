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
  try {
    await new Promise((r) => wc.once('did-finish-load', r));
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
    await click('#run');
    const statuses = [];
    const rowCounts = [];
    const t0 = Date.now();
    await wait(`document.body.dataset.runState === 'running'`, 10000, 'run start');
    while ((await js(`document.body.dataset.runState`)) === 'running') {
      const s = (await status()).replace(/ · \d+:\d\d(:\d\d)?$/, '');
      if (statuses[statuses.length - 1] !== s) statuses.push(s);
      const n = await js(`document.querySelectorAll('#results li').length`);
      if (rowCounts[rowCounts.length - 1] !== n) rowCounts.push(n);
      if (Date.now() - t0 > 20 * 60 * 1000) throw new Error('run took over 20 minutes');
      await sleep(250);
    }
    summary.steps.run = {
      seconds: Math.round((Date.now() - t0) / 1000),
      statusSamples: statuses.length,
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
    await setInput(temp, '0.3');
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
    summary.ok = true;
  } catch (err) {
    summary.error = String(err && err.stack ? err.stack : err);
  } finally {
    if (env.CORPUSQA_SMOKE_OUT) fs.writeFileSync(env.CORPUSQA_SMOKE_OUT, JSON.stringify(summary, null, 2));
    quit();
  }
}

module.exports = { run };
