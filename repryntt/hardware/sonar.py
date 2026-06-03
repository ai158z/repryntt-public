"""
repryntt.hardware.sonar — HC-SR04 Ultrasonic Distance Sensors.

Two HC-SR04 sensors providing distance measurements:
    - Front sensor: alongside the Waveshare IMX219-83 stereo camera
    - Rear sensor: covering the blind spot behind the tank

How it works:
    1. Send a 10µs HIGH pulse on TRIGGER pin
    2. Sensor emits 8x 40kHz ultrasonic bursts
    3. ECHO pin goes HIGH for the duration of the round-trip
    4. Distance = (echo_duration * speed_of_sound) / 2

Range: 2cm - 400cm, accuracy: ~3mm
Beam angle: ~15° cone

⚠️ VOLTAGE WARNING: HC-SR04 ECHO outputs 5V. Jetson GPIO is 3.3V.
A voltage divider (1kΩ + 2kΩ) on each ECHO line is recommended.
Without it, the Jetson GPIO may be damaged over time.

Wiring (BOARD pin numbering):
    Front HC-SR04:
        VCC  → Pin 2 or 4 (5V)
        TRIG → Pin 24
        ECHO → Pin 26
        GND  → Pin 20 or 25

    Rear HC-SR04:
        VCC  → Pin 2 or 4 (5V)
        TRIG → Pin 11
        ECHO → Pin 23
        GND  → nearby GND pin
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

# ── GPIO conditional import ──────────────────────────────────────────

try:
    import Jetson.GPIO as GPIO
    GPIO_AVAILABLE = True
except ImportError:
    GPIO_AVAILABLE = False

# ── Pin assignments (BOARD numbering) ────────────────────────────────

# Front HC-SR04 (with the stereo camera)
FRONT_TRIG = 24   # SPI_CS0 — used as GPIO output
FRONT_ECHO = 26   # SPI_CS1 — used as GPIO input

# Rear HC-SR04 (behind the tank)
REAR_TRIG = 11    # GPIO — output
REAR_ECHO = 23    # SPI_CLK — used as GPIO input

# ── Constants ────────────────────────────────────────────────────────

SPEED_OF_SOUND_CM_S = 34300  # cm/s at ~20°C
MAX_DISTANCE_CM = 400        # HC-SR04 max range
TIMEOUT_S = 0.03             # 30ms = ~500cm round-trip (generous)
MIN_DISTANCE_CM = 2          # anything closer is unreliable


@dataclass
class SonarReading:
    """Single distance reading from an ultrasonic sensor."""
    sensor: str           # "front" or "rear"
    distance_cm: float    # measured distance, -1 if failed
    valid: bool           # True if reading is within range
    timestamp: float      # time.time() when read


class SonarController:
    """HC-SR04 ultrasonic sensor driver for Jetson GPIO.

    Manages front and rear ultrasonic distance sensors. Each measurement
    takes ~10-30ms depending on distance. Thread-safe.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._initialized = False
        self._front_ok = False
        self._rear_ok = False

    def initialize(self) -> bool:
        """Set up GPIO pins for both HC-SR04 sensors."""
        if not GPIO_AVAILABLE:
            logger.warning("Sonar: Jetson.GPIO not available")
            return False

        if self._initialized:
            return True

        try:
            # GPIO mode should already be set by tank controller,
            # but set it if not
            try:
                GPIO.setmode(GPIO.BOARD)
            except ValueError:
                pass  # already set
            GPIO.setwarnings(False)

            # Front sensor
            try:
                GPIO.setup(FRONT_TRIG, GPIO.OUT, initial=GPIO.LOW)
                GPIO.setup(FRONT_ECHO, GPIO.IN)
                self._front_ok = True
                logger.info(f"Sonar: front sensor ready (TRIG={FRONT_TRIG}, ECHO={FRONT_ECHO})")
            except Exception as e:
                logger.warning(f"Sonar: front sensor init failed: {e}")

            # Rear sensor
            try:
                GPIO.setup(REAR_TRIG, GPIO.OUT, initial=GPIO.LOW)
                GPIO.setup(REAR_ECHO, GPIO.IN)
                self._rear_ok = True
                logger.info(f"Sonar: rear sensor ready (TRIG={REAR_TRIG}, ECHO={REAR_ECHO})")
            except Exception as e:
                logger.warning(f"Sonar: rear sensor init failed: {e}")

            self._initialized = self._front_ok or self._rear_ok

            # Let pins settle
            time.sleep(0.1)
            return self._initialized

        except Exception as e:
            logger.error(f"Sonar init failed: {e}")
            return False

    def _ensure_init(self) -> bool:
        """Lazy-initialize on first use."""
        if not self._initialized:
            return self.initialize()
        return True

    def _read_sensor(self, trig_pin: int, echo_pin: int) -> float:
        """Take a single distance reading from one HC-SR04.

        Returns distance in cm, or -1 on failure.
        """
        # 1. Send 10µs trigger pulse
        GPIO.output(trig_pin, GPIO.LOW)
        time.sleep(0.002)  # ensure clean LOW
        GPIO.output(trig_pin, GPIO.HIGH)
        time.sleep(0.00001)  # 10µs pulse
        GPIO.output(trig_pin, GPIO.LOW)

        # 2. Wait for ECHO to go HIGH (start of return pulse)
        start = time.time()
        timeout = start + TIMEOUT_S
        while GPIO.input(echo_pin) == GPIO.LOW:
            start = time.time()
            if start > timeout:
                return -1  # no echo received

        # 3. Wait for ECHO to go LOW (end of return pulse)
        end = time.time()
        timeout2 = end + TIMEOUT_S
        while GPIO.input(echo_pin) == GPIO.HIGH:
            end = time.time()
            if end > timeout2:
                return -1  # echo stuck high

        # 4. Calculate distance
        duration = end - start
        distance = (duration * SPEED_OF_SOUND_CM_S) / 2.0

        if distance < MIN_DISTANCE_CM or distance > MAX_DISTANCE_CM:
            return -1

        return round(distance, 1)

    def read_front(self, samples: int = 3) -> SonarReading:
        """Read front HC-SR04. Takes multiple samples and returns median."""
        if not self._ensure_init() or not self._front_ok:
            return SonarReading("front", -1, False, time.time())

        with self._lock:
            readings = []
            for _ in range(samples):
                d = self._read_sensor(FRONT_TRIG, FRONT_ECHO)
                if d > 0:
                    readings.append(d)
                time.sleep(0.01)  # 10ms between pings (avoid crosstalk)

        if readings:
            readings.sort()
            median = readings[len(readings) // 2]
            return SonarReading("front", median, True, time.time())
        return SonarReading("front", -1, False, time.time())

    def read_rear(self, samples: int = 3) -> SonarReading:
        """Read rear HC-SR04. Takes multiple samples and returns median."""
        if not self._ensure_init() or not self._rear_ok:
            return SonarReading("rear", -1, False, time.time())

        with self._lock:
            readings = []
            for _ in range(samples):
                d = self._read_sensor(REAR_TRIG, REAR_ECHO)
                if d > 0:
                    readings.append(d)
                time.sleep(0.01)

        if readings:
            readings.sort()
            median = readings[len(readings) // 2]
            return SonarReading("rear", median, True, time.time())
        return SonarReading("rear", -1, False, time.time())

    def read_both(self) -> Dict[str, SonarReading]:
        """Read front and rear sensors. Returns dict with both readings."""
        front = self.read_front()
        time.sleep(0.02)  # prevent ultrasonic crosstalk between sensors
        rear = self.read_rear()
        return {"front": front, "rear": rear}

    def scan(self) -> Dict[str, Any]:
        """Full sensor scan — human-readable status and distances."""
        both = self.read_both()
        result = {
            "front_cm": both["front"].distance_cm if both["front"].valid else None,
            "rear_cm": both["rear"].distance_cm if both["rear"].valid else None,
            "front_valid": both["front"].valid,
            "rear_valid": both["rear"].valid,
        }

        # Interpret distances
        for direction in ["front", "rear"]:
            d = result[f"{direction}_cm"]
            if d is None:
                result[f"{direction}_status"] = "no_reading"
            elif d < 10:
                result[f"{direction}_status"] = "DANGER_very_close"
            elif d < 30:
                result[f"{direction}_status"] = "close"
            elif d < 100:
                result[f"{direction}_status"] = "moderate"
            else:
                result[f"{direction}_status"] = "clear"

        return result

    def status(self) -> Dict[str, Any]:
        """Get sensor hardware status."""
        return {
            "initialized": self._initialized,
            "front_sensor": self._front_ok,
            "rear_sensor": self._rear_ok,
            "gpio_available": GPIO_AVAILABLE,
            "pins": {
                "front_trig": FRONT_TRIG,
                "front_echo": FRONT_ECHO,
                "rear_trig": REAR_TRIG,
                "rear_echo": REAR_ECHO,
            },
            "range_cm": f"{MIN_DISTANCE_CM}-{MAX_DISTANCE_CM}",
            "beam_angle_deg": 15,
        }


# ── Singleton ────────────────────────────────────────────────────────

_sonar: Optional[SonarController] = None


def get_sonar() -> SonarController:
    """Get or create the global SonarController instance."""
    global _sonar
    if _sonar is None:
        _sonar = SonarController()
    return _sonar
