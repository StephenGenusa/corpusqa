'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { EtaEstimator, fmtRemaining } = require('../src/eta.js');

const SEC = 1000;

test('no estimate before the first response', () => {
  const eta = new EtaEstimator();
  eta.record('reading', 0, 100, 0);
  assert.equal(eta.remainingMs(5 * SEC), null);
});

test('fewer than 50 responses: rate from all responses since the phase began', () => {
  const eta = new EtaEstimator();
  eta.record('reading', 0, 100, 0);
  // 10 responses, one every 2 s.
  for (let i = 1; i <= 10; i++) eta.record('reading', i, 100, i * 2 * SEC);
  // 90 left at 2 s each, measured from the last response.
  assert.equal(eta.remainingMs(20 * SEC), 180 * SEC);
});

test('counts down between responses and never goes negative', () => {
  const eta = new EtaEstimator();
  eta.record('reading', 0, 3, 0);
  eta.record('reading', 1, 3, 10 * SEC); // 2 left at 10 s each = 20 s
  assert.equal(eta.remainingMs(15 * SEC), 15 * SEC);
  assert.equal(eta.remainingMs(60 * SEC), 0);
});

test('uses only the last 50 responses once more have arrived', () => {
  const eta = new EtaEstimator();
  eta.record('reading', 0, 1000, 0);
  let t = 0;
  // 100 slow responses (10 s each), then 50 fast ones (1 s each).
  for (let i = 1; i <= 100; i++) eta.record('reading', i, 1000, (t += 10 * SEC));
  for (let i = 101; i <= 150; i++) eta.record('reading', i, 1000, (t += 1 * SEC));
  // The window holds only the 50 fast responses: 850 left at 1 s each.
  assert.equal(eta.remainingMs(t), 850 * SEC);
});

test('window size is configurable', () => {
  const eta = new EtaEstimator(2);
  eta.record('reading', 0, 10, 0);
  eta.record('reading', 1, 10, 100 * SEC);
  eta.record('reading', 2, 10, 101 * SEC);
  eta.record('reading', 3, 10, 102 * SEC);
  // Last 2 responses took 1 s each: 7 left.
  assert.equal(eta.remainingMs(102 * SEC), 7 * SEC);
});

test('a jump in done counts as one response but its work is credited', () => {
  // A failed file credits its unread sections at once (done jumps); that
  // is one event, and the remaining work shrinks by the whole jump.
  const eta = new EtaEstimator();
  eta.record('reading', 0, 100, 0);
  eta.record('reading', 1, 100, 2 * SEC);
  eta.record('reading', 41, 100, 4 * SEC);
  // 2 responses in 4 s = 2 s each; 59 left.
  assert.equal(eta.remainingMs(4 * SEC), 118 * SEC);
});

test('a new phase starts a fresh estimate', () => {
  const eta = new EtaEstimator();
  eta.record('converting', 0, 2, 0);
  eta.record('converting', 1, 2, 30 * SEC);
  eta.record('reading', 0, 100, 40 * SEC);
  assert.equal(eta.remainingMs(41 * SEC), null);
  eta.record('reading', 1, 100, 42 * SEC);
  assert.equal(eta.remainingMs(42 * SEC), 99 * 2 * SEC);
});

test('repeated progress with no new work is not a response', () => {
  const eta = new EtaEstimator();
  eta.record('reading', 0, 10, 0);
  eta.record('reading', 1, 10, 1 * SEC);
  eta.record('reading', 1, 10, 50 * SEC); // same count again
  assert.equal(eta.remainingMs(1 * SEC), 9 * SEC);
});

test('finished phase has nothing left', () => {
  const eta = new EtaEstimator();
  eta.record('reading', 0, 2, 0);
  eta.record('reading', 2, 2, 1 * SEC);
  assert.equal(eta.remainingMs(1 * SEC), 0);
});

test('fmtRemaining rounds up to whole minutes past a minute', () => {
  assert.equal(fmtRemaining(45 * SEC), 'under a minute');
  assert.equal(fmtRemaining(61 * SEC), '2 min');
  assert.equal(fmtRemaining(59 * 60 * SEC), '59 min');
  assert.equal(fmtRemaining((4 * 3600 + 5 * 60) * SEC), '4 h 05 min');
  assert.equal(fmtRemaining((26 * 3600) * SEC), '26 h 00 min');
});
