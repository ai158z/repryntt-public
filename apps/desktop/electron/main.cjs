/* ════════════════════════════════════════════════════════════════════
 *  Repryntt Desktop — main process
 *
 *  Native shell around the local Nexus dashboard (http://localhost:8089)
 *  OR a paired cloud dashboard (https://www.repryntt.com/dashboard/nexus).
 *  The agent daemon runs separately — this app is just the polished window
 *  + native menu + tray status + cross-platform installer.
 *
 *  Architecture mirrors Hermes Desktop / Cursor / any modern Electron
 *  wrapper: BrowserWindow.loadURL(<dashboard>), native menu for app-level
 *  actions, tray icon for connection status.
 * ════════════════════════════════════════════════════════════════════ */
'use strict';

const { app, BrowserWindow, Menu, Tray, dialog, shell, ipcMain, nativeImage } = require('electron');
const path = require('path');
const fs = require('fs');
const net = require('net');
const http = require('http');
const https = require('https');

// ── Auto-update + structured logging ───────────────────────────────────
// electron-log routes console.log + autoUpdater logs to a file the user
// can hand us when something goes wrong. electron-updater pulls signed
// releases from GitHub Releases (configured via package.json `publish`).
let autoUpdater = null;
let updaterLog = null;
try {
  updaterLog = require('electron-log');
  updaterLog.transports.file.level = 'info';
  autoUpdater = require('electron-updater').autoUpdater;
  autoUpdater.logger = updaterLog;
  autoUpdater.autoDownload = true;          // download in background
  autoUpdater.autoInstallOnAppQuit = true;  // apply on next quit
} catch (e) {
  // electron-updater isn't installed yet — `npm install` hasn't been
  // run. The app still runs, just without auto-update.
  console.warn('Auto-update unavailable (electron-updater not installed yet):', e.message);
}

// ── Config persistence ─────────────────────────────────────────────────
const CONFIG_DIR = path.join(app.getPath('userData'));
const CONFIG_PATH = path.join(CONFIG_DIR, 'repryntt-desktop.json');

const DEFAULT_CONFIG = {
  backend: 'auto',                                  // 'auto' | 'local' | 'cloud'
  localUrl: 'http://localhost:8089',
  cloudUrl: 'https://www.repryntt.com/dashboard/nexus',
  remember: true,
  lastBackend: null,
  // First-run state. `welcomed` flips to true once the user has
  // completed the welcome flow (picked cloud or local + the chosen
  // path was reachable). After that we skip welcome on subsequent
  // launches and go straight to the dashboard.
  welcomed: false,
  // Cloud-only auth — only set when backend === 'cloud'. Stored in
  // userData (not in ~/.repryntt/) so it travels with the desktop
  // install, not the local daemon. Treated as a secret on disk.
  cloudApiKey: '',
};

function loadConfig() {
  try {
    if (fs.existsSync(CONFIG_PATH)) {
      const data = JSON.parse(fs.readFileSync(CONFIG_PATH, 'utf-8'));
      return { ...DEFAULT_CONFIG, ...data };
    }
  } catch (e) {
    console.error('Config load failed:', e);
  }
  return { ...DEFAULT_CONFIG };
}

function saveConfig(cfg) {
  try {
    fs.mkdirSync(CONFIG_DIR, { recursive: true });
    fs.writeFileSync(CONFIG_PATH, JSON.stringify(cfg, null, 2), 'utf-8');
  } catch (e) {
    console.error('Config save failed:', e);
  }
}

// ── Backend probe ──────────────────────────────────────────────────────
//
// Server-side TCP probe so we don't get burned by browser cross-origin
// restrictions or HEAD-method allowance on the Nexus side. We just want
// "is something accepting connections on the port".

function probeTcp(host, port, timeout = 1500) {
  return new Promise(resolve => {
    const socket = new net.Socket();
    let done = false;
    const finish = ok => {
      if (done) return;
      done = true;
      socket.destroy();
      resolve(ok);
    };
    socket.setTimeout(timeout);
    socket.once('connect', () => finish(true));
    socket.once('error', () => finish(false));
    socket.once('timeout', () => finish(false));
    try {
      socket.connect(port, host);
    } catch (_) {
      finish(false);
    }
  });
}

async function probeUrl(urlStr) {
  try {
    const u = new URL(urlStr);
    const port = u.port ? parseInt(u.port, 10) : (u.protocol === 'https:' ? 443 : 80);
    const host = u.hostname;
    return await probeTcp(host, port);
  } catch (_) {
    return false;
  }
}

async function resolveBackend(cfg) {
  if (cfg.backend === 'local') return cfg.localUrl;
  if (cfg.backend === 'cloud') return cfg.cloudUrl;
  // 'auto' — prefer local, fall back to cloud
  if (await probeUrl(cfg.localUrl)) return cfg.localUrl;
  return cfg.cloudUrl;
}

// ── Window + tray state ────────────────────────────────────────────────

let mainWindow = null;
let tray = null;
let currentBackend = null;
let config = loadConfig();
let isQuitting = false;

// ── Window creation ────────────────────────────────────────────────────

function createWindow(targetUrl) {
  mainWindow = new BrowserWindow({
    width: 1400,
    height: 900,
    minWidth: 900,
    minHeight: 600,
    title: 'Repryntt',
    backgroundColor: '#ffffff',
    show: false,
    webPreferences: {
      preload: path.join(__dirname, 'preload.cjs'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: false,            // preload needs Node API for IPC
      spellcheck: true,
    },
  });

  mainWindow.once('ready-to-show', () => {
    mainWindow.show();
  });

  mainWindow.on('close', e => {
    if (!isQuitting && process.platform === 'darwin') {
      // Standard mac behavior: hide to dock, keep running
      e.preventDefault();
      mainWindow.hide();
    }
  });

  mainWindow.on('closed', () => {
    mainWindow = null;
  });

  // Open external links in the OS browser instead of inside the app
  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    try {
      const u = new URL(url);
      const backendHost = new URL(targetUrl).hostname;
      if (u.hostname === backendHost) {
        return { action: 'allow' };
      }
    } catch (_) {}
    shell.openExternal(url);
    return { action: 'deny' };
  });

  // Recover gracefully if the page fails to load (backend down, etc.)
  mainWindow.webContents.on('did-fail-load', (_e, errorCode, errorDescription, validatedURL) => {
    if (errorCode === -3) return; // user-initiated abort
    showBackendErrorPage(validatedURL, errorDescription);
  });

  return mainWindow.loadURL(targetUrl);
}

function showBackendErrorPage(url, reason) {
  if (!mainWindow) return;
  const safeUrl = String(url || '').replace(/[<>&"']/g, c => `&#${c.charCodeAt(0)};`);
  const safeReason = String(reason || 'unknown').replace(/[<>&"']/g, c => `&#${c.charCodeAt(0)};`);
  const html = `<!doctype html>
<html><head><meta charset="utf-8"><title>Repryntt</title>
<style>
  :root { font-family: -apple-system, system-ui, sans-serif; }
  body { margin: 0; padding: 56px 64px; color: #0a0a0a; background: #fff; }
  h1 { margin: 0 0 12px; font-size: 22px; }
  code, pre { background: #f5f5f5; padding: 2px 6px; border-radius: 4px; font-family: ui-monospace, monospace; }
  pre { padding: 14px; }
  p { color: #4b5563; line-height: 1.5; }
  button { padding: 10px 20px; border: none; border-radius: 8px; background: #0a0a0a; color: #fff; font-weight: 600; cursor: pointer; }
  button:hover { opacity: 0.9; }
  .row { margin-top: 24px; display: flex; gap: 8px; }
  ul { color: #4b5563; line-height: 1.7; }
</style></head>
<body>
  <h1>Couldn't reach the Repryntt dashboard</h1>
  <p>The desktop app tried to connect to:</p>
  <pre>${safeUrl}</pre>
  <p>Reason: <code>${safeReason}</code></p>
  <p>Common causes:</p>
  <ul>
    <li>The local daemon isn't running — open a terminal and run <code>repryntt start</code></li>
    <li>The Nexus service is still starting up (give it ~10 seconds and click Reconnect)</li>
    <li>You picked a remote backend in Settings but you aren't online</li>
  </ul>
  <div class="row">
    <button onclick="location.reload()">Reconnect</button>
    <button onclick="window.repryntt.openSettings()" style="background:#fff;color:#0a0a0a;border:1px solid #ddd;">Settings</button>
  </div>
</body></html>`;
  mainWindow.loadURL('data:text/html;charset=utf-8,' + encodeURIComponent(html));
}

// ── Tray ───────────────────────────────────────────────────────────────

function buildTray() {
  if (process.platform === 'darwin') {
    // Native menu bar item is rendered by tray.png
  }
  const iconPath = path.join(__dirname, '..', 'assets', 'tray-icon.png');
  let trayImage;
  if (fs.existsSync(iconPath)) {
    trayImage = nativeImage.createFromPath(iconPath);
  } else {
    // 1×1 transparent fallback so tray creation never crashes if icon is missing
    trayImage = nativeImage.createEmpty();
  }
  if (process.platform === 'darwin') trayImage.setTemplateImage(true);
  try {
    tray = new Tray(trayImage);
  } catch (e) {
    console.warn('Tray init failed (icon missing?):', e.message);
    return;
  }
  refreshTrayMenu();
  tray.setToolTip('Repryntt — autonomous AI agent');
  tray.on('click', () => {
    if (!mainWindow) return;
    if (mainWindow.isVisible()) mainWindow.hide();
    else { mainWindow.show(); mainWindow.focus(); }
  });
}

function refreshTrayMenu() {
  if (!tray) return;
  const statusLabel = currentBackend
    ? `Connected: ${currentBackend}`
    : 'Disconnected';
  const menu = Menu.buildFromTemplate([
    { label: statusLabel, enabled: false },
    { type: 'separator' },
    { label: 'Show Repryntt', click: () => { if (mainWindow) { mainWindow.show(); mainWindow.focus(); } } },
    { label: 'Reconnect', click: () => reconnect() },
    { label: 'Settings…', click: () => openSettings() },
    { type: 'separator' },
    { label: 'Open repryntt.com', click: () => shell.openExternal('https://www.repryntt.com') },
    { type: 'separator' },
    { label: 'Quit Repryntt', click: () => { isQuitting = true; app.quit(); } },
  ]);
  tray.setContextMenu(menu);
}

// ── Application menu ───────────────────────────────────────────────────

function buildAppMenu() {
  const isMac = process.platform === 'darwin';
  const template = [
    ...(isMac ? [{
      label: app.name,
      submenu: [
        { role: 'about' },
        { type: 'separator' },
        { label: 'Settings…', accelerator: 'Cmd+,', click: () => openSettings() },
        { type: 'separator' },
        { role: 'services' },
        { type: 'separator' },
        { role: 'hide' },
        { role: 'hideOthers' },
        { role: 'unhide' },
        { type: 'separator' },
        { role: 'quit' },
      ],
    }] : []),
    {
      label: 'File',
      submenu: [
        { label: 'Reconnect', accelerator: 'CommandOrControl+R', click: () => reconnect() },
        ...(!isMac ? [{ label: 'Settings…', accelerator: 'Ctrl+,', click: () => openSettings() }] : []),
        { type: 'separator' },
        isMac ? { role: 'close' } : { role: 'quit' },
      ],
    },
    {
      label: 'Edit',
      submenu: [
        { role: 'undo' },
        { role: 'redo' },
        { type: 'separator' },
        { role: 'cut' },
        { role: 'copy' },
        { role: 'paste' },
        { role: 'selectAll' },
      ],
    },
    {
      label: 'View',
      submenu: [
        { role: 'reload' },
        { role: 'forceReload' },
        { role: 'toggleDevTools' },
        { type: 'separator' },
        { role: 'resetZoom' },
        { role: 'zoomIn' },
        { role: 'zoomOut' },
        { type: 'separator' },
        { role: 'togglefullscreen' },
      ],
    },
    {
      label: 'Window',
      submenu: [
        { role: 'minimize' },
        { role: 'zoom' },
        ...(isMac ? [
          { type: 'separator' },
          { role: 'front' },
          { type: 'separator' },
          { role: 'window' },
        ] : [{ role: 'close' }]),
      ],
    },
    {
      role: 'help',
      submenu: [
        { label: 'Check for updates…', click: () => checkForUpdatesManual() },
        { label: 'Run setup again', click: () => { config.welcomed = false; saveConfig(config); createWelcomeWindow(); if (mainWindow) mainWindow.close(); } },
        { type: 'separator' },
        { label: 'Open repryntt.com', click: () => shell.openExternal('https://www.repryntt.com') },
        { label: 'View on GitHub', click: () => shell.openExternal('https://github.com/ai158z/repryntt-public') },
        { label: 'Report an issue', click: () => shell.openExternal('https://github.com/ai158z/repryntt-public/issues') },
      ],
    },
  ];
  Menu.setApplicationMenu(Menu.buildFromTemplate(template));
}

// ── Settings dialog ────────────────────────────────────────────────────

async function openSettings() {
  const buttons = ['Cancel', 'Auto-detect (recommended)', 'Local (this machine)', 'Cloud (repryntt.com)'];
  const result = await dialog.showMessageBox(mainWindow || null, {
    type: 'question',
    title: 'Repryntt — Settings',
    message: 'Which backend should Repryntt connect to?',
    detail: `Auto-detect prefers your local agent daemon (${config.localUrl}) and falls back to the cloud dashboard if it isn't running.

Local only:    ${config.localUrl}
Cloud only:    ${config.cloudUrl}

Current: ${currentBackend || '(disconnected)'}`,
    buttons,
    defaultId: 1,
    cancelId: 0,
  });
  if (result.response === 0) return;
  const choice = ['', 'auto', 'local', 'cloud'][result.response];
  config.backend = choice;
  saveConfig(config);
  reconnect();
}

// ── Reconnect ──────────────────────────────────────────────────────────

async function reconnect() {
  const target = await resolveBackend(config);
  currentBackend = target;
  config.lastBackend = target;
  saveConfig(config);
  refreshTrayMenu();
  if (mainWindow) await createWindow(target);
}

// ── Welcome window (first-run) ─────────────────────────────────────────

let welcomeWindow = null;

function createWelcomeWindow() {
  welcomeWindow = new BrowserWindow({
    width: 880,
    height: 640,
    minWidth: 700,
    minHeight: 540,
    title: 'Welcome to Repryntt',
    backgroundColor: '#ffffff',
    resizable: true,
    minimizable: false,
    maximizable: false,
    fullscreenable: false,
    show: false,
    webPreferences: {
      preload: path.join(__dirname, 'preload.cjs'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: false,
    },
  });
  welcomeWindow.once('ready-to-show', () => welcomeWindow.show());
  welcomeWindow.on('closed', () => { welcomeWindow = null; });
  welcomeWindow.loadFile(path.join(__dirname, 'welcome.html'));
}

// ── Auto-update plumbing ───────────────────────────────────────────────

function setupAutoUpdate() {
  if (!autoUpdater) return;

  autoUpdater.on('checking-for-update', () => {
    if (updaterLog) updaterLog.info('Checking for update…');
  });

  autoUpdater.on('update-available', info => {
    if (updaterLog) updaterLog.info('Update available:', info?.version);
    // Don't block — downloads silently in the background.
  });

  autoUpdater.on('update-not-available', () => {
    if (updaterLog) updaterLog.info('Up to date.');
  });

  autoUpdater.on('error', err => {
    if (updaterLog) updaterLog.error('Auto-update error:', err?.message || err);
  });

  autoUpdater.on('download-progress', progress => {
    if (updaterLog) {
      updaterLog.info(
        `Downloading update: ${progress?.percent?.toFixed(1)}% (${Math.round(progress?.bytesPerSecond / 1024)} KB/s)`
      );
    }
  });

  autoUpdater.on('update-downloaded', async info => {
    if (updaterLog) updaterLog.info('Update downloaded:', info?.version);
    const result = await dialog.showMessageBox(mainWindow || null, {
      type: 'info',
      title: 'Repryntt — update ready',
      message: `A new version (${info?.version}) is ready to install.`,
      detail: 'Install it now? Repryntt will quit, install the update, and reopen.',
      buttons: ['Install and restart', 'Later'],
      defaultId: 0,
      cancelId: 1,
    });
    if (result.response === 0) {
      isQuitting = true;
      autoUpdater.quitAndInstall();
    }
    // If they pick Later, the update applies on the next quit (because
    // we set autoInstallOnAppQuit = true).
  });

  // Defer the first check ~10s after launch so we don't compete with
  // the dashboard loading. Then re-check every 4 hours.
  setTimeout(() => {
    try { autoUpdater.checkForUpdates(); } catch (e) {
      if (updaterLog) updaterLog.warn('Initial update check failed:', e?.message || e);
    }
  }, 10_000);
  setInterval(() => {
    try { autoUpdater.checkForUpdates(); } catch (_) {}
  }, 4 * 60 * 60 * 1000);
}

async function checkForUpdatesManual() {
  if (!autoUpdater) {
    await dialog.showMessageBox(mainWindow || null, {
      type: 'info',
      title: 'Updates',
      message: 'Auto-update isn\'t available in this build.',
      detail: 'Download the latest installer from the GitHub releases page.',
      buttons: ['Open releases page', 'Cancel'],
      defaultId: 0,
      cancelId: 1,
    }).then(r => {
      if (r.response === 0) shell.openExternal('https://github.com/ai158z/repryntt-public/releases');
    });
    return;
  }
  try {
    const r = await autoUpdater.checkForUpdates();
    if (!r?.updateInfo || r.updateInfo.version === app.getVersion()) {
      await dialog.showMessageBox(mainWindow || null, {
        type: 'info',
        title: 'Repryntt is up to date',
        message: `You're running v${app.getVersion()} — the latest.`,
        buttons: ['OK'],
      });
    }
  } catch (e) {
    await dialog.showErrorBox('Update check failed', String(e?.message || e));
  }
}

// ── IPC for preload-script bridge ──────────────────────────────────────

ipcMain.handle('repryntt:openSettings', () => openSettings());
ipcMain.handle('repryntt:reconnect', () => reconnect());
ipcMain.handle('repryntt:status', () => ({
  backend: currentBackend,
  config: { ...config, cloudApiKey: config.cloudApiKey ? '<redacted>' : '' },
}));
ipcMain.handle('repryntt:checkForUpdates', () => checkForUpdatesManual());
ipcMain.handle('repryntt:openExternal', (_e, url) => shell.openExternal(url));

// Probe an arbitrary URL (welcome screen Test button)
ipcMain.handle('repryntt:probeBackend', async (_e, urlStr) => {
  const ok = await probeUrl(urlStr);
  return { ok };
});

// Save the welcome-flow result and switch into the dashboard
ipcMain.handle('repryntt:completeWelcome', async (_e, payload) => {
  try {
    if (payload?.backend === 'cloud') {
      config.backend = 'cloud';
      config.cloudApiKey = payload.apiKey || '';
      if (payload.cloudUrl) config.cloudUrl = payload.cloudUrl;
    } else if (payload?.backend === 'local') {
      config.backend = 'local';
      if (payload.localUrl) config.localUrl = payload.localUrl;
    } else {
      return { ok: false, error: 'Unknown backend choice.' };
    }
    config.welcomed = true;
    saveConfig(config);

    // Close welcome and open the dashboard
    const target = await resolveBackend(config);
    currentBackend = target;
    config.lastBackend = target;
    saveConfig(config);
    refreshTrayMenu();
    if (welcomeWindow && !welcomeWindow.isDestroyed()) welcomeWindow.close();
    await createWindow(target);
    return { ok: true };
  } catch (e) {
    return { ok: false, error: String(e?.message || e) };
  }
});

// ── Single-instance lock ───────────────────────────────────────────────

const gotTheLock = app.requestSingleInstanceLock();
if (!gotTheLock) {
  app.quit();
} else {
  app.on('second-instance', () => {
    if (mainWindow) {
      if (mainWindow.isMinimized()) mainWindow.restore();
      mainWindow.show();
      mainWindow.focus();
    }
  });
}

// ── App lifecycle ──────────────────────────────────────────────────────

app.whenReady().then(async () => {
  buildAppMenu();
  buildTray();
  setupAutoUpdate();

  // First-run experience: if we've never seen this user, show the
  // welcome window before going to the dashboard. After they pick
  // cloud or local, the completeWelcome IPC handler closes the
  // welcome and opens the dashboard.
  if (!config.welcomed) {
    createWelcomeWindow();
    return;
  }

  const target = await resolveBackend(config);
  currentBackend = target;
  config.lastBackend = target;
  saveConfig(config);
  refreshTrayMenu();
  await createWindow(target);
});

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') app.quit();
});

app.on('activate', () => {
  if (BrowserWindow.getAllWindows().length === 0 && currentBackend) {
    createWindow(currentBackend);
  } else if (mainWindow) {
    mainWindow.show();
  }
});

app.on('before-quit', () => { isQuitting = true; });
