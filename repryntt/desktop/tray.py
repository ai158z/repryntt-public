"""
repryntt.desktop.tray — System Tray Integration
================================================
Puts a status icon in the OS system tray with quick controls.

Dependencies (optional):
    pip install pystray   (or: pip install repryntt[desktop])
    Pillow is already a core dependency.

Supports: Windows, macOS, Linux (AppIndicator or StatusNotifier).
"""
from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from repryntt.desktop.app import DesktopApp

logger = logging.getLogger(__name__)


class TrayManager:
    """System tray icon with status and controls."""

    def __init__(self, desktop_app: DesktopApp) -> None:
        self.app = desktop_app
        self._icon = None
        self._thread: threading.Thread | None = None

    # ── Lifecycle ─────────────────────────────────────────────────────

    def start(self) -> None:
        """Launch the tray icon in a daemon thread."""
        import pystray

        self._icon = pystray.Icon(
            "repryntt",
            icon=self._create_icon(),
            title="Repryntt — Autonomous AI Framework",
            menu=pystray.Menu(
                pystray.MenuItem("Open Dashboard", self._on_open, default=True),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Quit Repryntt", self._on_quit),
            ),
        )
        self._thread = threading.Thread(target=self._icon.run, daemon=True)
        self._thread.start()
        logger.info("System tray started")

    def stop(self) -> None:
        """Remove the tray icon."""
        if self._icon:
            try:
                self._icon.stop()
            except Exception:
                pass
            self._icon = None

    # ── Callbacks ─────────────────────────────────────────────────────

    def _on_open(self, icon, item) -> None:
        self.app.open_window()

    def _on_quit(self, icon, item) -> None:
        self.app.quit()

    # ── Icon generation ───────────────────────────────────────────────

    @staticmethod
    def _create_icon():
        """Programmatically generate a 64x64 tray icon using Pillow."""
        from PIL import Image, ImageDraw

        size = 64
        img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)

        # Outer ring — dark blue
        draw.ellipse([2, 2, size - 2, size - 2], fill="#161b22", outline="#58a6ff", width=3)
        # Inner dot — green (alive indicator)
        inner = 14
        draw.ellipse(
            [inner, inner, size - inner, size - inner],
            fill="#3fb950",
        )
        return img
