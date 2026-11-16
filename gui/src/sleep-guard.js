'use strict';

/**
 * Keeps the system from suspending while a run is in progress (a large
 * corpus can take hours on a local model).
 *
 * @param {object} blocker Electron's `powerSaveBlocker` (or a stand-in)
 * @returns {{start: () => void, stop: () => void, active: boolean}}
 */
function createSleepGuard(blocker) {
  let id = null;
  return {
    /** Starts blocking suspension; a second start does not stack. */
    start() {
      if (id !== null && blocker.isStarted(id)) return;
      id = blocker.start('prevent-app-suspension');
    },
    /** Releases the block; safe to call when not started. */
    stop() {
      if (id === null) return;
      if (blocker.isStarted(id)) blocker.stop(id);
      id = null;
    },
    get active() {
      return id !== null;
    },
  };
}

/**
 * Drives a guard from backend traffic: an accepted `run.start` holds it; the
 * run's `done` event or the backend exiting releases it.
 *
 * @param {{start: () => void, stop: () => void}} guard
 * @returns {{reply: (cmd: string, ok: boolean) => void, event: (ev: object) => void}}
 */
function trackRun(guard) {
  return {
    reply(cmd, ok) {
      if (cmd === 'run.start' && ok) guard.start();
    },
    event(ev) {
      if (ev && (ev.event === 'done' || ev.event === 'backend.exit')) guard.stop();
    },
  };
}

module.exports = { createSleepGuard, trackRun };
