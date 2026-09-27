"""The USB side of talking to a Disc Stakka.

Everything hidapi-specific lives here so that :mod:`discstakka.protocol` is pure
wire logic. The protocol layer holds one of these and calls five methods on it,
which is also the whole interface a simulated unit has to implement.
"""

import queue
import threading

VID = 0x0718
PID = 0xD000


class _HidapiThread(object):
    """The one thread that imports hidapi, enumerates, opens and closes.

    hidapi's macOS backend keeps a process-global IOHIDManager on the run loop
    of whichever thread first calls it. Under the threaded server that was a
    request thread, gone a moment later, and the next device IOKit had not seen
    was scheduled on its dead run loop: SIGTRAP in CFRunLoopAddSource, mid-job.
    hid_exit() at shutdown trapped the same way.

    A daemon thread, not a ThreadPoolExecutor: executor workers are joined
    before atexit runs hid_exit(), which would leave it the same dead run loop.
    Reads and writes stay on the caller; each open device has its own thread.
    """

    def __init__(self):
        self._calls = queue.Queue()
        self._lock = threading.Lock()
        self._thread = None

    def call(self, fn, *args):
        with self._lock:
            if self._thread is None:
                self._thread = threading.Thread(
                    target=self._run, name="hidapi", daemon=True
                )
                self._thread.start()
        done = threading.Event()
        outcome = {}
        self._calls.put((fn, args, outcome, done))
        done.wait()
        if "error" in outcome:
            raise outcome["error"]
        return outcome["result"]

    def _run(self):
        while True:
            fn, args, outcome, done = self._calls.get()
            try:
                outcome["result"] = fn(*args)
            except BaseException as exc:
                outcome["error"] = exc
            done.set()


_hidapi = _HidapiThread()


# `import hid` initialises the manager, so these import it on the hidapi thread.
# Imported on a request thread, the first import alone was enough to crash.


def _enumerate():
    import hid

    return hid.enumerate(VID, PID)


def _open():
    # Surfaced as OSError so the caller turns it into the same "cannot open"
    # message as any other failure to reach the unit, rather than an
    # unexpected-error traceback on the device page.
    try:
        import hid
    except ImportError as exc:
        raise OSError("hidapi is not installed: %s" % exc) from exc

    # Refresh hidapi's device list. After a sleep/wake cycle the cached entry
    # points at a device that no longer exists, and every open on this handle
    # fails forever even though the unit is healthy.
    try:
        hid.enumerate(VID, PID)
    except Exception:
        pass

    dev = hid.device()
    dev.open(VID, PID)
    return dev


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
            return bool(_hidapi.call(_enumerate))
        except Exception:
            return False

    def open(self):
        self._dev = _hidapi.call(_open)

    def read(self, size, timeout_ms):
        return self._dev.read(size, timeout_ms)

    def write(self, buf):
        return self._dev.write(buf)

    def close(self):
        dev, self._dev = self._dev, None
        if dev is not None:
            try:
                _hidapi.call(dev.close)
            except Exception:
                pass
