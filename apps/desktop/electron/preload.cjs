/* Minimal preload bridge — exposes a tiny `window.repryntt` API to the
 * renderer (the Nexus dashboard page and the welcome screen). All real
 * work happens in main.cjs; this just opens the door. */
'use strict';

const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('repryntt', {
  // Used by the dashboard's in-page error fallback page
  openSettings: () => ipcRenderer.invoke('repryntt:openSettings'),
  reconnect: () => ipcRenderer.invoke('repryntt:reconnect'),
  status: () => ipcRenderer.invoke('repryntt:status'),

  // Used by the welcome window (first-run flow)
  probeBackend: url => ipcRenderer.invoke('repryntt:probeBackend', url),
  completeWelcome: choice => ipcRenderer.invoke('repryntt:completeWelcome', choice),

  // Used by both
  openExternal: url => ipcRenderer.invoke('repryntt:openExternal', url),
  checkForUpdates: () => ipcRenderer.invoke('repryntt:checkForUpdates'),

  platform: process.platform,
});
