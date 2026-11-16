'use strict';

const { spawn } = require('node:child_process');
const { EventEmitter } = require('node:events');
const fs = require('node:fs');
const path = require('node:path');
const { createLineDecoder } = require('./jsonl');

const STDERR_TAIL_LINES = 50;
const DEFAULT_STOP_GRACE_MS = 3000;
// After 'exit', wait this long for the stdio streams to close (a grandchild
// such as soffice may hold them open) before finishing anyway.
const CLOSE_WAIT_MS = 500;

/**
 * Works out how to start the Python backend.
 *
 * Packaged: the frozen backend in `<resources>/backend/`, with the config
 * and `.env` in the per-user app data folder.
 *
 * Development: Python is `CORPUSQA_PYTHON`, else the repo's `.venv`
 * interpreter if present, else `python` on PATH. Config: `CORPUSQA_CONFIG`,
 * else `<repo>/corpusqa.yaml`.
 *
 * @param {object} o
 * @param {boolean} o.isPackaged running from an installed build
 * @param {string} [o.resourcesPath] Electron resources directory (packaged)
 * @param {string} o.platform `process.platform`
 * @param {string} [o.userData] per-user app data folder (packaged)
 * @param {string} [o.repoRoot] repository root, parent of `gui/` (development)
 * @param {object} [o.env] environment variables (development)
 * @param {(p: string) => boolean} [o.exists] file-exists check
 * @returns {{cmd: string, args: string[], cwd?: string}}
 */
function resolveBackend({ isPackaged, resourcesPath, platform, userData, repoRoot, env, exists }) {
  const p = platform === 'win32' ? path.win32 : path.posix;
  if (isPackaged) {
    const exe = platform === 'win32' ? 'corpusqa-backend.exe' : 'corpusqa-backend';
    return {
      cmd: p.join(resourcesPath, 'backend', exe),
      args: [
        '--config', p.join(userData, 'corpusqa.yaml'),
        '--env-file', p.join(userData, '.env'),
      ],
    };
  }
  const fileExists = exists || fs.existsSync;
  const venvPython =
    platform === 'win32'
      ? p.join(repoRoot, '.venv', 'Scripts', 'python.exe')
      : p.join(repoRoot, '.venv', 'bin', 'python');
  let cmd = 'python';
  if (env.CORPUSQA_PYTHON) cmd = env.CORPUSQA_PYTHON;
  else if (fileExists(venvPython)) cmd = venvPython;
  const configPath = env.CORPUSQA_CONFIG || p.join(repoRoot, 'corpusqa.yaml');
  return {
    cmd,
    args: ['-m', 'corpusqa.gui_backend', '--config', configPath],
    cwd: repoRoot,
  };
}

/**
 * The backend child process, spoken to in JSON lines over stdio.
 *
 * Emits `event` (each protocol event object), `exit`
 * (`{code, signal, stderrTail}`) and `badline` (stdout text that was not
 * protocol JSON).
 */
class Backend extends EventEmitter {
  /**
   * @param {object} o
   * @param {string} [o.cmd] executable (or pass `python` + `configPath`)
   * @param {string[]} [o.args] its arguments
   * @param {string} [o.python] Python interpreter
   * @param {string} [o.configPath] config file for `python -m corpusqa.gui_backend`
   * @param {string} [o.cwd] working directory
   * @param {object} [o.env] environment (defaults to this process's)
   * @param {number} [o.stopGraceMs] wait after closing stdin before killing
   */
  constructor({ cmd, args, python, configPath, cwd, env, stopGraceMs } = {}) {
    super();
    this.cmd = cmd || python;
    this.args = args || ['-m', 'corpusqa.gui_backend', '--config', configPath];
    this.cwd = cwd;
    this.env = env || process.env;
    this.stopGraceMs = stopGraceMs ?? DEFAULT_STOP_GRACE_MS;
    this.child = null;
    this.running = false;
    this.pending = new Map();
    this.nextId = 1;
    this.stderrTail = [];
    this.lastExit = null;
  }

  /**
   * Whether the backend runs, and how it last exited. Lets a window that
   * loads after a fast startup failure still show the stderr tail.
   * @returns {{running: boolean, lastExit: object|null}}
   */
  status() {
    return { running: this.running, lastExit: this.lastExit };
  }

  /** Spawns the backend unless it is already running. */
  start() {
    if (this.running) return;
    this.stderrTail = [];
    this.lastExit = null;
    this.running = true;
    const posix = process.platform !== 'win32';
    const child = spawn(this.cmd, this.args, {
      cwd: this.cwd,
      env: this.env,
      stdio: ['pipe', 'pipe', 'pipe'],
      windowsHide: true,
      // Own process group on POSIX so a kill also reaches its children.
      detached: posix,
    });
    this.child = child;
    let finished = false;
    let closeTimer = null;
    const finish = (code, signal) => {
      if (finished) return;
      finished = true;
      clearTimeout(closeTimer);
      stderrFeed.flush();
      const info = { code, signal: signal || null, stderrTail: [...this.stderrTail] };
      // A late exit of a child already replaced by a restart must not mark
      // the new one as stopped.
      if (this.child === child) {
        this.child = null;
        this.running = false;
        this.lastExit = info;
      }
      const reason = `backend exited (code ${code === null ? 'none' : code})`;
      for (const { reject } of this.pending.values()) reject(new Error(reason));
      this.pending.clear();
      this.emit('exit', info);
    };

    child.stdout.setEncoding('utf8');
    child.stdout.on('data', createLineDecoder(
      (obj) => this._onMessage(obj),
      (line) => this.emit('badline', line),
    ));
    child.stderr.setEncoding('utf8');
    const stderrFeed = createStderrSplitter((line) => this._pushStderr(line));
    child.stderr.on('data', stderrFeed);
    child.stdin.on('error', () => {}); // EPIPE when the child is gone
    child.on('error', (err) => {
      this._pushStderr(`${err.code || 'error'}: ${err.message}`);
      finish(null, null);
    });
    child.on('exit', (code, signal) => {
      closeTimer = setTimeout(() => finish(code, signal), CLOSE_WAIT_MS);
    });
    child.on('close', (code, signal) => finish(code, signal));
  }

  _pushStderr(line) {
    this.stderrTail.push(line);
    if (this.stderrTail.length > STDERR_TAIL_LINES) this.stderrTail.shift();
    this.emit('stderr', line);
  }

  _onMessage(obj) {
    if (typeof obj.id === 'string' && this.pending.has(obj.id)) {
      const { resolve, reject } = this.pending.get(obj.id);
      this.pending.delete(obj.id);
      if ('error' in obj) {
        const err = new Error(String(obj.error));
        err.fields = obj.fields || {};
        reject(err);
      } else {
        resolve(obj.result);
      }
    } else if (typeof obj.event === 'string') {
      this.emit('event', obj);
    } else {
      this.emit('badline', JSON.stringify(obj));
    }
  }

  /**
   * Sends one request.
   *
   * @param {string} cmd command name, e.g. `config.get`
   * @param {object} [params]
   * @returns {Promise<object>} the reply's `result`; rejects with an Error
   *   (carrying `fields` for `config.save`) on an error reply or exit
   */
  request(cmd, params = {}) {
    if (!this.running || !this.child) {
      return Promise.reject(new Error('backend is not running'));
    }
    const id = String(this.nextId++);
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      this.child.stdin.write(JSON.stringify({ id, cmd, params }) + '\n');
    });
  }

  /**
   * Closes stdin so the backend can finish cleanly, then kills it if it has
   * not exited within the grace period.
   *
   * @returns {Promise<number|null>} the exit code (null if killed or not running)
   */
  stop() {
    const child = this.child;
    if (!child || !this.running) return Promise.resolve(null);
    return new Promise((resolve) => {
      let timer = null;
      this.once('exit', ({ code }) => {
        clearTimeout(timer);
        resolve(code);
      });
      child.stdin.end();
      timer = setTimeout(() => killTree(child), this.stopGraceMs);
    });
  }
}

/**
 * Kills the child and its descendants (e.g. a `soffice` it started): its
 * process group on POSIX, `taskkill /T` on Windows, where `child.kill()`
 * would end only the backend itself.
 */
function killTree(child) {
  if (child.exitCode !== null || child.signalCode !== null) return;
  try {
    if (process.platform === 'win32' && child.pid) {
      const tk = spawn('taskkill', ['/pid', String(child.pid), '/T', '/F'], {
        stdio: 'ignore',
        windowsHide: true,
      });
      tk.on('error', () => child.kill());
    } else if (child.pid) {
      process.kill(-child.pid, 'SIGKILL');
    } else {
      child.kill();
    }
  } catch {
    try { child.kill('SIGKILL'); } catch { /* already gone */ }
  }
}

/** Splits a text stream into lines (no JSON parsing). */
function createStderrSplitter(onLine) {
  let buffer = '';
  const feed = (chunk) => {
    buffer += chunk;
    const parts = buffer.split(/\r?\n/);
    buffer = parts.pop();
    for (const line of parts) onLine(line);
  };
  feed.flush = () => {
    if (buffer) onLine(buffer);
    buffer = '';
  };
  return feed;
}

/**
 * Makes sure the per-user config exists, copying the bundled example on
 * first launch. An existing config is never overwritten.
 *
 * @param {string} userData per-user app data folder (created if missing)
 * @param {string} examplePath the bundled `corpusqa.example.yaml`
 * @returns {string} path of the user's `corpusqa.yaml`
 */
function ensureUserConfig(userData, examplePath) {
  const configPath = path.join(userData, 'corpusqa.yaml');
  fs.mkdirSync(userData, { recursive: true });
  try {
    fs.copyFileSync(examplePath, configPath, fs.constants.COPYFILE_EXCL);
  } catch (err) {
    if (err.code !== 'EEXIST') throw err;
  }
  return configPath;
}

module.exports = { Backend, resolveBackend, ensureUserConfig };
