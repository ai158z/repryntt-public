# Icon placeholders

This directory ships with no binary icons. Before cutting a desktop
release, drop these files in here:

| File | Purpose | Format |
|---|---|---|
| `icon.png` | base / Linux installer icon | ≥512×512 PNG |
| `icon.icns` | macOS bundle icon | ICNS (use `iconutil` or `electron-icon-builder`) |
| `icon.ico` | Windows installer + EXE icon | multi-resolution ICO |
| `tray-icon.png` | system tray icon | 16×16 or 32×32 PNG (mac: black-on-transparent template image) |

If any of these are missing, `electron-builder` will fall back to the
electron default icon (gray crystal). The app itself runs fine without
them — see `electron/main.cjs` for the fallback that uses
`nativeImage.createEmpty()` so the tray never crashes when the icon is
absent.

The cleanest way to generate everything from one PNG:

```bash
npx electron-icon-builder --input=./icon-source.png --output=./
```

…then commit the resulting `icon.png`, `icon.icns`, `icon.ico`.
