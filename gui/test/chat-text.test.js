'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { parseReply } = require('../src/chat-text.js');

test('suggestions become their own segments; the rest is text', () => {
  const reply = 'Try this:\n<question>How do the writers explain X?</question>\n'
    + '<definition>Include A and B.</definition>\nBecause sections are read alone.';
  assert.deepEqual(parseReply(reply), [
    { kind: 'text', text: 'Try this:' },
    { kind: 'question', text: 'How do the writers explain X?' },
    { kind: 'definition', text: 'Include A and B.' },
    { kind: 'text', text: 'Because sections are read alone.' },
  ]);
});

test('thinking blocks are dropped, and an unclosed one hides the rest', () => {
  assert.deepEqual(parseReply('<think>hmm</think>Hello'), [{ kind: 'text', text: 'Hello' }]);
  assert.deepEqual(parseReply('Hi<think>still going'), [{ kind: 'text', text: 'Hi' }]);
});

test('an empty suggestion or plain reply stays text; tags are case-insensitive', () => {
  assert.deepEqual(parseReply('Just a question back?'), [{ kind: 'text', text: 'Just a question back?' }]);
  assert.deepEqual(parseReply('<QUESTION> Q </QUESTION><question>  </question>'), [
    { kind: 'question', text: 'Q' },
  ]);
  assert.deepEqual(parseReply(''), []);
});
