'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const fs = require('node:fs');
const os = require('node:os');
const { EventEmitter } = require('node:events');
const { Backend, resolveBackend, ensureUserConfig, killTree } = require('../src/bridge');

const NODE = process.execPath;

// A tiny stand-in backend: replies to each request line, echoing cmd/params.
const ECHO = `
const rl = require('readline').createInterface({ input: process.stdin });
process.stdout.write(JSON.stringify({ event: 'notice', message: 'hello' }) + '\\n');
rl.on('line', (line) => {
  const req = JSON.parse(line);
  if (req.cmd === 'fail') {
    process.stdout.write(JSON.stringify({ id: req.id, error: 'bad value',
      fields: { 'tasks.extract.temperature': 'too hot' } }) + '\\n');
  } else {
    process.stdout.write(JSON.stringify({ id: req.id,
      result: { cmd: req.cmd, params: req.params } }) + '\\n');
  }
});
rl.on('close', () => process.exit(0));
`;

function nextExit(backend) {
  return new Promise((resolve) => backend.once('exit', resolve));
}

test('bridge reports exit with stderr tail', async () => {
  const backend = new Backend({
    cmd: NODE,
    args: ['-e', "console.error('boom'); setTimeout(() => process.exit(3), 100)"],
  });
  const exited = nextExit(backend);
  backend.start();
  const pending = backend.request('config.get', {});
  const info = await exited;
  assert.equal(info.code, 3);
  assert.ok(info.stderrTail.some((l) => l.includes('boom')), info.stderrTail.join('|'));
  await assert.rejects(pending, /backend exited/);
  await assert.rejects(backend.request('config.get', {}), /not running/);
});

test('bridge round-trips requests and forwards events', async () => {
  const backend = new Backend({ cmd: NODE, args: ['-e', ECHO] });
  const events = [];
  backend.on('event', (e) => events.push(e));
  backend.start();
  const result = await backend.request('models.list', { provider: 'p' });
  assert.deepEqual(result, { cmd: 'models.list', params: { provider: 'p' } });
  assert.deepEqual(events, [{ event: 'notice', message: 'hello' }]);
  const err = await backend.request('fail', {}).then(
    () => null,
    (e) => e,
  );
  assert.equal(err.message, 'bad value');
  assert.deepEqual(err.fields, { 'tasks.extract.temperature': 'too hot' });
  const code = await backend.stop();
  assert.equal(code, 0);
});

test('stop closes stdin and lets a well-behaved backend exit', async () => {
  const backend = new Backend({ cmd: NODE, args: ['-e', ECHO], stopGraceMs: 5000 });
  backend.start();
  await backend.request('ping', {});
  const t0 = Date.now();
  const code = await backend.stop();
  assert.equal(code, 0);
  assert.ok(Date.now() - t0 < 4000);
  assert.equal(backend.running, false);
});

test('stop kills a backend that ignores stdin EOF after the grace period', async () => {
  const backend = new Backend({
    cmd: NODE,
    args: ['-e', 'process.stdin.resume(); process.stdin.on("end", () => {}); setInterval(() => {}, 1000)'],
    stopGraceMs: 300,
  });
  backend.start();
  const t0 = Date.now();
  await backend.stop();
  const elapsed = Date.now() - t0;
  assert.ok(elapsed >= 250, `stopped too early: ${elapsed}`);
  assert.ok(elapsed < 3000, `stopped too late: ${elapsed}`);
  assert.equal(backend.running, false);
});

test('stderr tail keeps only the last 50 lines', async () => {
  const backend = new Backend({
    cmd: NODE,
    args: ['-e', 'for (let i = 0; i < 80; i++) console.error("line " + i); process.exit(1)'],
  });
  const exited = nextExit(backend);
  backend.start();
  const info = await exited;
  assert.equal(info.stderrTail.length, 50);
  assert.equal(info.stderrTail[0], 'line 30');
  assert.equal(info.stderrTail[49], 'line 79');
});

test('a command that cannot start reports exit and rejects requests', async () => {
  const backend = new Backend({ cmd: '/nonexistent/corpusqa-python', args: [] });
  const exited = nextExit(backend);
  backend.start();
  const pending = backend.request('config.get', {});
  const info = await exited;
  assert.equal(info.code, null);
  assert.ok(info.stderrTail.join('\n').includes('ENOENT'), info.stderrTail.join('|'));
  await assert.rejects(pending);
});

test('bridge can be restarted after an exit', async () => {
  const backend = new Backend({ cmd: NODE, args: ['-e', ECHO] });
  backend.start();
  await backend.request('a', {});
  await backend.stop();
  backend.start();
  const result = await backend.request('b', {});
  assert.equal(result.cmd, 'b');
  await backend.stop();
});

test('status keeps the last exit for a subscriber that arrives late', async () => {
  // Exits immediately, before anything listens (like a window still loading).
  const backend = new Backend({
    cmd: NODE,
    args: ['-e', "console.error('ImportError: no module named corpusqa'); process.exit(1)"],
  });
  assert.deepEqual(backend.status(), { running: false, lastExit: null });
  backend.start();
  assert.equal(backend.status().running, true);
  await new Promise((r) => setTimeout(r, 1500)); // no 'exit' listener attached
  const st = backend.status();
  assert.equal(st.running, false);
  assert.equal(st.lastExit.code, 1);
  assert.ok(st.lastExit.stderrTail.some((l) => l.includes('ImportError')));
  backend.start(); // a restart clears it
  assert.equal(backend.status().lastExit, null);
  await backend.stop();
});

test('status records a command that cannot start', async () => {
  const backend = new Backend({ cmd: '/nonexistent/python', args: [] });
  backend.start();
  await new Promise((r) => setTimeout(r, 300));
  const st = backend.status();
  assert.equal(st.running, false);
  assert.equal(st.lastExit.code, null);
  assert.ok(st.lastExit.stderrTail.join('\n').includes('ENOENT'));
});

// --- resolveBackend (dev branch) ---

const linuxRepo = '/home/u/corpus_qa';
const winRepo = 'C:\\Users\\u\\corpus_qa';

test('resolveBackend uses the repo venv on Linux', () => {
  const venv = path.posix.join(linuxRepo, '.venv', 'bin', 'python');
  const got = resolveBackend({
    isPackaged: false, resourcesPath: '/x', platform: 'linux', repoRoot: linuxRepo,
    env: {}, exists: (p) => p === venv,
  });
  assert.deepEqual(got, {
    cmd: venv,
    args: ['-m', 'corpusqa.gui_backend', '--config', '/home/u/corpus_qa/corpusqa.yaml'],
    cwd: linuxRepo,
  });
});

test('resolveBackend uses the repo venv on Windows', () => {
  const venv = 'C:\\Users\\u\\corpus_qa\\.venv\\Scripts\\python.exe';
  const got = resolveBackend({
    isPackaged: false, resourcesPath: 'C:\\x', platform: 'win32', repoRoot: winRepo,
    env: {}, exists: (p) => p === venv,
  });
  assert.equal(got.cmd, venv);
  assert.deepEqual(got.args, [
    '-m', 'corpusqa.gui_backend', '--config', 'C:\\Users\\u\\corpus_qa\\corpusqa.yaml',
  ]);
  assert.equal(got.cwd, winRepo);
});

test('resolveBackend honours CORPUSQA_PYTHON and CORPUSQA_CONFIG', () => {
  const got = resolveBackend({
    isPackaged: false, resourcesPath: '/x', platform: 'linux', repoRoot: linuxRepo,
    env: { CORPUSQA_PYTHON: '/opt/py/bin/python3', CORPUSQA_CONFIG: '/tmp/c.yaml' },
    exists: () => true,
  });
  assert.equal(got.cmd, '/opt/py/bin/python3');
  assert.deepEqual(got.args, ['-m', 'corpusqa.gui_backend', '--config', '/tmp/c.yaml']);
});

test('resolveBackend falls back to python on PATH', () => {
  const got = resolveBackend({
    isPackaged: false, resourcesPath: '/x', platform: 'linux', repoRoot: linuxRepo,
    env: {}, exists: () => false,
  });
  assert.equal(got.cmd, 'python');
});

// --- resolveBackend (packaged branch) and first-launch config ---

test('packaged mode resolves bundled backend with config and env file', () => {
  const r = resolveBackend({
    isPackaged: true, resourcesPath: '/r', platform: 'linux', userData: '/u',
    env: { CORPUSQA_PYTHON: '/ignored/python', CORPUSQA_CONFIG: '/ignored.yaml' },
  });
  assert.deepEqual(r, {
    cmd: '/r/backend/corpusqa-backend',
    args: ['--config', '/u/corpusqa.yaml', '--env-file', '/u/.env'],
  });
});

test('windows packaged backend has .exe', () => {
  const r = resolveBackend({
    isPackaged: true, resourcesPath: 'C:\\App\\resources', platform: 'win32',
    userData: 'C:\\Users\\me\\AppData\\Roaming\\corpusqa', env: {},
  });
  assert.deepEqual(r, {
    cmd: 'C:\\App\\resources\\backend\\corpusqa-backend.exe',
    args: [
      '--config', 'C:\\Users\\me\\AppData\\Roaming\\corpusqa\\corpusqa.yaml',
      '--env-file', 'C:\\Users\\me\\AppData\\Roaming\\corpusqa\\.env',
    ],
  });
});

test('first launch seeds config from bundled example', () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'cqa-ud-'));
  try {
    const userData = path.join(dir, 'userData'); // does not exist yet
    const example = path.join(dir, 'corpusqa.example.yaml');
    fs.writeFileSync(example, 'version: 1 # example\n');
    const cfg = ensureUserConfig(userData, example);
    assert.equal(cfg, path.join(userData, 'corpusqa.yaml'));
    assert.equal(fs.readFileSync(cfg, 'utf8'), 'version: 1 # example\n');
    fs.writeFileSync(cfg, 'mine: true\n');
    fs.writeFileSync(example, 'changed: true\n');
    assert.equal(ensureUserConfig(userData, example), cfg);
    assert.equal(fs.readFileSync(cfg, 'utf8'), 'mine: true\n');
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

const STUBBORN = 'process.stdin.resume(); process.stdin.on("end", () => {}); setInterval(() => {}, 1000)';

test('stop resolves within its bound when the kill helper fails', async () => {
  const backend = new Backend({
    cmd: NODE,
    args: ['-e', STUBBORN],
    stopGraceMs: 200,
    stopHardLimitMs: 600,
    killTree: () => {}, // e.g. taskkill missing or refused: the child lives on
  });
  backend.start();
  const child = backend.child;
  const t0 = Date.now();
  const code = await backend.stop();
  const elapsed = Date.now() - t0;
  assert.equal(code, null);
  assert.ok(elapsed >= 550 && elapsed < 2000, `elapsed ${elapsed}`);
  const gone = nextExit(backend);
  child.kill('SIGKILL');
  await gone;
});

test('stop has a default hard bound of grace + 2 s', () => {
  const backend = new Backend({ cmd: NODE, args: ['-e', ''], stopGraceMs: 300 });
  assert.equal(backend.stopHardLimitMs, 2300);
});

function fakeChild() {
  const child = { pid: 4242, exitCode: null, signalCode: null, killed: [] };
  child.kill = (sig) => child.killed.push(sig || 'SIGTERM');
  return child;
}

function fakeSpawn(result) {
  const calls = [];
  const fn = (cmd, args) => {
    calls.push([cmd, args]);
    const tk = new EventEmitter();
    setImmediate(() => tk.emit(result.event, result.value));
    return tk;
  };
  fn.calls = calls;
  return fn;
}

const tick = () => new Promise((r) => setImmediate(r));

test('windows killTree uses absolute taskkill and falls back on non-zero exit', async () => {
  const child = fakeChild();
  const spawnFn = fakeSpawn({ event: 'exit', value: 1 });
  killTree(child, { platform: 'win32', spawnFn, env: { SystemRoot: 'D:\\Win' } });
  await tick();
  await tick();
  assert.equal(spawnFn.calls[0][0], path.win32.join('D:\\Win', 'System32', 'taskkill.exe'));
  assert.deepEqual(spawnFn.calls[0][1], ['/pid', '4242', '/T', '/F']);
  assert.equal(child.killed.length, 1);
});

test('windows killTree falls back when taskkill cannot start', async () => {
  const child = fakeChild();
  const spawnFn = fakeSpawn({ event: 'error', value: new Error('ENOENT') });
  killTree(child, { platform: 'win32', spawnFn, env: {} });
  await tick();
  await tick();
  assert.equal(child.killed.length, 1);
});

test('windows killTree leaves the child alone after a clean taskkill', async () => {
  const child = fakeChild();
  const spawnFn = fakeSpawn({ event: 'exit', value: 0 });
  killTree(child, { platform: 'win32', spawnFn, env: {} });
  await tick();
  await tick();
  assert.equal(spawnFn.calls[0][0], path.win32.join('C:\\Windows', 'System32', 'taskkill.exe'));
  assert.equal(child.killed.length, 0);
});
