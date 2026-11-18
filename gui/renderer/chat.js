// Wording help: a one-off chat with a model that knows how corpusqa reads
// documents, for phrasing the Question and Definition. Nothing is saved; the
// conversation lives in this window until "New conversation" or app exit.

import { h, clear, call, loadLocal, saveLocal, styledText } from './dom.js';

const { parseReply } = window.CorpusChat;
const { OTHER, modelOptions } = window.CorpusModelOptions;
const MODEL_KEY = 'corpusqa.chatModel'; // {provider, model}

/** A reply's text as written in Markdown: `code`, **bold** and *italic*. */
function richText(text) {
  return text.split(/(`[^`\n]+`)/).flatMap((part) => (
    /^`[^`]+`$/.test(part) ? [h('code', { text: part.slice(1, -1) })] : styledText(part)));
}

export class ChatPanel {
  /**
   * @param {HTMLElement} el the panel (an <aside>)
   * @param {HTMLButtonElement} toggle the button that opens it
   * @param {object} deps
   * @param {(provider: string) => Promise<object>} deps.getModels
   * @param {() => object|null} deps.getConfig
   * @param {() => object} deps.getContext the main window's fields
   * @param {(kind: 'question'|'definition', text: string) => void} deps.onUse
   */
  constructor(el, toggle, deps) {
    this.el = el;
    this.toggle = toggle;
    this.deps = deps;
    this.messages = [];
    this.busy = false;
    this._build();
    toggle.addEventListener('click', () => (this.isOpen ? this.close() : this.open()));
    el.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') {
        e.stopPropagation();
        this.close();
        toggle.focus();
      }
    });
  }

  get isOpen() { return this.el.classList.contains('open'); }

  open() {
    this.el.classList.add('open');
    this.el.inert = false;
    this.toggle.setAttribute('aria-expanded', 'true');
    this._fillProviders();
    this._renderMessages();
    this.input.focus();
  }

  close() {
    this.el.classList.remove('open');
    this.el.inert = true;
    this.toggle.setAttribute('aria-expanded', 'false');
  }

  /** The chat model: the remembered choice, else the synthesize task's. */
  _chosen() {
    const saved = loadLocal(MODEL_KEY, null);
    const config = this.deps.getConfig();
    if (saved && saved.provider && config && config.providers[saved.provider]) return saved;
    const synth = config ? config.tasks.synthesize : null;
    return synth ? { provider: synth.provider, model: synth.model } : { provider: '', model: '' };
  }

  _model() {
    return this.modelSel.value === OTHER ? this.modelCustom.value.trim() : this.modelSel.value;
  }

  _build() {
    this.providerSel = h('select', { 'aria-label': 'Chat provider', onchange: () => this._fillModels('') });
    this.modelSel = h('select', {
      'aria-label': 'Chat model',
      onchange: () => {
        this.modelCustom.hidden = this.modelSel.value !== OTHER;
        if (this.modelSel.value === OTHER) this.modelCustom.focus();
        else this._rememberModel();
      },
    });
    this.modelCustom = h('input', {
      type: 'text', class: 'model-custom', hidden: true, spellcheck: 'false', autocomplete: 'off',
      'aria-label': 'Chat model name', placeholder: 'LiteLLM model, e.g. lm_studio/qwen3-32b',
      onchange: () => this._rememberModel(),
    });
    this.log = h('div', { class: 'chat-log', 'aria-live': 'polite' });
    this.input = h('textarea', {
      class: 'chat-input', rows: '3', placeholder: 'Ask how to phrase your question… (Enter sends, Shift+Enter for a new line)',
      'aria-label': 'Message',
      onkeydown: (e) => {
        if (e.key === 'Enter' && !e.shiftKey) {
          e.preventDefault();
          this.send();
        }
      },
    });
    this.sendBtn = h('button', { type: 'button', class: 'primary', onclick: () => this.send() }, 'Send');
    this.el.inert = true;
    clear(this.el);
    this.el.append(
      h('div', { class: 'drawer-head' },
        h('h2', { text: 'Wording help' }),
        h('button', { type: 'button', class: 'icon-btn', 'aria-label': 'Close wording help', onclick: () => this.close() }, '×')),
      h('div', { class: 'chat-models' },
        h('label', { class: 'field' }, h('span', { class: 'label', text: 'Provider' }), this.providerSel),
        h('label', { class: 'field grow' }, h('span', { class: 'label', text: 'Chat model' }), this.modelSel),
        this.modelCustom),
      this.log,
      h('div', { class: 'chat-compose' }, this.input,
        h('div', { class: 'chat-actions' },
          h('button', { type: 'button', onclick: () => this.reset() }, 'New conversation'),
          h('span', { class: 'hint grow', text: 'Not saved. Sees your current question and settings.' }),
          this.sendBtn)),
    );
  }

  _fillProviders() {
    const config = this.deps.getConfig();
    if (!config) return;
    const { provider, model } = this._chosen();
    clear(this.providerSel);
    for (const name of Object.keys(config.providers)) {
      this.providerSel.append(h('option', { value: name, selected: name === provider }, name));
    }
    this._fillModels(model);
  }

  async _fillModels(wanted) {
    const provider = this.providerSel.value;
    clear(this.modelSel);
    this.modelSel.append(h('option', { value: wanted, text: wanted || 'Loading…' }));
    const listing = await this.deps.getModels(provider);
    if (provider !== this.providerSel.value) return;
    const { options } = modelOptions(listing, wanted);
    clear(this.modelSel);
    for (const o of options) {
      this.modelSel.append(h('option', { value: o.value, selected: o.selected, disabled: o.disabled }, o.text));
    }
    this.modelCustom.hidden = true;
  }

  _rememberModel() {
    const model = this._model();
    if (model) saveLocal(MODEL_KEY, { provider: this.providerSel.value, model });
  }

  reset() {
    if (this.busy) return;
    this.messages = [];
    this._renderMessages();
    this.input.focus();
  }

  async send() {
    const text = this.input.value.trim();
    if (!text || this.busy) return;
    const model = this._model();
    if (!model) {
      this._renderMessages('Choose a chat model first.');
      return;
    }
    this.messages.push({ role: 'user', content: text });
    this.input.value = '';
    this._setBusy(true);
    this._renderMessages();
    let error;
    try {
      const reply = await call('chat.send', {
        provider: this.providerSel.value,
        model,
        messages: this.messages.map(({ role, content }) => ({ role, content })),
        context: this.deps.getContext(),
      });
      this.messages.push({ role: 'assistant', content: reply.text, truncated: reply.truncated });
    } catch (e) {
      // Take the message back so it can be sent again.
      this.messages.pop();
      this.input.value = text;
      error = `Could not get a reply: ${e.message}`;
    }
    this._setBusy(false);
    this._renderMessages(error);
    this.input.focus();
  }

  _setBusy(busy) {
    this.busy = busy;
    this.sendBtn.disabled = busy;
    this.el.dataset.busy = busy ? '1' : '';
  }

  _renderMessages(error) {
    clear(this.log);
    if (!this.messages.length && !error) {
      this.log.append(h('p', { class: 'empty chat-intro', text:
        'Describe what you want to find, or paste a question. The assistant knows how corpusqa reads '
        + 'the documents and suggests a Question and Definition you can use with one click.' }));
    }
    for (const m of this.messages) {
      if (m.role === 'user') {
        this.log.append(h('div', { class: 'msg msg-user' }, m.content));
        continue;
      }
      const msg = h('div', { class: 'msg msg-assistant' });
      const parts = parseReply(m.content);
      if (!parts.length) msg.append(h('p', { class: 'hint', text: '(empty reply)' }));
      for (const part of parts) {
        if (part.kind === 'text') {
          msg.append(h('p', {}, richText(part.text)));
          continue;
        }
        const label = part.kind === 'question' ? 'Question' : 'Definition';
        msg.append(h('div', { class: 'suggestion' },
          h('div', { class: 'suggestion-head' },
            h('span', { class: 'label', text: `Suggested ${label.toLowerCase()}` }),
            h('button', { type: 'button', class: 'use', onclick: (e) => {
              this.deps.onUse(part.kind, part.text);
              e.currentTarget.textContent = 'Used ✓';
            } }, `Use as ${label}`)),
          h('p', {}, richText(part.text))));
      }
      if (m.truncated) msg.append(h('p', { class: 'hint', text: 'The reply was cut off at its length limit.' }));
      this.log.append(msg);
    }
    if (this.busy) this.log.append(h('div', { class: 'msg msg-assistant msg-pending', text: 'Thinking…' }));
    if (error) this.log.append(h('p', { class: 'field-error', text: error }));
    this.log.scrollTop = this.log.scrollHeight;
  }
}
