#!/usr/bin/env python3
"""
SAIGE Connection Detector
Determines whether the current session is local (on-device) or remote (SSH / VS Code Remote / etc.)
so notification delivery can be routed appropriately.
"""

import os
import logging

logger = logging.getLogger(__name__)

# Connection types
LOCAL  = 'local'
REMOTE = 'remote'


def detect_connection_type() -> str:
    """
    Detect whether the current process is running in a local or remote session.

    Remote indicators (any of these → REMOTE):
        - SSH_CONNECTION or SSH_CLIENT env var set (SSH session)
        - VSCODE_GIT_ASKPASS_NODE or VSCODE_IPC_HOOK_CLI set (VS Code Remote)
        - REMOTEHOST set (some remote desktop tools)
        - No DISPLAY and no WAYLAND_DISPLAY (headless / SSH without X forwarding)

    Returns:
        'local'  – user is physically at the device (display available, no SSH)
        'remote' – user is connected over the network
    """
    # Explicit SSH session
    if os.environ.get('SSH_CONNECTION') or os.environ.get('SSH_CLIENT') or os.environ.get('SSH_TTY'):
        return REMOTE

    # VS Code Remote (Remote-SSH, Remote-Containers, Codespaces)
    if os.environ.get('VSCODE_GIT_ASKPASS_NODE') or os.environ.get('VSCODE_IPC_HOOK_CLI'):
        return REMOTE

    # Generic remote host markers
    if os.environ.get('REMOTEHOST'):
        return REMOTE

    # If there's a local display, likely local
    if os.environ.get('DISPLAY') or os.environ.get('WAYLAND_DISPLAY'):
        return LOCAL

    # No display at all — could be headless, treat as remote
    return REMOTE


def has_local_display() -> bool:
    """True if a local graphical display is available (X11 / Wayland)."""
    return bool(os.environ.get('DISPLAY') or os.environ.get('WAYLAND_DISPLAY'))


def get_notification_channels(priority: int = 0) -> dict:
    """
    Return which notification channels should be used based on connection type.

    Args:
        priority: 0=casual, 1=important, 2=urgent

    Returns:
        dict with boolean flags:
            desktop_notify  – use notify-send / osascript (only if local)
            gui_popup       – launch tkinter popup (only if local + display)
            websocket_push  – POST to chat server /api/ai_message (always)
            terminal_bell   – print \\a bell character (always, cheap)
            browser_notify  – rely on Web Notifications API in chat page (always)
    """
    conn = detect_connection_type()
    local = (conn == LOCAL)
    display = has_local_display()

    return {
        'desktop_notify': local and display,
        'gui_popup':      local and display and priority >= 1,
        'websocket_push': True,          # always — the browser client handles it
        'terminal_bell':  True,          # always — cheap, audible in SSH
        'browser_notify': True,          # always — browser Notification API in chat page
        'connection_type': conn,
    }


if __name__ == '__main__':
    import json
    conn = detect_connection_type()
    channels = get_notification_channels(priority=1)
    print(f"Connection type: {conn}")
    print(f"Notification channels: {json.dumps(channels, indent=2)}")
