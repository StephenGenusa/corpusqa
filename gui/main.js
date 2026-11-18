'use strict';

const {
  app, BrowserWindow, dialog, ipcMain, nativeTheme, powerSaveBlocker, shell,
} = require('electron');
const fs = require('node:fs');
const path = require('node:path');
const { Backend, ensureUserConfig, resolveBackend } = require('./src/bridge');
const { createSleepGuard, trackRun } = require('./src/sleep-guard');

// On Linux, Electron opens file dialogs through the desktop portal when one
// is running (GNOME). The portal ignores the start folder and hidden-file
// options, so "Open results…" opened in the home folder with .corpusqa
// hidden. Requiring an impossible portal version makes it use the GTK
// dialog, which honours both.
if (process.platform === 'linux') app.commandLine.appendSwitch('xdg-portal-required-version', '999');

const REPO_ROOT = path.resolve(__dirname, '..');

let win = null;
let backend = null;
let configPath = null;
let backendStopped = false;
let stopping = false;
// No system suspension while a run is in progress (runs can take hours).
const sleepGuard = createSleepGuard(powerSaveBlocker);
const runTracker = trackRun(sleepGuard);

function send(event) {
  runTracker.event(event);
  if (win && !win.isDestroyed()) win.webContents.send('backend-event', event);
}

function createBackend() {
  const userData = app.getPath('userData');
  if (app.isPackaged) {
    // First launch: start from the bundled example config. A failure here
    // surfaces as the backend's own "cannot read config" error.
    try {
      ensureUserConfig(userData, path.join(process.resourcesPath, 'corpusqa.example.yaml'));
    } catch (err) {
      process.stderr.write(`cannot create ${userData}/corpusqa.yaml: ${err.message}\n`);
    }
  }
  const spec = resolveBackend({
    isPackaged: app.isPackaged,
    resourcesPath: process.resourcesPath,
    platform: process.platform,
    userData,
    repoRoot: REPO_ROOT,
    env: process.env,
  });
  configPath = spec.args[spec.args.indexOf('--config') + 1];
  backend = new Backend(spec);
  backend.on('event', send);
  backend.on('exit', (info) => send({ event: 'backend.exit', ...info }));
  backend.on('stderr', (line) => {
    if (process.env.CORPUSQA_DEBUG) process.stderr.write(`[backend] ${line}\n`);
  });
  backend.on('badline', (line) => process.stderr.write(`[backend stdout] ${line}\n`));
  backend.start();
}

function createWindow() {
  win = new BrowserWindow({
    width: 1320,
    height: 860,
    minWidth: 900,
    minHeight: 560,
    show: false,
    title: 'corpusqa',
    backgroundColor: nativeTheme.shouldUseDarkColors ? '#1b1a18' : '#f6f3ec',
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  });
  if (!process.env.CORPUSQA_DEBUG) win.removeMenu();
  win.webContents.setWindowOpenHandler(() => ({ action: 'deny' }));
  win.webContents.on('will-navigate', (e) => e.preventDefault());
  win.once('ready-to-show', () => win.show());
  win.on('closed', () => {
    win = null;
  });
  win.loadFile(path.join(__dirname, 'renderer', 'index.html'));
  if (process.env.CORPUSQA_SMOKE === '1') {
    require('./smoke').run(win, () => app.quit());
  }
  // README demo recorder (development only; see demo/make_gif.sh).
  if (process.env.CORPUSQA_DEMO === '1') {
    require(path.join(__dirname, '..', 'demo', 'record.js')).run(win, () => app.quit());
  }
}

ipcMain.handle('backend', async (_e, cmd, params) => {
  try {
    const result = await backend.request(String(cmd), params || {});
    runTracker.reply(String(cmd), true);
    return { ok: true, result };
  } catch (err) {
    runTracker.reply(String(cmd), false);
    return { ok: false, error: err.message, fields: err.fields || {} };
  }
});

// The backend may exit before the window has subscribed to events (e.g. no
// Python on PATH); the renderer asks for this to recover the stderr tail.
ipcMain.handle('backend-status', () => backend.status());

ipcMain.handle('backend-restart', async () => {
  await backend.stop();
  backend.start();
  return true;
});

ipcMain.handle('open-config-folder', () => {
  if (configPath) shell.showItemInFolder(configPath);
  return configPath;
});

/**
 * The first of `candidates` that is an existing folder, with a trailing
 * separator: without one GTK opens the parent and treats the last part as a
 * file name. Undefined when none exists (the dialog picks its own).
 */
function dialogStart(...candidates) {
  for (const c of candidates) {
    if (typeof c !== 'string' || !c) continue;
    try {
      if (fs.statSync(c).isDirectory()) return c.endsWith(path.sep) ? c : c + path.sep;
    } catch {
      /* missing or unreadable: try the next */
    }
  }
  return undefined;
}

ipcMain.handle('pick-folder', async (_e, folder) => {
  const res = await dialog.showOpenDialog(win, {
    title: 'Choose a folder of documents',
    defaultPath: dialogStart(folder),
    properties: ['openDirectory'],
  });
  return res.canceled || res.filePaths.length === 0 ? null : res.filePaths[0];
});

ipcMain.handle('pick-results-file', async (_e, folder) => {
  // The smoke driver cannot click a native dialog; it names the file instead.
  if (process.env.CORPUSQA_SMOKE === '1' && process.env.CORPUSQA_SMOKE_PICK) {
    return process.env.CORPUSQA_SMOKE_PICK;
  }
  const corpus = typeof folder === 'string' ? folder : '';
  const res = await dialog.showOpenDialog(win, {
    title: 'Open saved results',
    // The folder's results (a dot-folder, hence showHiddenFiles), else the folder.
    defaultPath: dialogStart(corpus && path.join(corpus, '.corpusqa', 'results'), corpus),
    properties: ['openFile', 'showHiddenFiles'],
    filters: [{ name: 'corpusqa results', extensions: ['json'] }],
  });
  return res.canceled || res.filePaths.length === 0 ? null : res.filePaths[0];
});

app.whenReady().then(() => {
  createBackend();
  createWindow();
});

app.on('window-all-closed', () => app.quit());
app.on('will-quit', () => sleepGuard.stop());

// Give the backend a chance to stop cleanly (it may be mid-conversion of a
// large file): close its stdin, wait, then kill it.
app.on('before-quit', (e) => {
  if (backendStopped || !backend) return;
  e.preventDefault();
  if (stopping) return;
  stopping = true;
  backend.stop().finally(() => {
    backendStopped = true;
    app.quit();
  });
});

for (const sig of ['SIGINT', 'SIGTERM']) process.on(sig, () => app.quit());
