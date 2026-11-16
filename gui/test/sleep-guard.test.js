'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { createSleepGuard, trackRun } = require('../src/sleep-guard');

function fakeBlocker() {
  const started = new Set();
  let next = 1;
  return {
    started,
    calls: [],
    start(type) {
      this.calls.push(['start', type]);
      const id = next++;
      started.add(id);
      return id;
    },
    stop(id) {
      this.calls.push(['stop', id]);
      started.delete(id);
    },
    isStarted(id) {
      return started.has(id);
    },
  };
}

test('guard prevents app suspension once and releases it', () => {
  const b = fakeBlocker();
  const guard = createSleepGuard(b);
  guard.start();
  guard.start(); // a second start does not stack blockers
  assert.deepEqual(b.calls, [['start', 'prevent-app-suspension']]);
  assert.equal(guard.active, true);
  guard.stop();
  guard.stop(); // idempotent
  assert.equal(b.started.size, 0);
  assert.equal(guard.active, false);
  assert.equal(b.calls.filter(([c]) => c === 'stop').length, 1);
});

test('a started run holds the guard until done or backend exit', () => {
  const b = fakeBlocker();
  const guard = createSleepGuard(b);
  const track = trackRun(guard);
  track.reply('config.get', true);
  assert.equal(guard.active, false);
  track.reply('run.start', false); // rejected start: nothing to hold
  assert.equal(guard.active, false);
  track.reply('run.start', true);
  assert.equal(guard.active, true);
  track.event({ event: 'progress', phase: 'reading', done: 1, total: 2 });
  assert.equal(guard.active, true);
  track.event({ event: 'done', failures: [] });
  assert.equal(guard.active, false);
  track.reply('run.start', true);
  track.event({ event: 'backend.exit', code: 1 });
  assert.equal(guard.active, false);
});
