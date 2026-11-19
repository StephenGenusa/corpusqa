// Results list: findings per file, the Problems section and the answer.

import { h, clear, fmtInt, fileStem, styledText } from './dom.js';

const { parseCitations, citationTargets, decodeEntities } = window.CorpusReader;

const byFile = (a, b) => a.localeCompare(b, undefined, { numeric: true, sensitivity: 'base' });

export class ResultsView {
  /**
   * @param {object} els DOM elements (list, summary, empty, problems, problemList,
   *   problemCount, answer)
   * @param {(row: object) => void} onOpen called when a row is selected
   */
  constructor(els, onOpen) {
    this.els = els;
    this.onOpen = onOpen;
    this.els.list.addEventListener('keydown', (e) => this._onKey(e));
    this.reset();
  }

  reset() {
    this.rows = [];
    this.groups = new Map(); // rel_path -> row[]
    this.filesSeen = 0;
    this.filesRelevant = 0;
    this.flags = [];
    this.failures = [];
    this.selected = null;
    this.answerText = null;
    this.answerCitations = null;
    clear(this.els.list);
    clear(this.els.answer);
    this.els.answer.hidden = true;
    this._renderProblems();
    this._renderSummary();
  }

  /** Adds one `file_result` event's findings, keeping files in name order. */
  addFileResult(ev) {
    this.filesSeen += 1;
    if (ev.relevant && ev.findings.length) this.filesRelevant += 1;
    if (this.groups.has(ev.rel_path)) this._removeGroup(ev.rel_path);
    const findings = ev.findings
      .map((f, i) => ({ f, i }))
      .sort((a, b) => (a.f.start ?? Infinity) - (b.f.start ?? Infinity) || a.i - b.i)
      // `index`: the finding's place in the file's list, which answer
      // citations refer to.
      .map(({ f, i }) => ({ ...f, rel_path: ev.rel_path, index: i }));
    if (findings.length) {
      const next = [...this.groups.keys()].filter((k) => byFile(k, ev.rel_path) > 0).sort(byFile)[0];
      const before = next ? this.groups.get(next)[0].el : null;
      for (const row of findings) {
        row.el = this._rowElement(row);
        this.els.list.insertBefore(row.el, before);
      }
      this.groups.set(ev.rel_path, findings);
      this.rows = [...this.groups.keys()].sort(byFile).flatMap((k) => this.groups.get(k));
    }
    this._renderSummary();
    if (this.answerText) this.setAnswer(this.answerText, this.answerCitations);
  }

  _removeGroup(relPath) {
    for (const row of this.groups.get(relPath)) row.el.remove();
    this.groups.delete(relPath);
    this.rows = this.rows.filter((r) => r.rel_path !== relPath);
  }

  _rowElement(row) {
    const meta = h('div', { class: 'row-meta' },
      h('span', { class: 'row-file', title: row.rel_path, text: fileStem(row.rel_path) }),
      row.page_label ? h('span', { class: 'row-page', text: `· p. ${row.page_label}` }) : null,
      row.verified
        ? h('span', { class: 'badge badge-ok', title: 'Quote found verbatim in the source', text: '✓' })
        : h('span', { class: 'badge badge-warn', title: 'Quote not located in the source', text: 'unverified' }),
    );
    const button = h('button', { type: 'button', class: 'hit', onclick: () => this.select(row) },
      meta,
      h('blockquote', { class: 'row-quote' }, styledText(row.quote)),
      row.claim ? h('p', { class: 'row-why' }, h('span', { class: 'why', text: 'why: ' }), decodeEntities(row.claim)) : null,
    );
    return h('li', {}, button);
  }

  select(row) {
    if (this.selected) this.selected.el.classList.remove('selected');
    this.selected = row;
    row.el.classList.add('selected');
    row.el.scrollIntoView({ block: 'nearest' });
    this.onOpen(row);
  }

  _onKey(e) {
    if (e.key !== 'ArrowDown' && e.key !== 'ArrowUp') return;
    if (!this.rows.length) return;
    e.preventDefault();
    const idx = this.selected ? this.rows.indexOf(this.selected) : -1;
    const next = Math.min(this.rows.length - 1, Math.max(0, idx + (e.key === 'ArrowDown' ? 1 : -1)));
    this.select(this.rows[next]);
    this.rows[next].el.querySelector('button').focus();
  }

  setFlags(items) {
    this.flags = items;
    this._renderProblems();
  }

  setFailures(failures) {
    this.failures = failures || [];
    this._renderProblems();
  }

  _renderProblems() {
    const { problems, problemList, problemCount } = this.els;
    clear(problemList);
    for (const item of this.flags) {
      problemList.append(h('li', {},
        h('span', { class: 'problem-file', text: item.rel_path }),
        h('span', { class: 'problem-kind', text: ` — conversion: ${item.status.replace(/_/g, ' ')}` }),
        item.error ? h('div', { class: 'problem-detail', text: item.error }) : null,
      ));
    }
    for (const [relPath, message] of this.failures) {
      problemList.append(h('li', {},
        h('span', { class: 'problem-file', text: relPath }),
        h('span', { class: 'problem-kind', text: ' — reading failed' }),
        h('div', { class: 'problem-detail', text: message }),
      ));
    }
    const n = this.flags.length + this.failures.length;
    problemCount.textContent = `(${n})`;
    problems.hidden = n === 0;
  }

  _renderSummary() {
    const n = this.rows.length;
    const unverified = this.rows.filter((r) => !r.verified).length;
    this.els.empty.hidden = this.filesSeen > 0;
    if (!this.filesSeen) {
      this.els.summary.textContent = '';
      return;
    }
    let text = `${fmtInt(n)} finding${n === 1 ? '' : 's'} in ${fmtInt(this.filesRelevant)} of ${fmtInt(this.filesSeen)} files read`;
    if (unverified) text += ` · ${fmtInt(unverified)} unverified`;
    this.els.summary.textContent = text;
  }

  /**
   * Shows the synthesized answer; each citation selects the finding it cites.
   * @param {string|null} text
   * @param {Array<{rel_path: string, index: number}>|null} [citations] the
   *   finding behind each citation, in order (absent in older results)
   */
  setAnswer(text, citations = null) {
    this.answerText = text;
    this.answerCitations = citations;
    const { answer } = this.els;
    clear(answer);
    if (!text) {
      answer.hidden = true;
      return;
    }
    answer.append(h('h2', { class: 'answer-title', text: 'Answer' }));
    const paths = [...this.groups.keys()];
    const paras = text.split(/\n{2,}/).map((para) => parseCitations(para, paths));
    const cites = paras.flat().filter((seg) => seg.kind === 'cite');
    const targets = citationTargets(cites, citations, this.rows);
    let k = 0;
    for (const segs of paras) {
      const p = h('p');
      for (const seg of segs) {
        if (seg.kind === 'text') {
          p.append(decodeEntities(seg.text));
          continue;
        }
        const idx = targets[k++];
        p.append(h('button', {
          type: 'button',
          class: seg.unverified ? 'cite cite-unverified' : 'cite',
          disabled: idx === -1,
          title: idx === -1 ? 'No matching finding' : 'Show this finding',
          text: seg.text,
          onclick: () => this.select(this.rows[idx]),
        }));
      }
      answer.append(p);
    }
    answer.hidden = false;
  }
}
