"""Device ownership.

The Disc Stakka does exactly one thing at a time and silently ignores anything
sent while it is BUSY, so all access is funnelled through a single worker thread
that owns the HID handle. Web requests never touch the device; they submit a
:class:`~discstakka.jobs.Job` and poll it.

This is why the app must run **single-process**. The handle and the job registry
live in memory, so a multi-worker WSGI server would give each worker its own
device and they would fight over it.

What a job actually does lives in :mod:`discstakka.flows`.
"""

import threading
import time
import traceback

from . import jobs
from .catalog import db
from .protocol import DeviceError, DiscStakka, NotConnected, describe_status
from .trace import NullTrace, Trace


class Busy(Exception):
    """Raised when a job is submitted while another is still running."""

    def __init__(self, job):
        Exception.__init__(self, "device is busy")
        self.job = job


class DeviceController(object):
    """Owns the unit, and runs one job at a time against it."""

    def __init__(self, ds=None, db_path=None, trace_dir=None):
        self._ds = ds if ds is not None else DiscStakka()
        self._db_path = db_path
        self._trace_dir = trace_dir
        self._lock = threading.Lock()
        #: Held by whoever is using the unit: a job, a probe, or the keeper.
        #: Taken after _lock, never before it.
        self._device = threading.Lock()
        self._current = None
        self.registry = jobs.JobRegistry()

    # -- introspection ---------------------------------------------------

    @property
    def current(self):
        with self._lock:
            job = self._current
        if job is not None and job.done:
            return None
        return job

    def info(self):
        """Header/status summary. Never raises - the UI must render regardless."""
        job = self.current
        out = {
            "present": self._ds.present(),
            "connected": self._ds.connected,
            "serial": None,
            "firmware": None,
            "busy_job": job.id if job else None,
            "error": None,
        }
        if self._ds.connected:
            out["serial"] = "%08x" % self._ds.serial if self._ds.serial else None
            out["firmware"] = self._ds.firmware
        return out

    def reconnect(self):
        with self._lock:
            if self._current is not None and not self._current.done:
                raise Busy(self._current)
            with self._device:
                self._ds.reconnect()

    def probe(self):
        """One-shot status read for the diagnostics page."""
        with self._lock, self._device:
            if self._current is not None and not self._current.done:
                raise Busy(self._current)
            self._ds.open()
            return {
                "serial": "%08x" % self._ds.serial,
                "firmware": self._ds.firmware,
                "position": self._ds.position(),
                "status": describe_status(self._ds.status()),
            }

    # -- keeping the unit alive ------------------------------------------

    def keep_open(self, interval_s=0.5):
        """Hold the unit open whenever it is on the bus and no job has it.

        The unit resets itself about every 2.5 s unless the host polls it and
        answers its 0xCC. macOS polls every HID device from attach, so opening
        on the first job was enough there. Linux's usbhid only polls a device
        something has open, so a unit attached to a Linux host cycled through
        disconnect and re-enumerate until the next job, and again whenever its
        handle went stale. An open handle is polled by hidapi's read thread.
        """
        thread = threading.Thread(
            target=self._keep_open, args=(interval_s,), name="keep-open", daemon=True
        )
        thread.start()

    def _keep_open(self, interval_s):
        while True:
            self.tend()
            time.sleep(interval_s)

    def tend(self):
        """One pass of keep_open. Never waits for a job, and never raises."""
        if self.current is not None:
            return
        if not self._device.acquire(blocking=False):
            return
        try:
            if self._ds.connected and self._ds.alive():
                return
            if self._ds.present():
                self._ds.open()
        except DeviceError:
            pass
        except Exception:  # pragma: no cover - keep the keeper alive
            traceback.print_exc()
        finally:
            self._device.release()

    # -- submission ------------------------------------------------------

    def submit(self, job, runner, *args):
        with self._lock:
            if self._current is not None and not self._current.done:
                raise Busy(self._current)
            self._current = job
        self.registry.add(job)
        thread = threading.Thread(
            target=self._run, args=(job, runner, args), daemon=True
        )
        thread.start()
        return job

    def _trace_for(self, job):
        if self._trace_dir is None:
            return NullTrace()
        try:
            return Trace(self._trace_dir, job)
        except OSError:
            return NullTrace()  # evidence is never a reason to fail the job

    def _run(self, job, runner, args):
        with self._device:
            self._run_owned(job, runner, args)

    def _run_owned(self, job, runner, args):
        conn = None
        trace = self._trace_for(job)
        try:
            self._ds.open()
            self._ds.trace = trace.status
            conn = db.connect(self._db_path)
            runner(self._ds, conn, job, trace, *args)
        except NotConnected as exc:
            job.fail("Lost the Disc Stakka: %s" % exc)
        except DeviceError as exc:
            job.fail(str(exc))
        except Exception as exc:  # pragma: no cover - last-resort guard
            traceback.print_exc()
            job.fail("Unexpected error: %s" % exc)
        finally:
            self._ds.trace = None
            trace.mark("end")
            trace.close()
            if conn is not None:
                conn.close()
            if not job.done:
                job.fail("Job ended without reporting a result")
