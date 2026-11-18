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
  decodeEntities,
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

// --- code-point offsets (Python) -> UTF-16 indices (JS) ---
const { codePointMapper, mapDocOffsets } = require('../src/reader');

test('codePointMapper is the identity for text without astral characters', () => {
  const toUnit = codePointMapper('plain text, café — ünïcode');
  assert.equal(toUnit(0), 0);
  assert.equal(toUnit(7), 7);
  assert.equal(toUnit(null), null);
});

test('codePointMapper shifts offsets after each astral character', () => {
  const text = '😀 intro\n\nThe quote here.';
  const toUnit = codePointMapper(text);
  // Python: text.index("The") == 9 (code points); the JS index is 10.
  assert.equal(toUnit(9), 10);
  assert.equal(text.slice(toUnit(9), toUnit(9 + 15)), 'The quote here.');
  assert.equal(toUnit(0), 0); // before the emoji
  assert.equal(toUnit(1), 2); // just after it
});

test('codePointMapper counts several astral characters, including adjacent ones', () => {
  const text = 'a𝔄b😀😀c𠀀d';
  const toUnit = codePointMapper(text);
  const cps = [...text]; // Python-style code points
  for (let cp = 0; cp <= cps.length; cp++) {
    assert.equal(text.slice(0, toUnit(cp)), cps.slice(0, cp).join(''), `cp ${cp}`);
  }
});

test('mapDocOffsets converts page_map offsets and keeps a mapper', () => {
  const text = '😀😀 p1\n\np2';
  // Code points: 😀 0, 😀 1, ' ' 2, p 3, 1 4, \n 5, \n 6, p 7.
  const doc = mapDocOffsets({ markdown: text, page_map: [[0, '1'], [7, '2']] });
  assert.deepEqual(doc.page_map, [[0, '1'], [9, '2']]);
  assert.equal(text.slice(9), 'p2');
  assert.equal(doc.toUnit(7), 9);
});


test('decodeEntities turns the converter\'s escapes back into text', () => {
  assert.equal(decodeEntities('exalted him,\u2019 &amp;c. (Phil. 2:8)'), 'exalted him,\u2019 &c. (Phil. 2:8)');
  assert.equal(decodeEntities('a &lt;b&gt; c &#124; d &#x7C; e &quot;f&quot; &apos;g&apos;'), 'a <b> c | d | e "f" \'g\'');
  // One pass: an escaped escape stays one level escaped.
  assert.equal(decodeEntities('&amp;lt;'), '&lt;');
  // Unknown or malformed sequences are left alone.
  assert.equal(decodeEntities('&nbsp; & amp; &#xZZ; &#99999999;'), '&nbsp; & amp; &#xZZ; &#99999999;');
  assert.equal(decodeEntities(''), '');
});

test('buildBlocks turns *x*, **x** and ***x*** into styled runs without the asterisks', () => {
  const text = 'He *wherefore* and **bold** and ***both***.';
  const [block] = buildBlocks(text, 0, text.length, { pageMap: [], highlight: null });
  assert.deepEqual(block.runs, [
    { kind: 'text', text: 'He ' },
    { kind: 'text', text: 'wherefore', em: true },
    { kind: 'text', text: ' and ' },
    { kind: 'text', text: 'bold', strong: true },
    { kind: 'text', text: ' and ' },
    { kind: 'text', text: 'both', em: true, strong: true },
    { kind: 'text', text: '.' },
  ]);
});

test('buildBlocks leaves lone asterisks (footnote and omission marks) alone', () => {
  const text = 'idols * * and Paine. * The views and 2 * 3 = 6';
  const [block] = buildBlocks(text, 0, text.length, { pageMap: [], highlight: null });
  assert.deepEqual(block.runs, [{ kind: 'text', text }]);
});

test('buildBlocks keeps a highlight that starts inside an italic span', () => {
  const text = 'so *the joy set* before him';
  // highlight covers "joy set* before" in stored offsets
  const [block] = buildBlocks(text, 0, text.length, { pageMap: [], highlight: [8, 23] });
  assert.deepEqual(block.runs, [
    { kind: 'text', text: 'so ' },
    { kind: 'text', text: 'the ', em: true },
    { kind: 'hl', text: 'joy set', em: true },
    { kind: 'hl', text: ' before' },
    { kind: 'text', text: ' him' },
  ]);
});

test('buildBlocks styles headings but leaves table rows as written', () => {
  const text = '## ***The People’s Friend***\n\n| *a* | b |';
  const blocks = buildBlocks(text, 0, text.length, { pageMap: [], highlight: null });
  assert.deepEqual(blocks[0].runs, [{ kind: 'text', text: 'The People’s Friend', em: true, strong: true }]);
  assert.deepEqual(blocks[1].runs, [{ kind: 'text', text: '| *a* | b |' }]);
});

test('buildBlocks keeps a page mark that falls inside an italic span', () => {
  const text = 'a *bc de* f';
  const [block] = buildBlocks(text, 0, text.length, { pageMap: [[6, '9']], highlight: null });
  assert.deepEqual(block.runs, [
    { kind: 'text', text: 'a ' },
    { kind: 'text', text: 'bc ', em: true },
    { kind: 'page', label: '9' },
    { kind: 'text', text: 'de', em: true },
    { kind: 'text', text: ' f' },
  ]);
});
