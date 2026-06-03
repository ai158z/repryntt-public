#!/usr/bin/env python3
"""
volume_dial.py — Maps USB audio dial HID events to ALSA mixer volume.

The "USB PnP Audio Device" dial sends KEY_VOLUMEUP (115), KEY_VOLUMEDOWN (114),
and KEY_MUTE (113) as HID keyboard events on /dev/input/event1. On a headless
Jetson (no desktop environment), nothing listens to those events by default.

This script reads the input device and translates dial turns into
`amixer -c 0 sset Speaker <±5%>` commands.

Usage:
    python3 scripts/volume_dial.py          # foreground
    python3 scripts/volume_dial.py &        # background
    # Or via systemd (see bottom of file for unit template)

Requires: read access to /dev/input/event* (run as root or add user to 'input' group)
"""

import struct
import subprocess
import sys
import os
import signal
import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [volume_dial] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────

INPUT_DEVICE = "/dev/input/event1"  # USB PnP Audio Device HID
ALSA_CARD = "0"                      # card 0 = USB PnP Audio Device
MIXER_CONTROL = "Speaker"
STEP_PERCENT = 5                     # volume change per click

# Linux input event codes
EV_KEY = 0x01
KEY_MUTE = 113
KEY_VOLUMEDOWN = 114
KEY_VOLUMEUP = 115

# Key states
KEY_PRESS = 1    # key down
KEY_RELEASE = 0
KEY_REPEAT = 2   # auto-repeat (held down)


def find_dial_device() -> str:
    """Find the USB PnP Audio Device input event node."""
    # Try the known path first
    if os.path.exists(INPUT_DEVICE):
        return INPUT_DEVICE

    # Search /proc/bus/input/devices for it
    try:
        with open("/proc/bus/input/devices") as f:
            content = f.read()
        blocks = content.split("\n\n")
        for block in blocks:
            if "USB PnP Audio Device" in block:
                for line in block.split("\n"):
                    if line.startswith("H: Handlers="):
                        for handler in line.split("=", 1)[1].split():
                            if handler.startswith("event"):
                                path = f"/dev/input/{handler}"
                                if os.path.exists(path):
                                    return path
    except Exception:
        pass

    return INPUT_DEVICE  # fallback


def set_volume(percent_change: int) -> None:
    """Adjust ALSA mixer volume by a percentage step."""
    direction = f"{abs(percent_change)}%+" if percent_change > 0 else f"{abs(percent_change)}%-"
    try:
        result = subprocess.run(
            ["amixer", "-c", ALSA_CARD, "sset", MIXER_CONTROL, direction],
            capture_output=True, text=True, timeout=2,
        )
        # Parse current volume from output
        for line in result.stdout.split("\n"):
            if "Playback" in line and "%" in line:
                log.info(f"Volume: {line.strip()}")
                break
    except Exception as e:
        log.error(f"amixer failed: {e}")


def toggle_mute() -> None:
    """Toggle ALSA mixer mute."""
    try:
        subprocess.run(
            ["amixer", "-c", ALSA_CARD, "sset", MIXER_CONTROL, "toggle"],
            capture_output=True, timeout=2,
        )
        # Read back state
        result = subprocess.run(
            ["amixer", "-c", ALSA_CARD, "sget", MIXER_CONTROL],
            capture_output=True, text=True, timeout=2,
        )
        for line in result.stdout.split("\n"):
            if "Playback" in line and "[" in line:
                log.info(f"Mute toggle: {line.strip()}")
                break
    except Exception as e:
        log.error(f"amixer mute failed: {e}")


def run():
    device = find_dial_device()
    log.info(f"Listening on {device} for volume dial events")
    log.info(f"Card {ALSA_CARD}, control '{MIXER_CONTROL}', step ±{STEP_PERCENT}%")

    # struct input_event on aarch64: unsigned long (8), unsigned long (8), unsigned short, unsigned short, int
    EVENT_SIZE = struct.calcsize("llHHi")

    try:
        with open(device, "rb") as f:
            while True:
                data = f.read(EVENT_SIZE)
                if not data or len(data) < EVENT_SIZE:
                    break

                tv_sec, tv_usec, ev_type, code, value = struct.unpack("llHHi", data)

                if ev_type != EV_KEY:
                    continue

                # React on key press and repeat (not release)
                if value not in (KEY_PRESS, KEY_REPEAT):
                    continue

                if code == KEY_VOLUMEUP:
                    set_volume(+STEP_PERCENT)
                elif code == KEY_VOLUMEDOWN:
                    set_volume(-STEP_PERCENT)
                elif code == KEY_MUTE:
                    toggle_mute()

    except PermissionError:
        log.error(f"Permission denied on {device}. Run as root or: sudo usermod -aG input $USER")
        sys.exit(1)
    except FileNotFoundError:
        log.error(f"Device {device} not found. Is the USB audio dongle plugged in?")
        sys.exit(1)
    except KeyboardInterrupt:
        log.info("Stopped")


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    run()

# ── systemd unit (install with: sudo systemctl enable --now volume-dial) ──
# Save as /etc/systemd/system/volume-dial.service:
#
# [Unit]
# Description=USB Audio Dial → ALSA Volume
# After=sound.target
#
# [Service]
# ExecStart=/usr/bin/python3 /opt/repryntt/scripts/volume_dial.py
# Restart=on-failure
# RestartSec=5
# User=root
#
# [Install]
# WantedBy=multi-user.target
