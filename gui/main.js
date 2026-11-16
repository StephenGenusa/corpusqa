'use strict';

const {
  app, BrowserWindow, dialog, ipcMain, nativeTheme, powerSaveBlocker, shell,
} = require('electron');
const path = require('node:path');
const { Backend, ensureUserConfig, resolveBackend } = require('./src/bridge');
const { createSleepGuard, trackRun } = require('./src/sleep-guard');

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

ipcMain.handle('pick-folder', async (_e, defaultPath) => {
  const res = await dialog.showOpenDialog(win, {
    title: 'Choose a folder of documents',
    defaultPath: typeof defaultPath === 'string' && defaultPath ? defaultPath : undefined,
    properties: ['openDirectory'],
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
