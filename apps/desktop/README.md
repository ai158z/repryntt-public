# Repryntt Desktop

The native desktop app for **Repryntt** — the autonomous, self-prompting
AI agent you can actually own.

A polished window around your local Nexus dashboard (or the
[repryntt.com](https://www.repryntt.com/dashboard) hosted dashboard,
your choice), with native menus, system tray, single-instance lock,
and cross-platform installers.

Available for **macOS**, **Windows**, and **Linux**.

## How it works

```
   ┌─────────────────────┐
   │  Repryntt Desktop   │  ← this app (Electron window + native menu + tray)
   │     (Electron)      │
   └─────────┬───────────┘
             │ loads
             ▼
   ┌──────────────────────────────────────────────┐
   │  ANY ONE OF:                                  │
   │                                               │
   │  • Local Nexus    http://localhost:8089       │
   │    (your repryntt daemon, running on this box) │
   │                                               │
   │  • Cloud dashboard                             │
   │    https://www.repryntt.com/dashboard/nexus   │
   │    (paired via cloud_runner from another box) │
   └──────────────────────────────────────────────┘
```

The agent daemon runs **separately** (start it with `repryntt start` in
a terminal, or as a systemd service on Linux). Repryntt Desktop is just
the polished native window for the dashboard.

By default, the app **auto-detects** which backend to use: it first
probes `localhost:8089`, and falls back to the cloud dashboard if you're
not running the daemon locally. You can lock it to one or the other via
the **Settings** menu (Cmd/Ctrl + `,`).

## Install (prebuilt)

> ⚠️ Prebuilt installers are not yet published. Until they are, use
> the "Build from source" instructions below.

When the first release is cut, installers will be downloadable from
[GitHub Releases](https://github.com/ai158z/repryntt-public/releases):

| Platform | File |
|---|---|
| macOS Apple Silicon | `Repryntt-0.1.0-arm64.dmg` |
| macOS Intel | `Repryntt-0.1.0-x64.dmg` |
| Windows 64-bit | `Repryntt-Setup-0.1.0.exe` |
| Windows portable | `Repryntt-0.1.0-portable.exe` |
| Linux AppImage | `Repryntt-0.1.0-x86_64.AppImage` |
| Debian / Ubuntu | `repryntt_0.1.0_amd64.deb` |
| Fedora / RHEL | `repryntt-0.1.0.x86_64.rpm` |

## Build from source

Requirements:

- **Node.js 20+** and **npm**
- For Windows installers built on non-Windows: cross-compile is supported but signing requires Windows
- For macOS installers: you should build on macOS

```bash
# From the repo root:
cd apps/desktop
npm install

# Run in dev mode (with devtools open):
npm run dev

# Build a production installer for your current platform:
npm run dist

# Or target specific platforms:
npm run dist:mac        # macOS DMG + ZIP, both arm64 and x64
npm run dist:win        # Windows NSIS installer + portable EXE
npm run dist:linux      # AppImage + DEB + RPM, both x64 and arm64
npm run dist:all        # macOS + Windows + Linux all in one go (requires the matching toolchains)
```

Built installers end up in `apps/desktop/dist/`.

## What lives where

```
apps/desktop/
├── package.json              ← deps + electron-builder config
├── electron/
│   ├── main.cjs              ← main process (BrowserWindow + menu + tray)
│   └── preload.cjs           ← context-isolation bridge for the dashboard
├── assets/                   ← app icons (icon.icns / icon.ico / icon.png)
├── scripts/                  ← build helpers (currently empty)
└── README.md                 ← you are here
```

## Configuration

User settings persist to:

| Platform | Path |
|---|---|
| macOS | `~/Library/Application Support/Repryntt/repryntt-desktop.json` |
| Windows | `%APPDATA%\Repryntt\repryntt-desktop.json` |
| Linux | `~/.config/Repryntt/repryntt-desktop.json` |

Schema:

```json
{
  "backend": "auto",
  "localUrl": "http://localhost:8089",
  "cloudUrl": "https://www.repryntt.com/dashboard/nexus",
  "remember": true,
  "lastBackend": "http://localhost:8089"
}
```

`backend` can be `"auto"`, `"local"`, or `"cloud"`. Auto prefers local
and falls back to cloud.

## Replacing the placeholder icon

The repo ships with placeholder icons. To use your own, drop these
files into `assets/`:

- `icon.png` — at least 512×512 PNG (used for Linux + as a base)
- `icon.icns` — macOS bundle
- `icon.ico` — Windows ICO
- `tray-icon.png` — 16×16 or 32×32 PNG for the system tray

You can convert one master PNG to all formats with `electron-icon-builder`:

```bash
npx electron-icon-builder --input=./assets/icon-source.png --output=./assets/
```

## Why Electron, not Tauri?

We picked Electron for the same reason Hermes Desktop did: most users
will run the dashboard in a browser anyway, so the embedded Chromium
matches the browser-experience exactly. Tauri (Rust-based, smaller
binary) is an attractive option, but the WebView2 / WebKit2GTK
inconsistencies across platforms break parts of the dashboard's UI.
Electron is heavier on disk but renders identically everywhere.

## License

MIT — see the root [LICENSE](../../LICENSE).
