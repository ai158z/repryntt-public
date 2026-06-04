/* Minimal preload bridge — exposes a tiny `window.repryntt` API to the
 * renderer (the Nexus dashboard page). The Nexus web UI doesn't need to
 * know about Electron; this just lets the in-page error fallback call
 * Settings without spawning a new window. */
'use strict';

const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('repryntt', {
  openSettings: () => ipcRenderer.invoke('repryntt:openSettings'),
  reconnect: () => ipcRenderer.invoke('repryntt:reconnect'),
  status: () => ipcRenderer.invoke('repryntt:status'),
  platform: process.platform,
});
