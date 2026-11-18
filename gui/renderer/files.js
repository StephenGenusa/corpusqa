// Documents panel: choose which of the folder's documents a run reads.
// The choice is remembered per folder on this computer and saved with each
// run's results (only when it is a subset).

import { h, clear, call, fmtInt, fileStem, loadLocal, saveLocal } from './dom.js';

const { FileSelection } = window.CorpusFiles;
const STORE_KEY = 'corpusqa.selection'; // {folder: string[] | null}

const fmtSize = (n) => (n >= 1e6 ? `${(n / 1e6).toFixed(1)} MB` : `${Math.max(1, Math.round(n / 1e3))} KB`);

export class DocumentsPanel {
  /**
   * @param {{toggle: HTMLButtonElement, panel: HTMLElement}} els
   * @param {() => void} onChange called whenever the choice changes
   */
  constructor(els, onChange) {
    this.els = els;
    this.onChange = onChange;
    this.folder = '';
    this.selection = null; // FileSelection for this.folder, once listed
    this.error = null;
    this.token = 0;
    this._build();
    els.toggle.addEventListener('click', () => (this.isOpen ? this.close() : this.open()));
    document.addEventListener('mousedown', (e) => {
      if (this.isOpen && !els.panel.contains(e.target) && !els.toggle.contains(e.target)) this.close();
    });
    els.panel.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') {
        e.stopPropagation();
        this.close();
        els.toggle.focus();
      }
    });
    this._label();
  }

  get isOpen() { return !this.els.panel.hidden; }

  /** Paths for `run.start` (null: every document). */
  chosenList() { return this.selection ? this.selection.chosenList() : null; }

  /** True when the folder is listed and nothing is chosen. */
  get noneChosen() { return !!this.selection && this.selection.chosenCount === 0; }

  /** {chosen, total} for the chat context (chosen null when all). */
  counts() {
    if (!this.selection) return { chosen: null, total: null };
    return { chosen: this.selection.isAll ? null : this.selection.chosenCount, total: this.selection.total };
  }

  summary() { return this.selection ? this.selection.summary() : ''; }

  /**
   * Lists `folder` and applies its remembered choice (or `selection`, from a
   * saved run, which then becomes the remembered one).
   * @returns {Promise<string[]>} chosen paths no longer in the folder
   */
  async load(folder, selection) {
    const token = ++this.token;
    if (folder !== this.folder) this.selection = null; // else keep it until relisted
    this.folder = folder;
    this.error = null;
    this._label();
    if (!folder) return [];
    let files;
    try {
      ({ files } = await call('files.list', { folder }));
    } catch (e) {
      if (token === this.token) {
        this.error = e.message;
        this._label();
      }
      return [];
    }
    if (token !== this.token) return [];
    this.selection = new FileSelection(files);
    const fromRun = selection !== undefined;
    const missing = this.selection.restore(fromRun ? selection : this._remembered(folder));
    if (fromRun) this._remember();
    this._label();
    if (this.isOpen) this._renderList();
    this.onChange();
    return missing;
  }

  open() {
    this.els.panel.hidden = false;
    this.els.toggle.setAttribute('aria-expanded', 'true');
    if (this.folder) this.load(this.folder); // pick up files added since
    this._renderList();
    this.filter.focus();
  }

  close() {
    this.els.panel.hidden = true;
    this.els.toggle.setAttribute('aria-expanded', 'false');
  }

  _remembered(folder) {
    const saved = loadLocal(STORE_KEY, {});
    return saved && Array.isArray(saved[folder]) ? saved[folder] : null;
  }

  _remember() {
    const saved = loadLocal(STORE_KEY, {}) || {};
    saved[this.folder] = this.selection.chosenList();
    saveLocal(STORE_KEY, saved);
  }

  /** After a change; `relist` redraws the rows (a single tick need not). */
  _changed(relist = true) {
    this._remember();
    this._label();
    if (relist) this._renderList();
    else this._counts(this.selection.visible(this.filter.value));
    this.onChange();
  }

  _counts(shown) {
    const s = this.selection;
    this.count.textContent = `${fmtInt(s.chosenCount)} of ${fmtInt(s.total)} selected`;
    this.scope.textContent = shown.length !== s.total ? `applies to the ${fmtInt(shown.length)} shown` : '';
    this.allBtn.disabled = this.noneBtn.disabled = !shown.length;
  }

  _label() {
    const { toggle } = this.els;
    const s = this.selection;
    toggle.disabled = !this.folder;
    toggle.classList.toggle('subset', !!s && !s.isAll);
    toggle.textContent = `${s ? s.summary() : 'Documents'} ▾`;
    toggle.title = this.error ? `Cannot list the folder: ${this.error}` : 'Choose which documents to query';
  }

  _build() {
    const { panel } = this.els;
    this.filter = h('input', {
      type: 'text', class: 'docs-filter', spellcheck: 'false', autocomplete: 'off',
      placeholder: 'Filter by name, e.g. 1898', 'aria-label': 'Filter documents',
      oninput: () => this._renderList(),
    });
    this.count = h('span', { class: 'docs-count', 'aria-live': 'polite' });
    this.list = h('ul', { class: 'docs-list', 'aria-label': 'Documents' });
    this.allBtn = h('button', { type: 'button', onclick: () => this._bulk(true) }, 'Select all');
    this.noneBtn = h('button', { type: 'button', onclick: () => this._bulk(false) }, 'Select none');
    clear(panel);
    panel.append(
      h('div', { class: 'docs-head' },
        h('h2', { text: 'Documents to query' }), this.count),
      this.filter,
      h('div', { class: 'docs-actions' }, this.allBtn, this.noneBtn,
        this.scope = h('span', { class: 'hint' })),
      this.list,
      h('div', { class: 'docs-foot' },
        h('span', { class: 'hint', text: 'Remembered for this folder on this computer.' }),
        h('button', { type: 'button', class: 'primary', onclick: () => { this.close(); this.els.toggle.focus(); } }, 'Done')),
    );
  }

  _bulk(on) {
    if (!this.selection) return;
    const shown = this.selection.visible(this.filter.value);
    if (on) this.selection.selectAll(shown);
    else this.selection.selectNone(shown);
    this._changed();
  }

  _renderList() {
    if (!this.isOpen) return;
    clear(this.list);
    const s = this.selection;
    if (!s) {
      this.count.textContent = '';
      this.scope.textContent = '';
      this.allBtn.disabled = this.noneBtn.disabled = true;
      this.list.append(h('li', { class: 'empty', text: this.error ? `Cannot list the folder: ${this.error}` : this.folder ? 'Listing…' : 'Choose a folder first.' }));
      return;
    }
    const shown = s.visible(this.filter.value);
    this._counts(shown);
    if (!shown.length) {
      this.list.append(h('li', { class: 'empty', text: s.total ? 'No documents match the filter.' : 'No documents in this folder.' }));
      return;
    }
    for (const f of shown) {
      const dir = f.rel_path.includes('/') ? f.rel_path.slice(0, f.rel_path.lastIndexOf('/')) : '';
      this.list.append(h('li', {},
        h('label', { class: 'doc', title: f.rel_path },
          h('input', {
            type: 'checkbox', checked: s.isChosen(f.rel_path),
            onchange: () => { s.toggle(f.rel_path); this._changed(false); },
          }),
          h('span', { class: 'doc-name', text: fileStem(f.rel_path) }),
          dir ? h('span', { class: 'doc-dir', text: dir }) : null,
          f.converted ? null : h('span', { class: 'badge badge-muted', title: 'Converted to text on the next run that reads it', text: 'new' }),
          h('span', { class: 'doc-size', text: fmtSize(f.size) }))));
    }
  }
}
