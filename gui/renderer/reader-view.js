// Reader pane: shows a window of the converted text around a finding and
// grows it as the user scrolls, so multi-megabyte volumes stay responsive.

import { h, clear, call } from './dom.js';

const {
  readerTarget, pageLabelAt, windowAround, extendBefore, extendAfter, buildBlocks, mapDocOffsets,
} = window.CorpusReader;

const RADIUS = 20000;
const EDGE_PX = 2000;
const CACHE_DOCS = 8;

export class ReaderView {
  /**
   * @param {object} els DOM elements (head, file, page, notice, scroll, doc)
   * @param {() => string} getFolder the folder the current results came from
   */
  constructor(els, getFolder) {
    this.els = els;
    this.getFolder = getFolder;
    // `${folder}\0${rel}` -> {markdown, page_map, toUnit}; page_map already in
    // UTF-16 indices, toUnit maps the backend's code-point offsets.
    this.cache = new Map();
    this.view = null; // {text, pageMap, start, end, highlight}
    this.token = 0;
    this.extending = false;
    els.scroll.addEventListener('scroll', () => this._onScroll(), { passive: true });
  }

  clearCache() {
    this.cache.clear();
  }

  async _doc(folder, relPath) {
    const key = `${folder}\0${relPath}`;
    if (this.cache.has(key)) {
      const doc = this.cache.get(key);
      this.cache.delete(key); // refresh LRU position
      this.cache.set(key, doc);
      return doc;
    }
    const doc = mapDocOffsets(await call('doc.get', { folder, rel_path: relPath }));
    this.cache.set(key, doc);
    while (this.cache.size > CACHE_DOCS) this.cache.delete(this.cache.keys().next().value);
    return doc;
  }

  /** Opens `row` (a finding plus `rel_path`) in the reader. */
  async open(row) {
    const token = ++this.token;
    const { els } = this;
    els.head.hidden = false;
    els.file.textContent = row.rel_path;
    els.file.title = row.rel_path;
    els.page.textContent = row.page_label ? `p. ${row.page_label}` : '';
    els.notice.hidden = true;
    let doc;
    try {
      doc = await this._doc(this.getFolder(), row.rel_path);
    } catch (err) {
      if (token !== this.token) return;
      this.view = null;
      clear(els.doc);
      els.doc.append(h('p', { class: 'empty', text: `Cannot open ${row.rel_path}: ${err.message}` }));
      return;
    }
    if (token !== this.token) return;
    const text = doc.markdown;
    const pageMap = doc.page_map || [];
    // Finding offsets are Python code-point indices; convert to UTF-16.
    const target = readerTarget(row);
    if (target.offset !== null) target.offset = doc.toUnit(target.offset);
    if (target.notice) {
      els.notice.textContent = `${target.notice}. Showing the start of the document; the quote may be paraphrased.`;
      els.notice.hidden = false;
    }
    const highlight = target.offset === null
      ? null
      : [target.offset, doc.toUnit(row.end ?? row.start)];
    const { start, end } = windowAround(text, target.offset, RADIUS);
    this.view = { text, pageMap, start, end, highlight };
    if (!row.page_label && target.offset !== null) {
      const label = pageLabelAt(pageMap, target.offset);
      if (label) els.page.textContent = `p. ${label}`;
    }
    clear(els.doc);
    els.doc.append(this._render(start, end));
    if (!text.length) els.doc.append(h('p', { class: 'empty', text: 'This document has no text.' }));
    const marks = els.doc.querySelectorAll('mark');
    if (marks.length) {
      marks[0].scrollIntoView({ block: 'center' });
      for (const m of marks) m.classList.add('flash');
    } else {
      els.scroll.scrollTop = 0;
    }
    this._extend(); // fill the viewport if the window is short
  }

  _render(start, end) {
    const { text, pageMap, highlight } = this.view;
    const frag = document.createDocumentFragment();
    for (const block of buildBlocks(text, start, end, { pageMap, highlight })) {
      if (block.type === 'page') {
        frag.append(h('div', { class: 'page-mark', 'aria-label': `page ${block.label}`, text: `— p. ${block.label} —` }));
        continue;
      }
      const tag = block.type === 'heading' ? `h${Math.min(block.level + 1, 6)}` : block.type === 'pre' ? 'pre' : 'p';
      const el = h(tag, { class: block.type === 'heading' ? 'doc-heading' : null });
      for (const run of block.runs) {
        if (run.kind === 'text') el.append(run.text);
        else if (run.kind === 'hl') el.append(h('mark', { class: 'quote-hl', text: run.text }));
        else el.append(h('span', { class: 'page-inline', 'aria-label': `page ${run.label}`, text: ` — p. ${run.label} — ` }));
      }
      frag.append(el);
    }
    return frag;
  }

  _onScroll() {
    if (!this.view || this.extending) return;
    this.extending = true;
    requestAnimationFrame(() => {
      try {
        this._extend();
      } finally {
        this.extending = false;
      }
    });
  }

  _extend() {
    const { scroll, doc } = this.els;
    const v = this.view;
    if (!v) return;
    if (scroll.scrollTop < EDGE_PX && v.start > 0) {
      const newStart = extendBefore(v.text, v.start, RADIUS);
      const frag = this._render(newStart, v.start);
      const before = scroll.scrollHeight;
      doc.prepend(frag);
      scroll.scrollTop += scroll.scrollHeight - before;
      v.start = newStart;
    }
    if (scroll.scrollHeight - scroll.scrollTop - scroll.clientHeight < EDGE_PX && v.end < v.text.length) {
      const newEnd = extendAfter(v.text, v.end, RADIUS);
      doc.append(this._render(v.end, newEnd));
      v.end = newEnd;
    }
  }
}
