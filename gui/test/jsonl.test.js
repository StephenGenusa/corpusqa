'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { createLineDecoder } = require('../src/jsonl');

test('decoder handles split and merged chunks', () => {
  const got = [];
  const feed = createLineDecoder((o) => got.push(o), () => {});
  feed('{"a":1}\n{"b"');
  feed(':2}\r\n');
  assert.deepEqual(got, [{ a: 1 }, { b: 2 }]);
});

test('decoder reports bad lines and keeps going', () => {
  const got = [];
  const bad = [];
  const feed = createLineDecoder((o) => got.push(o), (l) => bad.push(l));
  feed('not json\n{"ok":true}\n\n');
  assert.deepEqual(got, [{ ok: true }]);
  assert.deepEqual(bad, ['not json']);
});

test('decoder holds a partial line until its newline arrives', () => {
  const got = [];
  const feed = createLineDecoder((o) => got.push(o), () => {});
  feed('{"x":');
  assert.deepEqual(got, []);
  feed('"é"}');
  assert.deepEqual(got, []);
  feed('\n');
  assert.deepEqual(got, [{ x: 'é' }]);
});

test('decoder treats non-object JSON as a bad line', () => {
  const got = [];
  const bad = [];
  const feed = createLineDecoder((o) => got.push(o), (l) => bad.push(l));
  feed('42\n[1]\n');
  assert.deepEqual(got, []);
  assert.deepEqual(bad, ['42', '[1]']);
});
