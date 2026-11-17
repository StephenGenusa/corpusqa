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

  return { OTHER, modelNote, modelOptions };
});
