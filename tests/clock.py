"""A virtual clock, so the suite can exercise the real timeouts instantly.

protocol.py and device.py both read ``time.monotonic()`` through a module-level
``import time``, so replacing that attribute governs every deadline in them. The
simulated unit advances the same clock as it is polled, which means the real
SETTLE_MS, INSERT_WINDOW_MS and TAKE_WINDOW_MS run to completion in no wall time
at all and never depend on how fast the machine is.
"""

import contextlib


class Clock(object):
    """Stands in for the ``time`` module. Only moves when something moves it."""

    def __init__(self, start=1_000.0):
        self._t = start

    def monotonic(self):
        return self._t

    def time(self):
        return self._t

    def sleep(self, seconds):
        self._t += seconds

    def advance_ms(self, ms):
        self._t += ms / 1000.0

    @property
    def ms(self):
        return int(round(self._t * 1000))


@contextlib.contextmanager
def virtual_clock(*modules):
    """Swap ``module.time`` for a Clock in each module, for the duration."""
    clock = Clock()
    saved = [(m, m.time) for m in modules]
    for m in modules:
        m.time = clock
    try:
        yield clock
    finally:
        for m, original in saved:
            m.time = original
