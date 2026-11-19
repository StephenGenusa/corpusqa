'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { OTHER, modelOptions } = require('../src/model-options.js');

const LISTING = {
  models: [
    { id: 'lm_studio/zeta', loaded: false },
    { id: 'lm_studio/alpha', loaded: false },
    { id: 'lm_studio/mid', loaded: true, loaded_context: 16384 },
  ],
};

test('loaded models first, then by name, then Other', () => {
  const { options } = modelOptions(LISTING, 'lm_studio/alpha');
  assert.deepEqual(options.map((o) => o.value), [
    'lm_studio/mid', 'lm_studio/alpha', 'lm_studio/zeta', OTHER,
  ]);
  assert.equal(options[0].text, 'lm_studio/mid — loaded · 16,384 ctx');
  assert.equal(options[1].text, 'lm_studio/alpha — not loaded');
});

test('the configured model is selected', () => {
  const { options, listed } = modelOptions(LISTING, 'lm_studio/zeta');
  assert.equal(listed, true);
  assert.deepEqual(options.filter((o) => o.selected).map((o) => o.value), ['lm_studio/zeta']);
});

test('an unlisted configured model is kept, flagged and selected', () => {
  const { options, listed } = modelOptions(LISTING, 'lm_studio/gone@q8_0');
  assert.equal(listed, false);
  assert.equal(options[0].value, 'lm_studio/gone@q8_0');
  assert.match(options[0].text, /not in this provider's list/);
  assert.equal(options[0].selected, true);
  assert.equal(options.filter((o) => o.selected).length, 1);
});

test('no configured model selects a disabled placeholder', () => {
  const { options } = modelOptions(LISTING, '');
  assert.deepEqual(options[0], { value: '', text: 'Choose a model…', selected: true, disabled: true });
});

test('an empty or failed listing still offers Other', () => {
  const { options } = modelOptions({ models: [], error: 'down' }, 'ollama_chat/x');
  assert.deepEqual(options.map((o) => o.value), ['ollama_chat/x', OTHER]);
});

// -- selectionChanges --------------------------------------------------------

const { selectionChanges } = require('../src/model-options.js');

const NAMES = ['catalog_summarize', 'query_route', 'extract', 'synthesize'];
const exampleTasks = () => ({
  catalog_summarize: { provider: 'openrouter', model: 'openrouter/a', context_window: 200000 },
  query_route: { provider: 'openrouter', model: 'openrouter/b', context_window: 1000000 },
  extract: { provider: 'ollama_local', model: 'ollama/q', context_window: 32768 },
  synthesize: { provider: 'openrouter', model: 'openrouter/a', context_window: 200000 },
});
const windows = (changes) => Object.fromEntries(
  changes.filter(([k]) => k[2] === 'context_window').map(([k, v]) => [k[1], v]));

test('switching to a local model drops the hosted models\' huge windows', () => {
  const changes = selectionChanges(exampleTasks(), NAMES, 'lm_studio', 'lm_studio/g', { models: [] });
  // No load info: the extract task's window (extract already has it).
  assert.deepEqual(windows(changes), { catalog_summarize: 32768, query_route: 32768, synthesize: 32768 });
  assert.ok(changes.some(([k, v]) => k.join('.') === 'tasks.extract.model' && v === 'lm_studio/g'));
});

test('the window LM Studio loaded the model with wins', () => {
  const listing = { models: [{ id: 'lm_studio/g', loaded: true, loaded_context: 16384 }] };
  const changes = selectionChanges(exampleTasks(), NAMES, 'lm_studio', 'lm_studio/g', listing);
  assert.deepEqual(windows(changes), {
    catalog_summarize: 16384, query_route: 16384, extract: 16384, synthesize: 16384,
  });
});

test('the window is capped at the model\'s maximum', () => {
  const listing = { models: [{ id: 'lm_studio/g', loaded: false, max_context: 8192 }] };
  const changes = selectionChanges(exampleTasks(), NAMES, 'lm_studio', 'lm_studio/g', listing);
  assert.equal(windows(changes).synthesize, 8192);
});

test('tasks already on the model keep their settings', () => {
  const tasks = exampleTasks();
  for (const t of NAMES) Object.assign(tasks[t], { provider: 'lm_studio', model: 'lm_studio/g', context_window: 12000 });
  assert.deepEqual(selectionChanges(tasks, NAMES, 'lm_studio', 'lm_studio/g', { models: [] }), []);
});
