'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const {
  readerTarget,
  pageLabelAt,
  windowAround,
  extendBefore,
  extendAfter,
  buildBlocks,
  parseCitations,
  matchCitation,
} = require('../src/reader');

test('readerTarget returns notice for unverified finding', () => {
  assert.deepEqual(readerTarget({ verified: false, start: null, end: null }), {
    offset: null,
    notice: 'Quote not located in source',
  });
});

test('readerTarget returns the start offset for a verified finding', () => {
  assert.deepEqual(readerTarget({ verified: true, start: 120, end: 140 }), {
    offset: 120,
    notice: null,
  });
});

test('readerTarget treats a verified finding without offset as not located', () => {
  assert.deepEqual(readerTarget({ verified: true, start: null, end: null }), {
    offset: null,
    notice: 'Quote not located in source',
  });
});

test('pageLabelAt picks last mark at or before offset', () => {
  assert.equal(pageLabelAt([[0, '1'], [100, 'ii']], 150), 'ii');
  assert.equal(pageLabelAt([[0, '1'], [100, 'ii']], 100), 'ii');
  assert.equal(pageLabelAt([[0, '1'], [100, 'ii']], 99), '1');
});

test('pageLabelAt returns null before the first mark or with no map', () => {
  assert.equal(pageLabelAt([[50, '3']], 10), null);
  assert.equal(pageLabelAt([], 10), null);
  assert.equal(pageLabelAt(null, 10), null);
});

test('windowAround snaps to paragraph boundaries', () => {
  const text = 'aaaa\n\nbbbb bbbb\n\ncccc\n\ndddd';
  // offset inside "bbbb bbbb" (index 8), radius 3 -> start 5, end 11
  const w = windowAround(text, 8, 3);
  assert.equal(w.start, 6); // start of "bbbb bbbb"
  assert.equal(w.end, 15); // end of "bbbb bbbb"
  assert.equal(text.slice(w.start, w.end), 'bbbb bbbb');
});

test('windowAround clamps to the text and handles a null offset', () => {
  const text = 'one\n\ntwo\n\nthree';
  assert.deepEqual(windowAround(text, null, 4), { start: 0, end: 8 });
  assert.deepEqual(windowAround(text, 0, 1000), { start: 0, end: text.length });
});

test('windowAround falls back to a raw cut when no paragraph break is near', () => {
  const text = 'x'.repeat(100000);
  const w = windowAround(text, 50000, 1000);
  assert.ok(w.start >= 48000 && w.start <= 49000, `start ${w.start}`);
  assert.ok(w.end >= 51000 && w.end <= 52000, `end ${w.end}`);
});

test('extendBefore and extendAfter grow the window on paragraph boundaries', () => {
  const text = 'p1\n\np2\n\np3\n\np4\n\np5';
  // p3 = [8, 10)
  assert.equal(extendBefore(text, 8, 3), 4); // p2 start
  assert.equal(extendAfter(text, 10, 3), 14); // p4 end
  assert.equal(extendBefore(text, 0, 3), 0);
  assert.equal(extendAfter(text, text.length, 3), text.length);
});

test('buildBlocks splits paragraphs, styles headings and highlights', () => {
  const text = '# Title\n\nSome words here.\n\nMore.';
  const blocks = buildBlocks(text, 0, text.length, {
    pageMap: [],
    highlight: [14, 19],
  });
  assert.equal(blocks.length, 3);
  assert.deepEqual(blocks[0], {
    type: 'heading', level: 1, start: 0,
    runs: [{ kind: 'text', text: 'Title' }],
  });
  assert.equal(blocks[1].type, 'para');
  assert.deepEqual(blocks[1].runs, [
    { kind: 'text', text: 'Some ' },
    { kind: 'hl', text: 'words' },
    { kind: 'text', text: ' here.' },
  ]);
  assert.equal(blocks[2].start, 27);
});

test('buildBlocks places page markers inline or before a paragraph', () => {
  const text = 'Alpha beta\n\nGamma';
  const blocks = buildBlocks(text, 0, text.length, {
    pageMap: [[0, '1'], [6, '2'], [12, '3']],
    highlight: null,
  });
  // mark at 0 is the start of the first paragraph: a block marker before it
  assert.deepEqual(blocks[0], { type: 'page', label: '1', start: 0 });
  assert.deepEqual(blocks[1].runs, [
    { kind: 'text', text: 'Alpha ' },
    { kind: 'page', label: '2' },
    { kind: 'text', text: 'beta' },
  ]);
  assert.deepEqual(blocks[2], { type: 'page', label: '3', start: 12 });
  assert.deepEqual(blocks[3].runs, [{ kind: 'text', text: 'Gamma' }]);
});

test('buildBlocks highlight spanning paragraphs marks both parts', () => {
  const text = 'aaa bbb\n\nccc ddd';
  const blocks = buildBlocks(text, 0, text.length, { pageMap: [], highlight: [4, 12] });
  assert.deepEqual(blocks[0].runs, [
    { kind: 'text', text: 'aaa ' },
    { kind: 'hl', text: 'bbb' },
  ]);
  assert.deepEqual(blocks[1].runs, [
    { kind: 'hl', text: 'ccc' },
    { kind: 'text', text: ' ddd' },
  ]);
});

test('buildBlocks respects a window that starts mid-document', () => {
  const text = 'skip\n\nkeep this\n\nskip';
  const blocks = buildBlocks(text, 6, 15, { pageMap: [[0, '1']], highlight: null });
  assert.equal(blocks.length, 1);
  assert.equal(blocks[0].start, 6);
  assert.deepEqual(blocks[0].runs, [{ kind: 'text', text: 'keep this' }]);
});

test('parseCitations finds markers for known files', () => {
  const answer =
    'Ideas include X [skills/a b.md § Ideas, p.12] and Y [notes.txt (unverified)].';
  const segs = parseCitations(answer, ['skills/a b.md', 'notes.txt']);
  assert.deepEqual(segs, [
    { kind: 'text', text: 'Ideas include X ' },
    {
      kind: 'cite', text: '[skills/a b.md § Ideas, p.12]',
      relPath: 'skills/a b.md', heading: 'Ideas', page: '12', unverified: false,
    },
    { kind: 'text', text: ' and Y ' },
    {
      kind: 'cite', text: '[notes.txt (unverified)]',
      relPath: 'notes.txt', heading: null, page: null, unverified: true,
    },
    { kind: 'text', text: '.' },
  ]);
});

test('parseCitations leaves unknown brackets as text', () => {
  const segs = parseCitations('see [1] and [other.md]', ['notes.txt']);
  assert.deepEqual(segs, [{ kind: 'text', text: 'see [1] and [other.md]' }]);
});

test('matchCitation prefers heading and page, then falls back to the file', () => {
  const rows = [
    { rel_path: 'a.md', heading_path: ['Intro'], page_label: '1' },
    { rel_path: 'a.md', heading_path: ['Ideas'], page_label: '3' },
    { rel_path: 'a.md', heading_path: ['Ideas'], page_label: '4' },
    { rel_path: 'b.md', heading_path: [], page_label: null },
  ];
  const cite = (heading, page, relPath = 'a.md') => ({ relPath, heading, page });
  assert.equal(matchCitation(cite('Ideas', '4'), rows), 2);
  assert.equal(matchCitation(cite('Ideas', null), rows), 1);
  assert.equal(matchCitation(cite('Nope', '1'), rows), 0);
  assert.equal(matchCitation(cite(null, null, 'b.md'), rows), 3);
  assert.equal(matchCitation(cite(null, null, 'c.md'), rows), -1);
});
