"""
repryntt.desktop.app — Desktop & Mobile Application Coordinator
================================================================
Brings together the dashboard shell, system tray, service manager,
and pywebview native window into one cohesive desktop experience.
Also supports headless "mobile server" mode for PWA access from
phones and tablets on the local network.

Usage:
    from repryntt.desktop import launch_desktop, launch_mobile
    launch_desktop()                 # manages services + opens window
    launch_desktop(no_manage=True)   # window only, services already running
    launch_mobile()                  # network-accessible dashboard for phones

CLI:
    repryntt desktop                 # full desktop launch
    repryntt desktop --no-llm       # skip local LLM
    repryntt desktop --no-manage    # don't manage services
    repryntt mobile                  # start mobile-accessible dashboard
    repryntt mobile --no-manage     # dashboard only, services already running
"""
from __future__ import annotations

import logging
import os
import socket
import sys
import threading
import time

logger = logging.getLogger(__name__)


class DesktopApp:
    """Coordinates the full desktop application lifecycle.

    1. Starts the dashboard Flask server (port 8891)
    2. Optionally starts all repryntt services via ServiceManager
    3. Opens a pywebview native window pointing at the dashboard
    4. Optionally runs a system tray icon (pystray)
    5. On window close — shuts down services and exits
    """

    def __init__(self) -> None:
        self.service_mgr = None
        self.tray = None
        self.window = None
        self._manage_services = True
        self._shutting_down = False

    # ── Main entry ────────────────────────────────────────────────────

    def run(
        self,
        skip_llm: bool = False,
        skip_trading: bool = False,
        skip_evolution: bool = False,
        no_manage: bool = False,
    ) -> int:
        """Launch the desktop app.  Blocks until the window is closed.

        Args:
            skip_llm:       Don't start local LLM
            skip_trading:   Don't start trading pipeline
            skip_evolution: Don't start evolution loop
            no_manage:      Don't start/stop services (assume already running)

        Returns:
            Exit code (0 = success)
        """
        self._manage_services = not no_manage

        # ── Check pywebview ───────────────────────────────────────────
        try:
            import webview  # noqa: F401
        except ImportError:
            print("\033[31mError: pywebview not installed.\033[0m")
            print("Install desktop dependencies:")
            print("  pip install repryntt[desktop]")
            print()
            print("On Linux you may also need system packages:")
            print("  sudo apt install python3-gi python3-gi-cairo gir1.2-webkit2-4.1")
            return 1

        # ── Start dashboard server ────────────────────────────────────
        from repryntt.desktop.dashboard import DASHBOARD_PORT, create_dashboard_app

        dashboard_app = create_dashboard_app()
        dash_thread = threading.Thread(
            target=lambda: dashboard_app.run(
                host="127.0.0.1",
                port=DASHBOARD_PORT,
                debug=False,
                use_reloader=False,
            ),
            daemon=True,
            name="repryntt-dashboard",
        )
        dash_thread.start()
        logger.info("Dashboard server starting on :%d", DASHBOARD_PORT)

        if not self._wait_for_port(DASHBOARD_PORT, timeout=10):
            print(f"\033[31mDashboard failed to start on port {DASHBOARD_PORT}\033[0m")
            return 1

        # ── Start services (background) ──────────────────────────────
        if self._manage_services:
            svc_thread = threading.Thread(
                target=self._start_services,
                args=(skip_llm, skip_trading, skip_evolution),
                daemon=True,
                name="repryntt-services",
            )
            svc_thread.start()

        # ── Start system tray ─────────────────────────────────────────
        self._start_tray()

        # ── Open native window (blocks on main thread) ───────────────
        import webview

        self.window = webview.create_window(
            "Repryntt",
            f"http://127.0.0.1:{DASHBOARD_PORT}",
            width=1400,
            height=900,
            min_size=(900, 600),
            text_select=True,
        )

        print("\033[1mRepryntt Desktop\033[0m — native window opening...")
        webview.start()  # blocks until the window is closed

        # ── Cleanup ──────────────────────────────────────────────────
        self._shutdown()
        return 0

    # ── Service lifecycle ─────────────────────────────────────────────

    def _start_services(
        self,
        skip_llm: bool,
        skip_trading: bool,
        skip_evolution: bool,
    ) -> None:
        try:
            from repryntt.services import ServiceManager

            self.service_mgr = ServiceManager()
            self.service_mgr.start_all(
                skip_llm=skip_llm,
                skip_trading=skip_trading,
                skip_evolution=skip_evolution,
            )
            logger.info("All services started")
        except Exception as e:
            logger.error("Service startup failed: %s", e)

    # ── System tray ───────────────────────────────────────────────────

    def _start_tray(self) -> None:
        try:
            from repryntt.desktop.tray import TrayManager

            self.tray = TrayManager(self)
            self.tray.start()
        except ImportError:
            logger.info("pystray not installed — system tray disabled")
        except Exception as e:
            logger.warning("System tray failed to start: %s", e)

    # ── Window controls ───────────────────────────────────────────────

    def open_window(self) -> None:
        """Bring the main window to focus (called from tray)."""
        if self.window:
            try:
                self.window.restore()
            except Exception:
                pass

    def quit(self) -> None:
        """Full application quit (called from tray Quit action)."""
        if self.window:
            try:
                self.window.destroy()
            except Exception:
                pass

    # ── Shutdown ──────────────────────────────────────────────────────

    def _shutdown(self) -> None:
        if self._shutting_down:
            return
        self._shutting_down = True

        # Stop tray
        if self.tray:
            try:
                self.tray.stop()
            except Exception:
                pass

        # Stop services
        if self._manage_services and self.service_mgr:
            print("\n\033[1mShutting down services...\033[0m")
            try:
                self.service_mgr.stop_all()
            except Exception as e:
                logger.error("Service shutdown error: %s", e)

        print("\033[32mRepryntt Desktop closed.\033[0m")

    # ── Helpers ───────────────────────────────────────────────────────

    @staticmethod
    def _wait_for_port(port: int, timeout: int = 10) -> bool:
        """Block until a TCP port accepts connections (or timeout)."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                    return True
            except (ConnectionRefusedError, OSError):
                time.sleep(0.2)
        return False


# ── Convenience function ──────────────────────────────────────────────

def launch_desktop(**kwargs) -> int:
    """Create a DesktopApp and run it.  Returns exit code."""
    app = DesktopApp()
    return app.run(**kwargs)


# ── Mobile Server Mode ───────────────────────────────────────────────

class MobileServer:
    """Headless dashboard server for phone/tablet access over the LAN.

    Unlike DesktopApp, this does NOT open a pywebview window.
    It serves the responsive PWA dashboard on 0.0.0.0:<port>
    with token authentication, so mobile devices on the local
    network can access and install it as a home-screen app.
    """

    def __init__(self) -> None:
        self.service_mgr = None
        self._manage_services = True

    def run(
        self,
        skip_llm: bool = False,
        skip_trading: bool = False,
        skip_evolution: bool = False,
        no_manage: bool = False,
        port: int = 8891,
    ) -> int:
        """Start the mobile-accessible dashboard.

        Prints a connection URL + auth token.  Blocks until Ctrl+C.
        """
        self._manage_services = not no_manage

        from repryntt.desktop.dashboard import (
            DASHBOARD_PORT,
            _generate_token,
            create_dashboard_app,
        )

        # Generate auth token for this session
        token = _generate_token()
        dashboard_app = create_dashboard_app(auth_token=token)

        # Find LAN IP
        lan_ip = self._get_lan_ip()

        # ── Start services (background) ──────────────────────────────
        if self._manage_services:
            svc_thread = threading.Thread(
                target=self._start_services,
                args=(skip_llm, skip_trading, skip_evolution),
                daemon=True,
                name="repryntt-services",
            )
            svc_thread.start()

        # ── Print access info ─────────────────────────────────────────
        print()
        print("\033[1m" + "=" * 56 + "\033[0m")
        print("\033[1m  Repryntt Mobile Dashboard\033[0m")
        print("\033[1m" + "=" * 56 + "\033[0m")
        print()
        print(f"  \033[32mURL:\033[0m   http://{lan_ip}:{port}")
        print(f"  \033[33mToken:\033[0m {token}")
        print()
        print("  \033[1mOn your phone/tablet:\033[0m")
        print(f"  1. Connect to the same WiFi network")
        print(f"  2. Open: http://{lan_ip}:{port}?token={token}")
        print(f"  3. Tap 'Add to Home Screen' for app-like experience")
        print()
        print("  \033[1mWorks on:\033[0m Android (Chrome) + iOS (Safari)")
        print("\033[1m" + "=" * 56 + "\033[0m")
        print()
        print("  Press Ctrl+C to stop")
        print()

        try:
            dashboard_app.run(
                host="0.0.0.0",
                port=port,
                debug=False,
                use_reloader=False,
            )
        except KeyboardInterrupt:
            pass

        # ── Cleanup ──────────────────────────────────────────────────
        if self._manage_services and self.service_mgr:
            print("\n\033[1mShutting down services...\033[0m")
            try:
                self.service_mgr.stop_all()
            except Exception as e:
                logger.error("Service shutdown error: %s", e)

        print("\033[32mMobile server stopped.\033[0m")
        return 0

    def _start_services(
        self,
        skip_llm: bool,
        skip_trading: bool,
        skip_evolution: bool,
    ) -> None:
        try:
            from repryntt.services import ServiceManager

            self.service_mgr = ServiceManager()
            self.service_mgr.start_all(
                skip_llm=skip_llm,
                skip_trading=skip_trading,
                skip_evolution=skip_evolution,
            )
            logger.info("All services started")
        except Exception as e:
            logger.error("Service startup failed: %s", e)

    @staticmethod
    def _get_lan_ip() -> str:
        """Best-effort LAN IP detection."""
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.settimeout(0.5)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except Exception:
            return "0.0.0.0"


def launch_mobile(**kwargs) -> int:
    """Create a MobileServer and run it.  Returns exit code."""
    server = MobileServer()
    return server.run(**kwargs)
