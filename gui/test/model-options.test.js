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
