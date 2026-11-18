/* Pure helpers for the reader pane and the synthesized answer.
 *
 * Loaded by Node (tests) via require() and by the renderer as a classic
 * script, where it defines `window.CorpusReader`.
 */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.CorpusReader = factory();
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';

  const NOT_LOCATED = 'Quote not located in source';
  const PARA = '\n\n';
  // How far a window edge may move to reach a paragraph boundary.
  const snapLimit = (radius) => Math.max(radius, 2000);

  /**
   * Where the reader should open for a finding.
   * @param {{verified: boolean, start: number|null}} finding
   * @returns {{offset: number|null, notice: string|null}}
   */
  function readerTarget(finding) {
    if (finding && finding.verified && Number.isInteger(finding.start)) {
      return { offset: finding.start, notice: null };
    }
    return { offset: null, notice: NOT_LOCATED };
  }

  /**
   * The page label in effect at `offset`: the last mark at or before it.
   * @param {Array<[number, string]>|null} pageMap sorted by offset
   * @param {number} offset
   * @returns {string|null}
   */
  function pageLabelAt(pageMap, offset) {
    if (!pageMap || pageMap.length === 0) return null;
    let lo = 0;
    let hi = pageMap.length - 1;
    let found = -1;
    while (lo <= hi) {
      const mid = (lo + hi) >> 1;
      if (pageMap[mid][0] <= offset) {
        found = mid;
        lo = mid + 1;
      } else {
        hi = mid - 1;
      }
    }
    return found === -1 ? null : pageMap[found][1];
  }

  // Start of the paragraph at or before `pos`, searched no further back than
  // `limit` characters; falls back to a line start, then to `pos` itself.
  function snapBack(text, pos, limit) {
    if (pos <= 0) return 0;
    const para = text.lastIndexOf(PARA, pos);
    if (para !== -1 && pos - para <= limit) return para + PARA.length;
    if (para === -1 && pos <= limit) return 0;
    const line = text.lastIndexOf('\n', pos);
    if (line !== -1 && pos - line <= limit) return line + 1;
    return pos;
  }

  // End of the paragraph at or after `pos` (exclusive), same fallbacks.
  function snapForward(text, pos, limit) {
    if (pos >= text.length) return text.length;
    const para = text.indexOf(PARA, pos);
    if (para !== -1 && para - pos <= limit) return para;
    if (para === -1 && text.length - pos <= limit) return text.length;
    const line = text.indexOf('\n', pos);
    if (line !== -1 && line - pos <= limit) return line;
    return pos;
  }

  /**
   * A window of about `radius` characters each side of `offset`, snapped
   * outwards to paragraph boundaries.
   * @param {string} text
   * @param {number|null} offset
   * @param {number} [radius]
   * @returns {{start: number, end: number}}
   */
  function windowAround(text, offset, radius = 20000) {
    const at = Math.min(Math.max(offset || 0, 0), text.length);
    const start = Math.min(snapBack(text, Math.max(0, at - radius), snapLimit(radius)), at);
    const end = Math.max(snapForward(text, Math.min(text.length, at + radius), snapLimit(radius)), at);
    return { start, end };
  }

  /**
   * New window start about `step` characters before `start`.
   * @returns {number} a paragraph start strictly before `start` (or 0)
   */
  function extendBefore(text, start, step = 20000) {
    if (start <= 0) return 0;
    const from = Math.max(0, Math.min(start - step, start - PARA.length - 1));
    const snapped = snapBack(text, from, snapLimit(step));
    return snapped < start ? snapped : Math.max(0, start - step);
  }

  /**
   * New window end about `step` characters after `end`.
   * @returns {number} a paragraph end strictly after `end` (or text length)
   */
  function extendAfter(text, end, step = 20000) {
    if (end >= text.length) return text.length;
    const from = Math.min(text.length, Math.max(end + step, end + 1));
    const snapped = snapForward(text, from, snapLimit(step));
    return snapped > end ? snapped : Math.min(text.length, end + step);
  }

  // `*italic*`, `**bold**`, `***both***` as the converter writes them: the
  // opening run must be followed, and the closing run preceded, by a
  // non-space, so footnote and omission marks ("idols * * and") stay text.
  const EMPHASIS = /(?<!\*)(\*{1,3})(?=[^\s*])[^*\n]*?[^\s*]\1(?!\*)/g;

  // Emphasis spans in [from, to): delimiter ranges (hidden) and styled ranges.
  function findEmphasis(text, from, to) {
    const spans = [];
    const slice = text.slice(from, to);
    for (const m of slice.matchAll(EMPHASIS)) {
      const n = m[1].length;
      const start = from + m.index;
      const end = start + m[0].length;
      spans.push({ open: start, inner: [start + n, end - n], close: end, em: n !== 2, strong: n >= 2 });
    }
    return spans;
  }

  // Splits [start, end) into runs of plain, highlighted and page-mark pieces;
  // text runs inside emphasis carry `em` / `strong`, and the asterisks go.
  function makeRuns(text, from, to, marks, highlight, { emphasis = true } = {}) {
    const cuts = new Set([from, to]);
    if (highlight) {
      if (highlight[0] > from && highlight[0] < to) cuts.add(highlight[0]);
      if (highlight[1] > from && highlight[1] < to) cuts.add(highlight[1]);
    }
    for (const [off] of marks) cuts.add(off);
    const spans = emphasis ? findEmphasis(text, from, to) : [];
    for (const s of spans) {
      cuts.add(s.open).add(s.inner[0]).add(s.inner[1]).add(s.close);
    }
    const points = [...cuts].sort((a, b) => a - b);
    const runs = [];
    let mi = 0;
    let si = 0;
    for (let i = 0; i < points.length; i++) {
      const p = points[i];
      while (mi < marks.length && marks[mi][0] === p) {
        runs.push({ kind: 'page', label: marks[mi][1] });
        mi++;
      }
      if (i + 1 >= points.length) break;
      const q = points[i + 1];
      if (q <= p) continue;
      while (si < spans.length && spans[si].close <= p) si++;
      const span = si < spans.length && spans[si].open <= p ? spans[si] : null;
      if (span && (p < span.inner[0] || p >= span.inner[1])) continue; // a delimiter
      const hl = highlight && p >= highlight[0] && q <= highlight[1];
      const piece = text.slice(p, q);
      const kind = hl ? 'hl' : 'text';
      const style = {};
      if (span?.em) style.em = true;
      if (span?.strong) style.strong = true;
      const last = runs[runs.length - 1];
      if (last && last.kind === kind && !last.em === !style.em && !last.strong === !style.strong) {
        last.text += piece;
      } else {
        runs.push({ kind, text: piece, ...style });
      }
    }
    return runs;
  }

  /**
   * Turns the window [start, end) into display blocks: headings, paragraphs
   * and page markers. Offsets are absolute positions in `text`.
   *
   * @param {string} text the whole document
   * @param {number} start window start (a paragraph start)
   * @param {number} end window end (a paragraph end)
   * @param {{pageMap: Array<[number,string]>, highlight: [number,number]|null}} o
   * @returns {Array<object>} blocks
   */
  function buildBlocks(text, start, end, { pageMap, highlight }) {
    const blocks = [];
    const marks = (pageMap || []).filter(([off]) => off >= start && off < end);
    let mi = 0;
    const sep = /\n{2,}/g;
    sep.lastIndex = start;
    let pos = start;
    while (pos < end) {
      const m = sep.exec(text);
      const paraEnd = m && m.index < end ? m.index : end;
      const nextPos = m && m.index < end ? m.index + m[0].length : end;
      // Marks before this paragraph's first character (or in the gap) become
      // a block marker ahead of it.
      while (mi < marks.length && marks[mi][0] <= pos) {
        blocks.push({ type: 'page', label: marks[mi][1], start: marks[mi][0] });
        mi++;
      }
      if (paraEnd > pos) {
        const inner = [];
        while (mi < marks.length && marks[mi][0] < paraEnd) inner.push(marks[mi++]);
        const raw = text.slice(pos, paraEnd);
        const heading = /^(#{1,6})[ \t]+/.exec(raw);
        if (heading) {
          const textStart = pos + heading[0].length;
          const runs = makeRuns(text, textStart, paraEnd,
            inner.map(([o, l]) => [Math.max(o, textStart), l]), highlight);
          blocks.push({ type: 'heading', level: heading[1].length, start: pos, runs });
        } else {
          const type = raw.startsWith('|') || raw.startsWith('```') ? 'pre' : 'para';
          // Table rows and code are shown as written.
          const runs = makeRuns(text, pos, paraEnd, inner, highlight, { emphasis: type === 'para' });
          blocks.push({ type, start: pos, runs });
        }
      }
      // Marks inside the separator attach to the next paragraph.
      pos = nextPos;
      if (!m || m.index >= end) break;
    }
    while (mi < marks.length) {
      blocks.push({ type: 'page', label: marks[mi][1], start: marks[mi][0] });
      mi++;
    }
    return blocks;
  }

  /**
   * Splits a synthesized answer into text and citation segments. A citation
   * is `[<known rel_path>[ § heading][, p.N][ (unverified)]]`.
   * @param {string} answer
   * @param {string[]} relPaths files that have results
   * @returns {Array<object>} segments
   */
  function parseCitations(answer, relPaths) {
    const paths = [...new Set(relPaths)].sort((a, b) => b.length - a.length);
    const segs = [];
    let textStart = 0;
    let i = answer.indexOf('[');
    while (i !== -1) {
      let cite = null;
      for (const p of paths) {
        if (!answer.startsWith(p, i + 1)) continue;
        const after = answer[i + 1 + p.length];
        if (after !== ']' && after !== ' ' && after !== ',') continue;
        const close = answer.indexOf(']', i + 1 + p.length);
        if (close === -1) continue;
        cite = parseCiteBody(answer.slice(i, close + 1), p, answer.slice(i + 1 + p.length, close));
        break;
      }
      if (cite) {
        if (i > textStart) segs.push({ kind: 'text', text: answer.slice(textStart, i) });
        segs.push(cite);
        textStart = i + cite.text.length;
        i = answer.indexOf('[', textStart);
      } else {
        i = answer.indexOf('[', i + 1);
      }
    }
    if (textStart < answer.length) segs.push({ kind: 'text', text: answer.slice(textStart) });
    return segs;
  }

  function parseCiteBody(text, relPath, rest) {
    let body = rest;
    let unverified = false;
    if (body.endsWith(' (unverified)')) {
      unverified = true;
      body = body.slice(0, -' (unverified)'.length);
    }
    let page = null;
    const p = body.lastIndexOf(', p.');
    if (p !== -1) {
      page = body.slice(p + 4).trim() || null;
      body = body.slice(0, p);
    }
    let heading = null;
    if (body.startsWith(' § ')) heading = body.slice(3).trim() || null;
    return { kind: 'cite', text, relPath, heading, page, unverified };
  }

  /**
   * Index of the result row a citation points at, or -1. Prefers the same
   * file with matching heading and page, then heading, then page, then the
   * file's first row.
   * @param {{relPath: string, heading: string|null, page: string|null}} cite
   * @param {Array<{rel_path: string, heading_path: string[], page_label: string|null}>} rows
   * @returns {number}
   */
  function matchCitation(cite, rows) {
    let best = -1;
    let bestScore = -1;
    rows.forEach((row, idx) => {
      if (row.rel_path !== cite.relPath) return;
      const hp = row.heading_path || [];
      const headingOk = cite.heading !== null && hp[hp.length - 1] === cite.heading;
      const pageOk = cite.page !== null && row.page_label === cite.page;
      const score = (headingOk ? 2 : 0) + (pageOk ? 1 : 0);
      if (score > bestScore) {
        best = idx;
        bestScore = score;
      }
    });
    return best;
  }

  /**
   * Maps Python code-point offsets to JavaScript UTF-16 indices for `text`.
   *
   * Each astral-plane character (emoji, CJK Ext-B, math letters) is one code
   * point but two UTF-16 units, so every one before an offset shifts it by one.
   * @param {string} text
   * @returns {(cp: number|null) => number|null}
   */
  function codePointMapper(text) {
    if (!/[\uD800-\uDFFF]/.test(text)) return (cp) => cp;
    const astral = []; // code-point positions of astral characters, ascending
    let cp = 0;
    for (let i = 0; i < text.length; i++, cp++) {
      const c = text.charCodeAt(i);
      if (c >= 0xd800 && c <= 0xdbff && i + 1 < text.length) {
        const d = text.charCodeAt(i + 1);
        if (d >= 0xdc00 && d <= 0xdfff) {
          astral.push(cp);
          i++;
        }
      }
    }
    return (offset) => {
      if (offset === null || offset === undefined) return offset;
      let lo = 0;
      let hi = astral.length; // count of astral positions < offset
      while (lo < hi) {
        const mid = (lo + hi) >> 1;
        if (astral[mid] < offset) lo = mid + 1;
        else hi = mid;
      }
      return offset + lo;
    };
  }

  /**
   * Converts a `doc.get` reply's page_map to UTF-16 indices and attaches the
   * mapper (`toUnit`) for converting finding offsets.
   * @param {{markdown: string, page_map: Array<[number, string]>}} doc
   * @returns {{markdown: string, page_map: Array<[number, string]>, toUnit: Function}}
   */
  function mapDocOffsets(doc) {
    const toUnit = codePointMapper(doc.markdown);
    return {
      markdown: doc.markdown,
      page_map: (doc.page_map || []).map(([off, label]) => [toUnit(off), label]),
      toUnit,
    };
  }

  // The converter escapes these in its markdown (e.g. "&c." -> "&amp;c.").
  // Stored text and offsets keep the escapes; they are decoded only for
  // display, after slicing, so offsets into the stored text stay valid.
  const NAMED = { amp: '&', lt: '<', gt: '>', quot: '"', apos: "'" };

  /** Decodes the escapes the converter writes; anything else is left as is. */
  function decodeEntities(text) {
    return text.replace(/&(amp|lt|gt|quot|apos|#\d{1,7}|#x[0-9a-fA-F]{1,6});/g, (whole, name) => {
      if (name[0] !== '#') return NAMED[name];
      const code = name[1] === 'x' ? parseInt(name.slice(2), 16) : parseInt(name.slice(1), 10);
      return code > 0 && code <= 0x10ffff ? String.fromCodePoint(code) : whole;
    });
  }

  return {
    decodeEntities,
    NOT_LOCATED,
    codePointMapper,
    mapDocOffsets,
    readerTarget,
    pageLabelAt,
    windowAround,
    extendBefore,
    extendAfter,
    buildBlocks,
    parseCitations,
    matchCitation,
  };
});
