'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { FileSelection, matchesFilter } = require('../src/file-selection.js');

const FILES = ['vol1/Christadelphian 1864.docx', 'vol1/Christadelphian 1865.docx',
  'vol2/Christadelphian 1898.docx', 'Elpis Israel.docx'].map((rel_path) => ({ rel_path }));

test('everything is chosen by default and the list sent is null', () => {
  const s = new FileSelection(FILES);
  assert.equal(s.chosenCount, 4);
  assert.equal(s.isAll, true);
  assert.equal(s.chosenList(), null);
  assert.equal(s.summary(), 'All 4 documents');
});

test('filter matches every word, in any order, ignoring case', () => {
  assert.equal(matchesFilter('vol2/Christadelphian 1898.docx', '1898 christ'), true);
  assert.equal(matchesFilter('vol2/Christadelphian 1898.docx', '1898 elpis'), false);
  assert.equal(matchesFilter('anything', '   '), true);
  const s = new FileSelection(FILES);
  assert.deepEqual(s.visible('vol1').map((f) => f.rel_path),
    ['vol1/Christadelphian 1864.docx', 'vol1/Christadelphian 1865.docx']);
});

test('select none and select all act on the shown files only', () => {
  const s = new FileSelection(FILES);
  s.selectNone(s.visible('vol1'));
  assert.equal(s.chosenCount, 2);
  assert.deepEqual(s.chosenList(), ['Elpis Israel.docx', 'vol2/Christadelphian 1898.docx']);
  assert.equal(s.summary(), '2 of 4 documents');
  s.selectNone(s.visible(''));
  assert.equal(s.chosenCount, 0);
  assert.equal(s.summary(), 'No documents');
  s.selectAll(s.visible('1898'));
  assert.deepEqual(s.chosenList(), ['vol2/Christadelphian 1898.docx']);
});

test('toggle flips one file', () => {
  const s = new FileSelection(FILES);
  s.toggle('Elpis Israel.docx');
  assert.equal(s.isChosen('Elpis Israel.docx'), false);
  s.toggle('Elpis Israel.docx');
  assert.equal(s.isAll, true);
});

test('restoring a saved list keeps what exists and reports what does not', () => {
  const s = new FileSelection(FILES);
  const missing = s.restore(['Elpis Israel.docx', 'gone.docx']);
  assert.deepEqual(missing, ['gone.docx']);
  assert.deepEqual(s.chosenList(), ['Elpis Israel.docx']);
  assert.deepEqual(s.restore(null), []);
  assert.equal(s.isAll, true);
});

test('a remembered subset leaves files added later unchosen', () => {
  const s = new FileSelection([...FILES, { rel_path: 'new.docx' }]);
  s.restore(['Elpis Israel.docx']);
  assert.equal(s.isChosen('new.docx'), false);
});
