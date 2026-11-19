/* Builds the top-bar model dropdown's options from a provider's model list.
 *
 * Loaded by Node (tests) via require() and by the renderer as a classic
 * script, where it defines `window.CorpusModelOptions`.
 */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.CorpusModelOptions = factory();
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';

  /** Value of the option that reveals the free-text model field. */
  const OTHER = '__other__';

  const fmtInt = (n) => Number(n).toLocaleString('en-US');

  /** Short status shown after a model's name, or ''. */
  function modelNote(m) {
    if (m.loaded === true) return m.loaded_context ? `loaded · ${fmtInt(m.loaded_context)} ctx` : 'loaded';
    if (m.loaded === false) return 'not loaded';
    if (m.max_context) return `${fmtInt(m.max_context)} ctx`;
    return '';
  }

  /**
   * Options for the model <select>.
   *
   * Loaded models come first, then the rest by name. The configured model is
   * selected; when the provider does not list it, it is kept as its own
   * option flagged "not in this provider's list" so the user sees what is
   * set rather than a silently different choice. With nothing configured, a
   * disabled "Choose a model…" placeholder is selected. The last option
   * always lets the user type any LiteLLM model string.
   *
   * @param {{models: object[]}} listing Result of models.list.
   * @param {string} wanted The configured model id ('' for none).
   * @returns {{options: {value: string, text: string, selected: boolean, disabled: boolean}[],
   *            listed: boolean}} `listed` is false when `wanted` is set but not listed.
   */
  function modelOptions(listing, wanted) {
    const models = [...((listing && listing.models) || [])].sort((a, b) => {
      const la = a.loaded === true ? 0 : 1;
      const lb = b.loaded === true ? 0 : 1;
      return la - lb || a.id.localeCompare(b.id);
    });
    const listed = !wanted || models.some((m) => m.id === wanted);
    const options = [];
    if (!wanted) {
      options.push({ value: '', text: 'Choose a model…', selected: true, disabled: true });
    } else if (!listed) {
      options.push({
        value: wanted, text: `${wanted} — not in this provider's list`, selected: true, disabled: false,
      });
    }
    for (const m of models) {
      const note = modelNote(m);
      options.push({
        value: m.id, text: note ? `${m.id} — ${note}` : m.id, selected: m.id === wanted, disabled: false,
      });
    }
    options.push({ value: OTHER, text: 'Other — type a model name…', selected: false, disabled: false });
    return { options, listed };
  }

  /**
   * Config changes that move every task to the top bar's provider and model.
   *
   * A task that changes model also gets a context window that fits the new
   * model: its old one belonged to the old model (the example config gives
   * hosted models 200,000 or 1,000,000 tokens, far more than a local model
   * holds). The window is the one LM Studio has the model loaded with when
   * it says so; otherwise the extract task's (the window users tune), capped
   * at the model's maximum when the provider reports one. Tasks already on
   * this model are left alone.
   *
   * @param {Object<string, {provider: string, model: string, context_window: number}>} tasks
   * @param {string[]} names task names, in order
   * @param {string} provider
   * @param {string} model
   * @param {{models?: object[]}} [listing] the provider's models.list result
   * @returns {[string[], *][]} [keyPath, value] pairs for config.save
   */
  function selectionChanges(tasks, names, provider, model, listing) {
    const info = ((listing && listing.models) || []).find((m) => m.id === model);
    let window = tasks.extract.context_window;
    if (info && info.loaded_context) window = info.loaded_context;
    else if (info && info.max_context) window = Math.min(window, info.max_context);
    const changes = [];
    for (const t of names) {
      const cfg = tasks[t];
      if (cfg.model === model && cfg.provider === provider) continue;
      if (cfg.model !== model) changes.push([['tasks', t, 'model'], model]);
      if (cfg.provider !== provider) changes.push([['tasks', t, 'provider'], provider]);
      if (cfg.context_window !== window) changes.push([['tasks', t, 'context_window'], window]);
    }
    return changes;
  }

  return { OTHER, modelNote, modelOptions, selectionChanges };
});
