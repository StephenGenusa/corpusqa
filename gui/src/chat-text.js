/* Splits a wording-help reply into text and suggested Question/Definition.
 *
 * Loaded by Node (tests) via require() and by the renderer as a classic
 * script, where it defines `window.CorpusChat`.
 */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.CorpusChat = factory();
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';

  const SUGGESTION = /<(question|definition)>([\s\S]*?)<\/\1>/gi;

  /**
   * @param {string} reply the model's text
   * @returns {{kind: 'text'|'question'|'definition', text: string}[]}
   */
  function parseReply(reply) {
    // Reasoning models may emit <think>…</think>; an unclosed one runs to the end.
    const text = reply.replace(/<think>[\s\S]*?(<\/think>|$)/gi, '');
    const out = [];
    const pushText = (t) => {
      const trimmed = t.trim();
      if (trimmed) out.push({ kind: 'text', text: trimmed });
    };
    let last = 0;
    for (const m of text.matchAll(SUGGESTION)) {
      pushText(text.slice(last, m.index));
      const body = m[2].trim();
      if (body) out.push({ kind: m[1].toLowerCase(), text: body });
      last = m.index + m[0].length;
    }
    pushText(text.slice(last));
    return out;
  }

  return { parseReply };
});
