"""The USB side of talking to a Disc Stakka.

Everything hidapi-specific lives here so that :mod:`discstakka.protocol` is pure
wire logic. The protocol layer holds one of these and calls five methods on it,
which is also the whole interface a simulated unit has to implement.
"""

VID = 0x0718
PID = 0xD000


class HidTransport(object):
    """Talks to the real unit through hidapi.

    ``hid`` is imported inside the methods that need it rather than at module
    scope: the test suite and the catalogue never reach this far, and importing
    it eagerly would make hidapi a hard requirement for both.
    """

    def __init__(self):
        self._dev = None

    @staticmethod
    def present():
        try:
            import hid

            return bool(hid.enumerate(VID, PID))
        except Exception:
            return False

    def open(self):
        # Surfaced as OSError so the caller turns it into the same "cannot open"
        # message as any other failure to reach the unit, rather than an
        # unexpected-error traceback on the device page.
        try:
            import hid
        except ImportError as exc:
            raise OSError("hidapi is not installed: %s" % exc) from exc

        # Refresh hidapi's device list. After a sleep/wake cycle the cached
        # entry points at a device that no longer exists, and every open on
        # this handle fails forever even though the unit is healthy.
        try:
            hid.enumerate(VID, PID)
        except Exception:
            pass

        dev = hid.device()
        dev.open(VID, PID)
        self._dev = dev

    def read(self, size, timeout_ms):
        return self._dev.read(size, timeout_ms)

    def write(self, buf):
        return self._dev.write(buf)

    def close(self):
        dev, self._dev = self._dev, None
        if dev is not None:
            try:
                dev.close()
            except Exception:
                pass
