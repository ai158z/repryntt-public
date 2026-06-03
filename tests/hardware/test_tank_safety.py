"""Pin the Phase-7 kill-switch fix: SIGINT must run _stop_all() before
re-raising KeyboardInterrupt, so a Ctrl-C mid-move doesn't leave the
H-bridge driven HIGH and the tracks spinning until brown-out.
"""

from __future__ import annotations

import os
import signal

import pytest

from repryntt.hardware.tank import get_tank_controller


def test_safety_handlers_install_on_singleton_creation():
    get_tank_controller()
    from repryntt.hardware import tank as _tank_mod
    assert _tank_mod._safety_installed is True


def test_sigint_still_raises_keyboard_interrupt():
    """Our handler must chain to the previous one (SIG_DFL → KeyboardInterrupt)
    so caller code that wraps `try: ... except KeyboardInterrupt:` still
    works."""
    get_tank_controller()
    with pytest.raises(KeyboardInterrupt):
        os.kill(os.getpid(), signal.SIGINT)


def test_sigint_handler_is_ours():
    get_tank_controller()
    h = signal.getsignal(signal.SIGINT)
    # Our handler is a closure named "_h" inside _install_safety_handlers.
    assert callable(h) and getattr(h, "__name__", "") == "_h"
