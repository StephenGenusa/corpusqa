'use strict';

const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('corpusqa', {
  /** Sends a backend request; resolves to `{ok, result}` or `{ok: false, error, fields}`. */
  request: (cmd, params) => ipcRenderer.invoke('backend', cmd, params || {}),
  /** Subscribes to backend events; returns an unsubscribe function. */
  onEvent: (fn) => {
    const handler = (_e, event) => fn(event);
    ipcRenderer.on('backend-event', handler);
    return () => ipcRenderer.removeListener('backend-event', handler);
  },
  /** Opens a folder picker; resolves to a path or null. */
  pickFolder: (defaultPath) => ipcRenderer.invoke('pick-folder', defaultPath || ''),
  /** Resolves to `{running, lastExit: {code, signal, stderrTail} | null}`. */
  backendStatus: () => ipcRenderer.invoke('backend-status'),
  /** Stops (if needed) and restarts the backend process. */
  restartBackend: () => ipcRenderer.invoke('backend-restart'),
  /** Reveals the config file (and its `.env`) in the file manager. */
  openConfigFolder: () => ipcRenderer.invoke('open-config-folder'),
});
