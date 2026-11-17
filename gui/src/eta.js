/* Estimated time remaining for a run phase, smoothed over recent responses.
 *
 * Loaded by Node (tests) via require() and by the renderer as a classic
 * script, where it defines `window.CorpusEta`.
 */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.CorpusEta = factory();
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';

  /**
   * Time per response is the average over the last `windowSize` responses
   * (fewer early in a phase: all responses so far), so one slow or fast
   * response barely moves the estimate. Each progress event that advances
   * `done` is one response; a jump (a failed file crediting its unread
   * sections) is one response whose work is all credited.
   */
  class EtaEstimator {
    /** @param {number} windowSize Responses to average over. */
    constructor(windowSize = 50) {
      this.windowSize = windowSize;
      this.phase = null;
      this.reset(null);
    }

    reset(phase) {
      this.phase = phase;
      this.times = []; // [phase start or earlier anchor, ...response times]
      this.done = 0;
      this.total = 0;
    }

    /**
     * Records a progress event.
     * @param {string} phase e.g. 'converting' or 'reading'.
     * @param {number} done Units finished so far in this phase.
     * @param {number} total Units in this phase.
     * @param {number} now Timestamp in ms.
     */
    record(phase, done, total, now) {
      if (phase !== this.phase) this.reset(phase);
      this.total = total;
      if (!this.times.length) {
        // First event of the phase (normally 0/total): the timing anchor.
        this.times.push(now);
        this.done = done;
        return;
      }
      if (done <= this.done) return;
      this.done = done;
      this.times.push(now);
      // Keep the window's responses plus the one before them as the anchor.
      if (this.times.length > this.windowSize + 1) this.times.shift();
    }

    /**
     * Estimated ms left in the phase, or null before the first response.
     * Counts down from the last response's time, never below zero.
     * @param {number} now Timestamp in ms.
     */
    remainingMs(now) {
      const responses = this.times.length - 1;
      if (responses < 1) return null;
      const left = Math.max(0, this.total - this.done);
      if (left === 0) return 0;
      const last = this.times[this.times.length - 1];
      const perResponse = (last - this.times[0]) / responses;
      return Math.max(0, left * perResponse - (now - last));
    }
  }

  /** `4 h 05 min`, `12 min`, `under a minute`. */
  function fmtRemaining(ms) {
    if (ms < 60000) return 'under a minute';
    const min = Math.ceil(ms / 60000);
    const h = Math.floor(min / 60);
    const m = min % 60;
    return h > 0 ? `${h} h ${String(m).padStart(2, '0')} min` : `${m} min`;
  }

  return { EtaEstimator, fmtRemaining };
});
