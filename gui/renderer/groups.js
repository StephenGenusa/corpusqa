// Answers view: a saved run's hits grouped into distinct answers.
// Each answer expands to the hits that give it; clicking one opens the reader.

import { h, clear, fmtInt, fileStem, styledText } from './dom.js';

const { decodeEntities } = window.CorpusReader;

const plural = (n, word) => `${fmtInt(n)} ${word}${n === 1 ? '' : 's'}`;

export class GroupsView {
  /**
   * @param {HTMLElement} el container
   * @param {(row: object) => void} onOpen called with a finding row to read
   */
  constructor(el, onOpen) {
    this.el = el;
    this.onOpen = onOpen;
    this.selected = null;
    this.count = 0;
  }

  clear() {
    clear(this.el);
    this.selected = null;
    this.count = 0;
  }

  /**
   * Renders `run.grouping`. Rows carry `source` ('same' | 'changed' |
   * 'missing') so the reader can refuse a stale highlight.
   */
  render(run, sources) {
    this.clear();
    const grouping = run.grouping;
    if (!grouping) return;
    const files = new Map(run.files.map((f) => [f.rel_path, f]));
    const row = (ref) => {
      const f = files.get(ref.file)?.findings[ref.index];
      return f ? { ...f, rel_path: ref.file, source: sources[ref.file] || 'same' } : null;
    };
    const head = h('p', { class: 'results-summary', text:
      `${plural(grouping.groups.length, 'distinct answer')} · grouped by ${grouping.model}` });
    const list = h('ol', { class: 'groups-list', 'aria-label': 'Distinct answers' });
    grouping.groups.forEach((g, i) => {
      list.append(this._group(`${i + 1}.`, g.answer,
        `${plural(g.hits.length, 'hit')} in ${plural(g.volumes, 'file')}`,
        g.hits.map(row).filter(Boolean)));
    });
    if (grouping.ungrouped.length) {
      list.append(this._group('', 'Not grouped',
        `${plural(grouping.ungrouped.length, 'hit')} the model did not place`,
        grouping.ungrouped.map(row).filter(Boolean), 'group-ungrouped'));
    }
    this.count = grouping.groups.length;
    this.el.append(head, list);
  }

  _group(number, answer, meta, rows, extra = '') {
    const hits = h('ol', { class: 'results group-hits' });
    for (const r of rows) {
      r.el = h('li', {}, h('button', { type: 'button', class: 'hit', onclick: () => this._select(r) },
        h('div', { class: 'row-meta' },
          h('span', { class: 'row-file', title: r.rel_path, text: fileStem(r.rel_path) }),
          r.page_label ? h('span', { class: 'row-page', text: `· p. ${r.page_label}` }) : null,
          r.verified ? null : h('span', { class: 'badge badge-warn', text: 'unverified' })),
        h('blockquote', { class: 'row-quote' }, styledText(r.quote))));
      hits.append(r.el);
    }
    return h('li', { class: `group ${extra}`.trim() },
      h('details', {},
        h('summary', {},
          number ? h('span', { class: 'group-number', text: number }) : null,
          h('span', { class: 'group-answer', text: decodeEntities(answer) }),
          h('span', { class: 'group-meta', text: meta })),
        hits));
  }

  _select(r) {
    if (this.selected) this.selected.el.classList.remove('selected');
    this.selected = r;
    r.el.classList.add('selected');
    this.onOpen(r);
  }
}
