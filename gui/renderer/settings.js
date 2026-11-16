// Settings drawer: per-task and per-provider config (saved to corpusqa.yaml
// through config.save) and run options (remembered in localStorage).

import { h, clear, call, fmtInt, loadLocal, saveLocal } from './dom.js';

const TASKS = [
  ['extract', 'Reading documents'],
  ['synthesize', 'Writing the answer'],
  ['query_route', 'Routing'],
  ['catalog_summarize', 'Catalog summaries'],
];
const INPUT_SIZED = new Set(['extract', 'catalog_summarize']);

export const PROVIDER_KINDS = {
  ollama: { label: 'Ollama', api_base: 'http://localhost:11434', api_key_env: '' },
  lm_studio: { label: 'LM Studio', api_base: 'http://localhost:1234/v1', api_key_env: '' },
  openrouter: { label: 'OpenRouter', api_base: '', api_key_env: 'OPENROUTER_API_KEY' },
  openai_compatible: { label: 'OpenAI-compatible', api_base: '', api_key_env: '' },
};

const QUERY_KEY = 'corpusqa.queryOptions';
const QUERY_DEFAULTS = { relevance: 'recall', mode: 'auto', synthesize: false, use_cache: true };

function getPath(obj, path) {
  let node = obj;
  for (const key of path) {
    if (node === null || typeof node !== 'object') return null;
    node = node[key];
  }
  return node === undefined ? null : node;
}

function parseValue(raw, type, optional) {
  const v = raw.trim();
  if (v === '') return optional ? { value: null } : { error: 'Required' };
  if (type === 'int') return /^\d+$/.test(v) ? { value: Number(v) } : { error: 'Enter a whole number' };
  if (type === 'float') {
    const n = Number(v);
    return Number.isFinite(n) ? { value: n } : { error: 'Enter a number' };
  }
  return { value: v };
}

export class SettingsDrawer {
  /**
   * @param {HTMLElement} el the drawer element
   * @param {HTMLElement} toggle the ≡ button
   * @param {object} o
   * @param {(provider: string) => Promise<{models: object[], error: string|null}>} o.getModels
   * @param {(config: object) => void} o.onConfig called after a successful save
   */
  constructor(el, toggle, { getModels, onConfig }) {
    this.el = el;
    this.toggleBtn = toggle;
    this.getModels = getModels;
    this.onConfig = onConfig;
    this.config = null;
    this.fields = [];
    this.openKeys = new Set(['task:extract']);
    this.query = { ...QUERY_DEFAULTS, ...loadLocal(QUERY_KEY, {}) };
    toggle.addEventListener('click', () => this.toggle());
    el.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') this.close();
    });
    this._render();
  }

  get isOpen() {
    return this.el.classList.contains('open');
  }

  open() {
    this.el.classList.add('open');
    this.el.inert = false;
    this.toggleBtn.setAttribute('aria-expanded', 'true');
    this._updateWarnings();
    const first = this.el.querySelector('select, input, button');
    if (first) first.focus();
  }

  close() {
    this.el.classList.remove('open');
    this.el.inert = true;
    this.toggleBtn.setAttribute('aria-expanded', 'false');
    this.toggleBtn.focus();
  }

  toggle() {
    if (this.isOpen) this.close();
    else this.open();
  }

  /** Run options for `run.start` (relevance, mode, synthesize, use_cache). */
  queryOptions() {
    return { ...this.query };
  }

  setConfig(config) {
    this.config = config;
    this._render();
  }

  // ---- rendering -------------------------------------------------------

  _render() {
    const scrollTop = this.body ? this.body.scrollTop : 0;
    this.fields = [];
    this.byPath = new Map();
    clear(this.el);
    this.status = h('span', { class: 'drawer-status', role: 'status' });
    this.body = h('div', { class: 'drawer-body' }, this._runOptions());
    if (this.config) {
      this.body.append(this._tasksSection(), this._providersSection(), this._budgetSection());
    } else {
      this.body.append(h('p', { class: 'empty', text: 'The configuration could not be loaded.' }));
    }
    this.el.append(
      h('div', { class: 'drawer-head' },
        h('h2', { text: 'Settings' }),
        h('button', { type: 'button', class: 'icon-btn', 'aria-label': 'Close settings', text: '×', onclick: () => this.close() }),
      ),
      this.body,
      h('div', { class: 'drawer-foot' },
        h('button', {
          type: 'button',
          title: 'Show corpusqa.yaml and .env in the file manager',
          text: 'Open config folder',
          onclick: () => window.corpusqa.openConfigFolder(),
        }),
        this.status,
        h('button', { type: 'button', class: 'primary', text: 'Save', disabled: !this.config, onclick: () => this.save() }),
      ),
    );
    this.body.scrollTop = scrollTop;
  }

  _details(key, summary, ...children) {
    const d = h('details', { open: this.openKeys.has(key) }, h('summary', {}, summary), ...children);
    d.addEventListener('toggle', () => {
      if (d.open) this.openKeys.add(key);
      else this.openKeys.delete(key);
    });
    return d;
  }

  _runOptions() {
    const select = (key, label, options) => {
      const s = h('select', {
        onchange: (e) => this._setQuery(key, e.target.value),
      }, options.map(([v, t]) => h('option', { value: v, text: t, selected: this.query[key] === v })));
      return h('label', { class: 'field' }, h('span', { class: 'label', text: label }), s);
    };
    const check = (key, label) => h('label', { class: 'check' },
      h('input', { type: 'checkbox', checked: !!this.query[key], onchange: (e) => this._setQuery(key, e.target.checked) }),
      label);
    return h('section', { class: 'drawer-section' },
      h('h3', { text: 'Run options' }),
      h('p', { class: 'hint', text: 'Remembered on this computer.' }),
      h('div', { class: 'grid2' },
        select('relevance', 'Relevance', [['recall', 'Recall (keep anything related)'], ['balanced', 'Balanced'], ['strict', 'Strict']]),
        select('mode', 'Mode', [['auto', 'Auto'], ['sweep', 'Sweep (read every file)'], ['route', 'Route (choose files first)']]),
      ),
      check('synthesize', 'Write an answer from the findings (synthesize)'),
      check('use_cache', 'Reuse earlier readings of unchanged files (cache)'),
    );
  }

  _setQuery(key, value) {
    this.query[key] = value;
    saveLocal(QUERY_KEY, this.query);
  }

  _field(path, label, { type = 'text', optional = false, hint = null, placeholder = '' } = {}) {
    const current = getPath(this.config, path);
    const input = h('input', {
      type: 'text',
      inputmode: type === 'int' ? 'numeric' : type === 'float' ? 'decimal' : null,
      spellcheck: 'false',
      autocomplete: 'off',
      placeholder,
      value: current === null ? '' : String(current),
      dataset: { path: path.join('.') },
    });
    const err = h('span', { class: 'field-error', hidden: true });
    const field = { path, type, optional, input, err };
    this.fields.push(field);
    this.byPath.set(path.join('.'), field);
    return h('label', { class: 'field' },
      h('span', { class: 'label', text: label }),
      input,
      hint ? h('span', { class: 'hint', text: hint }) : null,
      err);
  }

  _selectField(path, label, options) {
    const current = getPath(this.config, path);
    const input = h('select', { dataset: { path: path.join('.') } },
      options.map((o) => h('option', { value: o, text: o, selected: o === current })));
    const err = h('span', { class: 'field-error', hidden: true });
    const field = { path, type: 'text', optional: false, input, err };
    this.fields.push(field);
    this.byPath.set(path.join('.'), field);
    return h('label', { class: 'field' }, h('span', { class: 'label', text: label }), input, err);
  }

  _tasksSection() {
    const providers = Object.keys(this.config.providers);
    const section = h('section', { class: 'drawer-section' },
      h('h3', { text: 'Tasks' }),
      h('p', { class: 'hint', text: 'Saved to corpusqa.yaml. The model and provider in the top bar apply to all four tasks.' }));
    this.warnings = new Map();
    for (const [task, title] of TASKS) {
      const p = ['tasks', task];
      const warn = h('p', { class: 'field-warn', hidden: true });
      this.warnings.set(task, warn);
      const cfg = this.config.tasks[task] || {};
      const body = h('div', { class: 'task-body' },
        h('div', { class: 'grid2' },
          this._field([...p, 'temperature'], 'Temperature', { type: 'float' }),
          this._field([...p, 'max_tokens'], 'Max output tokens', { type: 'int' }),
          this._field([...p, 'context_window'], 'Context window', { type: 'int' }),
          INPUT_SIZED.has(task)
            ? this._field([...p, 'max_input_tokens'], 'Max input tokens', {
              type: 'int', optional: true, placeholder: 'fill the window',
              hint: 'Section size per call; small models read more carefully with less.',
            })
            : null,
        ),
        warn,
        this._details(`adv:${task}`, 'Advanced',
          h('div', { class: 'grid2' },
            this._field([...p, 'model'], 'Model'),
            this._selectField([...p, 'provider'], 'Provider', providers))),
      );
      const summary = [h('span', { text: title }), h('span', { class: 'summary-sub', text: ` ${task} · ${cfg.model || ''}` })];
      section.append(this._details(`task:${task}`, summary, body));
    }
    section.addEventListener('input', () => this._updateWarnings());
    section.addEventListener('change', () => this._updateWarnings());
    return section;
  }

  _providersSection() {
    const section = h('section', { class: 'drawer-section' }, h('h3', { text: 'Providers' }));
    for (const [name, cfg] of Object.entries(this.config.providers)) {
      const p = ['providers', name];
      const kind = cfg.kind ? ` · ${PROVIDER_KINDS[cfg.kind]?.label || cfg.kind}` : '';
      section.append(this._details(`prov:${name}`, [h('span', { text: name }), h('span', { class: 'summary-sub', text: kind })],
        h('div', { class: 'grid2' },
          this._field([...p, 'max_concurrency'], 'Max concurrent calls', { type: 'int' }),
          this._field([...p, 'api_base'], 'API base URL', { optional: true, placeholder: 'provider default' }),
          this._field([...p, 'api_key_env'], 'API key variable', {
            optional: true, placeholder: 'none',
            hint: 'The name of an environment variable (or .env entry), never the key itself.',
          }),
        )));
    }
    section.append(this._addProvider());
    return section;
  }

  _addProvider() {
    const name = h('input', { type: 'text', spellcheck: 'false', autocomplete: 'off', placeholder: 'e.g. lm_studio_desk' });
    const kind = h('select', {}, Object.entries(PROVIDER_KINDS).map(([k, v]) => h('option', { value: k, text: v.label })));
    const base = h('input', { type: 'text', spellcheck: 'false', autocomplete: 'off' });
    const keyEnv = h('input', { type: 'text', spellcheck: 'false', autocomplete: 'off' });
    const err = h('p', { class: 'field-error', hidden: true });
    const prefill = () => {
      const k = PROVIDER_KINDS[kind.value];
      base.value = k.api_base;
      keyEnv.value = k.api_key_env;
      base.placeholder = kind.value === 'openrouter' ? 'provider default' : kind.value === 'openai_compatible' ? 'https://host/v1' : '';
    };
    kind.addEventListener('change', prefill);
    prefill();
    const add = async () => {
      err.hidden = true;
      const n = name.value.trim();
      let problem = null;
      if (!/^[A-Za-z0-9_.-]+$/.test(n)) problem = 'Name: letters, digits, _ . - only.';
      else if (this.config.providers[n]) problem = `A provider named ${n} already exists.`;
      if (problem) {
        err.textContent = problem;
        err.hidden = false;
        return;
      }
      const changes = [[['providers', n, 'kind'], kind.value]];
      if (base.value.trim()) changes.push([['providers', n, 'api_base'], base.value.trim()]);
      if (keyEnv.value.trim()) changes.push([['providers', n, 'api_key_env'], keyEnv.value.trim()]);
      try {
        const res = await call('config.save', { changes });
        this.openKeys.add(`prov:${n}`);
        this._applySaved(res.config, `Added provider ${n}.`);
      } catch (e) {
        const detail = Object.entries(e.fields || {}).map(([k, v]) => `${k}: ${v}`).join('; ');
        err.textContent = detail || e.message;
        err.hidden = false;
      }
    };
    return this._details('prov:+add', 'Add provider…',
      h('div', { class: 'grid2' },
        h('label', { class: 'field' }, h('span', { class: 'label', text: 'Name' }), name),
        h('label', { class: 'field' }, h('span', { class: 'label', text: 'Kind' }), kind),
        h('label', { class: 'field' }, h('span', { class: 'label', text: 'API base URL' }), base),
        h('label', { class: 'field' }, h('span', { class: 'label', text: 'API key variable' }), keyEnv)),
      err,
      h('button', { type: 'button', text: 'Add provider', onclick: add }));
  }

  _budgetSection() {
    return h('section', { class: 'drawer-section' },
      h('h3', { text: 'Budget' }),
      this._field(['budget', 'confirm_above_usd'], 'Ask before spending more than (USD)', { type: 'float' }));
  }

  // ---- LM Studio context warnings ---------------------------------------

  async _updateWarnings() {
    if (!this.config || !this.warnings) return;
    const seq = (this._warnSeq = (this._warnSeq || 0) + 1);
    for (const [task] of TASKS) {
      const warn = this.warnings.get(task);
      const model = this.byPath.get(`tasks.${task}.model`)?.input.value.trim();
      const provider = this.byPath.get(`tasks.${task}.provider`)?.input.value;
      const ctx = Number(this.byPath.get(`tasks.${task}.context_window`)?.input.value);
      let message = null;
      if (provider && model) {
        let listing;
        try {
          listing = await this.getModels(provider);
        } catch {
          listing = { models: [] };
        }
        if (seq !== this._warnSeq) return;
        const info = (listing.models || []).find((m) => m.id === model);
        if (info && info.loaded_context !== null && info.loaded_context !== undefined && ctx > info.loaded_context) {
          message = `LM Studio has this model loaded with ${fmtInt(info.loaded_context)} tokens of context, less than this window. ` +
            `Reload it in LM Studio with at least ${fmtInt(ctx)}, or lower the context window.`;
        } else if (info && info.loaded === false) {
          message = `This model is not loaded in LM Studio. Load it with at least ${fmtInt(ctx || 0)} tokens of context.`;
        }
      }
      warn.textContent = message || '';
      warn.hidden = !message;
    }
  }

  // ---- saving ------------------------------------------------------------

  _clearErrors() {
    for (const f of this.fields) {
      f.err.hidden = true;
      f.err.textContent = '';
      f.input.removeAttribute('aria-invalid');
    }
  }

  _showError(field, message) {
    field.err.textContent = message;
    field.err.hidden = false;
    field.input.setAttribute('aria-invalid', 'true');
  }

  _applySaved(config, message) {
    this.setConfig(config);
    this.onConfig(config);
    this.status.textContent = message;
    this.status.className = 'drawer-status ok';
  }

  /** Sends changed values to `config.save`; shows field errors inline. */
  async save() {
    this._clearErrors();
    const changes = [];
    let bad = 0;
    for (const f of this.fields) {
      const parsed = parseValue(f.input.value, f.type, f.optional);
      if (parsed.error) {
        this._showError(f, parsed.error);
        bad++;
        continue;
      }
      if (parsed.value !== getPath(this.config, f.path)) changes.push([f.path, parsed.value]);
    }
    this.status.className = 'drawer-status';
    if (bad) {
      this.status.textContent = 'Fix the marked fields; nothing was saved.';
      this.status.classList.add('bad');
      return false;
    }
    if (!changes.length) {
      this.status.textContent = 'No changes to save.';
      return true;
    }
    this.status.textContent = 'Saving…';
    try {
      const res = await call('config.save', { changes });
      this._applySaved(res.config, 'Saved to corpusqa.yaml.');
      return true;
    } catch (e) {
      const general = [];
      for (const [path, msg] of Object.entries(e.fields || {})) {
        const f = this.byPath.get(path);
        if (f) this._showError(f, msg);
        else general.push(path ? `${path}: ${msg}` : msg);
      }
      this.status.textContent = general.length || !Object.keys(e.fields || {}).length
        ? `Not saved: ${general.join('; ') || e.message}`
        : 'Not saved; see the marked fields.';
      this.status.classList.add('bad');
      return false;
    }
  }
}
