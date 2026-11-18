/* Which documents of a folder a run reads: pure logic for the Documents panel.
 *
 * Loaded by Node (tests) via require() and by the renderer as a classic
 * script, where it defines `window.CorpusFiles`.
 */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.CorpusFiles = factory();
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';

  /** True when every word of `filter` occurs in `relPath` (case-insensitive). */
  function matchesFilter(relPath, filter) {
    const hay = relPath.toLowerCase();
    return filter.toLowerCase().split(/\s+/).filter(Boolean).every((w) => hay.includes(w));
  }

  const plural = (n) => `${n.toLocaleString('en-US')} document${n === 1 ? '' : 's'}`;

  class FileSelection {
    /** @param {{rel_path: string}[]} files the folder's documents; all chosen */
    constructor(files) {
      this.files = files;
      this.chosen = new Set(files.map((f) => f.rel_path));
    }

    get total() { return this.files.length; }
    get chosenCount() { return this.chosen.size; }
    get isAll() { return this.chosen.size === this.files.length; }

    isChosen(relPath) { return this.chosen.has(relPath); }

    /** The files whose path matches `filter`, in folder order. */
    visible(filter) {
      return this.files.filter((f) => matchesFilter(f.rel_path, filter || ''));
    }

    selectAll(files) { for (const f of files) this.chosen.add(f.rel_path); }
    selectNone(files) { for (const f of files) this.chosen.delete(f.rel_path); }

    toggle(relPath) {
      if (this.chosen.has(relPath)) this.chosen.delete(relPath);
      else this.chosen.add(relPath);
    }

    /** Paths to send with a run: null when every document is chosen. */
    chosenList() {
      return this.isAll ? null : [...this.chosen].sort();
    }

    /**
     * Applies a saved choice (null = all). Returns the saved paths that are
     * not in the folder any more.
     */
    restore(saved) {
      if (saved == null) {
        this.chosen = new Set(this.files.map((f) => f.rel_path));
        return [];
      }
      const present = new Set(this.files.map((f) => f.rel_path));
      this.chosen = new Set(saved.filter((p) => present.has(p)));
      return saved.filter((p) => !present.has(p));
    }

    summary() {
      if (!this.chosen.size) return 'No documents';
      return this.isAll ? `All ${plural(this.total)}` : `${this.chosen.size.toLocaleString('en-US')} of ${plural(this.total)}`;
    }
  }

  return { FileSelection, matchesFilter };
});
