'use strict';

// Drives the real app through the README demo and captures one PNG per
// screen. Loaded by gui/main.js only when CORPUSQA_DEMO=1; run it through
// demo/make_gif.sh. Inputs: CORPUSQA_DEMO_FOLDER (the built library),
// CORPUSQA_DEMO_MODEL, CORPUSQA_DEMO_OUT (frames directory).
//
// The model really reads the books: every hit, grouping and answer shown is
// what this run produced. The app's remembered settings are put back at the
// end, as the smoke test does.

const fs = require('node:fs');
const path = require('node:path');

const QUESTION = 'What do the authors recommend for preventing race conditions in concurrent code?';
const DEFINITION = 'Include locks and mutexes, atomic operations, immutable data, message passing, '
  + 'and any warnings about deadlock or lock contention.';
const CHAT = 'I want to find what these books say about avoiding race conditions when several '
  + 'threads share data. How should I phrase the question?';

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function run(win, quit) {
  const env = process.env;
  const wc = win.webContents;
  const js = (code) => wc.executeJavaScript(code, true);
  const out = env.CORPUSQA_DEMO_OUT;
  const log = { frames: [], errors: [] };
  let n = 0;
  const wait = async (expr, timeoutMs, label) => {
    const end = Date.now() + timeoutMs;
    while (Date.now() < end) {
      if (await js(expr)) return;
      await sleep(250);
    }
    throw new Error(`timed out waiting for ${label}`);
  };
  const shot = async (name) => {
    await sleep(700); // let transitions settle
    const file = path.join(out, `${String(++n).padStart(2, '0')}-${name}.png`);
    fs.writeFileSync(file, (await wc.capturePage()).toPNG());
    log.frames.push(path.basename(file));
  };
  const set = (sel, value) => js(`(() => { const el = document.querySelector(${JSON.stringify(sel)});
    el.value = ${JSON.stringify(value)}; el.dispatchEvent(new Event('input', {bubbles: true}));
    el.dispatchEvent(new Event('change', {bubbles: true})); return true; })()`);
  const click = (sel) => js(`(() => { const el = document.querySelector(${JSON.stringify(sel)});
    if (!el) return false; el.click(); return true; })()`);

  let stored = null;
  try {
    await new Promise((r) => wc.once('did-finish-load', r));
    stored = await js('JSON.stringify(Object.entries(localStorage))');
    await wait(`document.body.dataset.ready === '1'`, 30000, 'app ready');

    // Run options: sweep every book, write an answer, read afresh.
    await js(`(() => { localStorage.setItem('corpusqa.queryOptions', JSON.stringify(
      { relevance: 'recall', mode: 'auto', synthesize: true, use_cache: false })); return true; })()`);
    await wc.reload();
    await wait(`document.body.dataset.ready === '1'`, 30000, 'app ready after reload');
    await wait(`[...document.querySelectorAll('#model option')].some(o => o.value === ${JSON.stringify(env.CORPUSQA_DEMO_MODEL)})`, 20000, 'model list');
    await set('#model', env.CORPUSQA_DEMO_MODEL);
    await set('#folder', env.CORPUSQA_DEMO_FOLDER);
    await set('#question', QUESTION);
    await set('#definition', DEFINITION);
    await wait(`/documents/i.test(document.getElementById('docs-toggle').textContent)`, 15000, 'documents listed');
    await js(`document.activeElement.blur(); true`);
    await shot('question');

    // Wording help: ask how to phrase it.
    await click('#chat-toggle');
    await wait(`document.getElementById('chat').classList.contains('open') && document.querySelectorAll('#chat select')[1].value`, 15000, 'chat');
    await set('#chat .chat-input', CHAT);
    await click('#chat .chat-actions button.primary');
    await wait(`document.querySelector('#chat .msg-assistant:not(.msg-pending)') || document.querySelector('#chat .field-error')`, 300000, 'chat reply');
    // Show the reply from its first suggestion.
    await js(`(() => { const s = document.querySelector('#chat .suggestion') || document.querySelector('#chat .msg-assistant');
      const logEl = document.querySelector('#chat .chat-log'); logEl.scrollTop = s.offsetTop - logEl.offsetTop - 70; return true; })()`);
    await shot('wording-help');
    await click('#chat .drawer-head button');
    await sleep(400);

    // Documents: the whole shelf, with the filter.
    await click('#docs-toggle');
    await wait(`!document.getElementById('docs-panel').hidden && document.querySelectorAll('#docs-panel .doc').length > 0`, 10000, 'documents panel');
    await js(`document.activeElement.blur(); true`);
    await shot('documents');
    await js(`[...document.querySelectorAll('#docs-panel .docs-foot button')].pop().click()`);

    // Run: capture it part way, with the time left showing.
    await click('#run');
    await wait(`document.body.dataset.runState === 'running'`, 15000, 'run start');
    await wait(`/Reading .* left/.test(document.getElementById('status-text').textContent) && (parseFloat(document.getElementById('bar-fill').style.width) || 0) >= 35`, 900000, 'progress');
    await shot('reading');
    await wait(`document.body.dataset.runState !== 'running'`, 1800000, 'run end');
    log.finished = await js(`document.getElementById('status-text').textContent`);

    // Hits: open one in the reader.
    await click('#results li button.hit');
    await wait(`document.querySelector('#reader-doc mark') || !document.getElementById('reader-notice').hidden`, 15000, 'reader');
    await shot('hits');

    // Group answers: the distinct answers, one opened.
    await click('#group-answers');
    await wait(`/^(Grouped into|Grouping cancelled|Grouping stopped)/.test(document.getElementById('status-text').textContent)`, 900000, 'grouping');
    log.grouped = await js(`document.getElementById('status-text').textContent`);
    await js(`document.getElementById('groups').scrollIntoView(); true`);
    await shot('answers');
    await click('#groups .group details summary');
    await sleep(300);
    await click('#groups .group .hit');
    await wait(`document.querySelector('#reader-doc mark') || !document.getElementById('reader-notice').hidden`, 15000, 'reader on group hit');
    await shot('answer-opened');

    log.notices = await js(`[...document.querySelectorAll('#notices .notice p')].map(p => p.textContent)`);
    log.groups = await js(`[...document.querySelectorAll('#groups .group')].map(g => g.querySelector('.group-answer').textContent + ' — ' + g.querySelector('.group-meta').textContent)`);
    log.ok = true;
  } catch (err) {
    log.errors.push(String(err && err.stack ? err.stack : err));
  } finally {
    if (stored !== null) {
      try {
        await js(`(() => { localStorage.clear();
          for (const [k, v] of JSON.parse(${JSON.stringify(stored)})) localStorage.setItem(k, v); return true; })()`);
      } catch (err) {
        log.errors.push(`settings not restored: ${err}`);
      }
    }
    fs.writeFileSync(path.join(out, 'record.json'), JSON.stringify(log, null, 2));
    quit();
  }
}

module.exports = { run };
