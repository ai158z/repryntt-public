#!/usr/bin/env python3
"""
Fix GPIO pinmux for Jetson Orin Nano Super — motors + sonar sensors.

The default device tree muxes several header pins to alternate functions
(SPI1, I2S, etc.) with tristate enabled, so GPIO output has no effect.
This script writes the correct pinmux registers via /dev/mem to route
the physical pins to the GPIO controller.

Must run as root (needs /dev/mem write access).
Pinmux settings are volatile — lost on reboot. The systemd service
(repryntt-pinmux.service) runs this at boot.

Pin mapping (BOARD mode → GPIO line → pinmux register):
  Motor pins:
    Pin 29 (AIN1)  → PQ.05 line 105 → 0x2430068
    Pin 31 (AIN2)  → PQ.06 line 106 → 0x2430070
    Pin 32 (PWMA)  → PG.06 line  41 → 0x2434080
    Pin 33 (PWMB)  → PH.00 line  43 → 0x2434040
    Pin 35 (BIN1)  → PI.02 line  53 → 0x24340A0
    Pin 37 (BIN2)  → PY.02 line 124 → 0x243D048
  Sonar pins (HC-SR04):
    Pin 11 (rear TRIG)  → PR.04  line 112 → 0x2430088
    Pin 23 (rear ECHO)  → PZ.03  line 133 → 0x2445028
    Pin 24 (front TRIG) → PZ.06  line 136 → 0x2445008
    Pin 26 (front ECHO) → PZ.07  line 137 → 0x2445038
"""

import mmap
import os
import sys

# Pinmux register addresses for TB6612 motor control pins
MOTOR_PINMUX = {
    29: ("AIN1", "PQ.05", 0x2430068),
    31: ("AIN2", "PQ.06", 0x2430070),
    32: ("PWMA", "PG.06", 0x2434080),
    33: ("PWMB", "PH.00", 0x2434040),
    35: ("BIN1", "PI.02", 0x24340A0),
    37: ("BIN2", "PY.02", 0x243D048),
}

# Pinmux register addresses for HC-SR04 sonar sensor pins
SONAR_PINMUX = {
    11: ("rear TRIG", "PR.04", 0x2430098),
    23: ("rear ECHO", "PZ.03", 0x243D028),
    24: ("front TRIG", "PZ.06", 0x243D008),
    26: ("front ECHO", "PZ.07", 0x243D038),
}

# All pins that need GPIO mode
ALL_PINMUX = {**MOTOR_PINMUX, **SONAR_PINMUX}

# 0x0400 = GPIO output mode, no tristate, no pull, no input enable
GPIO_OUTPUT_MODE = 0x0400

PAGE_SIZE = 4096
PAGE_MASK = ~(PAGE_SIZE - 1)


def fix_pinmux():
    if os.geteuid() != 0:
        print("ERROR: Must run as root (need /dev/mem access)")
        sys.exit(1)

    fd = os.open("/dev/mem", os.O_RDWR | os.O_SYNC)
    changed = 0

    try:
        for pin, (name, pad, addr) in sorted(ALL_PINMUX.items()):
            page_start = addr & PAGE_MASK
            offset = addr - page_start

            with mmap.mmap(fd, PAGE_SIZE * 2, mmap.MAP_SHARED,
                           mmap.PROT_READ | mmap.PROT_WRITE,
                           offset=page_start) as mem:
                old = int.from_bytes(mem[offset:offset + 4],
                                     byteorder=sys.byteorder)

                if old != GPIO_OUTPUT_MODE:
                    mem[offset:offset + 4] = GPIO_OUTPUT_MODE.to_bytes(
                        4, byteorder=sys.byteorder)
                    check = int.from_bytes(mem[offset:offset + 4],
                                           byteorder=sys.byteorder)
                    print(f"  Pin {pin} ({name}/{pad}) @ 0x{addr:X}: "
                          f"0x{old:04X} → 0x{check:04X}")
                    changed += 1
                else:
                    print(f"  Pin {pin} ({name}/{pad}) @ 0x{addr:X}: "
                          f"already 0x{old:04X} ✓")
    finally:
        os.close(fd)

    print(f"\n{'Fixed' if changed else 'Verified'} {len(ALL_PINMUX)} "
          f"GPIO pins ({changed} changed) — {len(MOTOR_PINMUX)} motor + {len(SONAR_PINMUX)} sonar")


if __name__ == "__main__":
    fix_pinmux()
