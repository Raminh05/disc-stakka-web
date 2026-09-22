"""Device ownership and the hardware flows.

The Disc Stakka does exactly one thing at a time and silently ignores anything
sent while it is BUSY, so all access is funnelled through a single worker
thread that owns the HID handle. Web requests never touch the device; they
submit a :class:`~discstakka.jobs.Job` and poll it.

This is why the app must run **single-process**. The handle and the job
registry live in memory, so a multi-worker WSGI server would give each worker
its own device and they would fight over it.
"""

import os
import threading
import time
import traceback

from catalog import db

from . import jobs
from .protocol import DiscStakka, DeviceError, NotConnected, describe_status

TRACE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "traces")


class _Trace(object):
    """Records every status observation during a flow, to a file per job.

    The load sequence depends on firmware state transitions that are only
    partly documented, so when a run misbehaves the status stream is the only
    evidence of what the unit actually did. Cheap to keep on permanently.
    """

    def __init__(self, job):
        self.path = os.path.join(TRACE_DIR, "%s-%s.log" % (job.kind, job.id))
        self.t0 = time.monotonic()
        self.last = None
        os.makedirs(TRACE_DIR, exist_ok=True)
        self.fh = open(self.path, "w")
        self.fh.write("# %s  slot=%s\n" % (job.title, job.slot))

    def status(self, st):
        if st != self.last:
            self.fh.write("%7.2fs  %s\n"
                          % (time.monotonic() - self.t0, describe_status(st)))
            self.fh.flush()
            self.last = st

    def mark(self, label):
        self.fh.write("%7.2fs  -- %s\n" % (time.monotonic() - self.t0, label))
        self.fh.flush()

    def close(self):
        try:
            self.fh.close()
        except Exception:
            pass


class Busy(Exception):
    """Raised when a job is submitted while another is still running."""

    def __init__(self, job):
        Exception.__init__(self, "device is busy")
        self.job = job


class DeviceController(object):
    def __init__(self, db_path=None):
        self._ds = DiscStakka()
        self._db_path = db_path
        self._lock = threading.Lock()
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
            self._ds.reconnect()

    def probe(self):
        """One-shot status read for the diagnostics page."""
        with self._lock:
            if self._current is not None and not self._current.done:
                raise Busy(self._current)
            self._ds.open()
            return {
                "serial": "%08x" % self._ds.serial,
                "firmware": self._ds.firmware,
                "position": self._ds.position(),
                "status": describe_status(self._ds.status()),
            }

    # -- submission ------------------------------------------------------

    def submit(self, job, runner, *args):
        with self._lock:
            if self._current is not None and not self._current.done:
                raise Busy(self._current)
            self._current = job
        self.registry.add(job)
        thread = threading.Thread(
            target=self._run, args=(job, runner, args), daemon=True)
        thread.start()
        return job

    def _run(self, job, runner, args):
        conn = None
        try:
            self._ds.open()
            conn = db.connect(self._db_path)
            runner(self._ds, conn, job, *args)
        except NotConnected as exc:
            job.fail("Lost the Disc Stakka: %s" % exc)
        except DeviceError as exc:
            job.fail(str(exc))
        except Exception as exc:  # pragma: no cover - last-resort guard
            traceback.print_exc()
            job.fail("Unexpected error: %s" % exc)
        finally:
            if conn is not None:
                conn.close()
            if not job.done:
                job.fail("Job ended without reporting a result")


# -- flows ---------------------------------------------------------------
#
# Each runner blocks on the worker thread, updating job phase as it goes. The
# web layer polls Job.snapshot() and renders whatever phase it finds.


def run_reset(ds, conn, job):
    job.set_phase(jobs.MOVING, "Resetting the unit...")
    ds.reset(progress=job.note)
    job.succeed("Unit reset. Carousel is at home.")


def run_eject(ds, conn, job, disc_id):
    disc = db.get_disc(conn, disc_id)
    if disc is None:
        job.fail("That disc is no longer in the catalogue.")
        return
    slot = disc["slot"]

    job.set_phase(jobs.MOVING, "Fetching slot %d..." % slot)
    ds.move_to(slot, eject=True, progress=job.note)

    # With a working sensor this tells us the disc really made it to the bay.
    if not ds.disc_in_bay():
        job.set_phase(jobs.PARKING, "Nothing came out; returning to home...")
        ds.park()
        db.log_event(conn, "failed", disc_id, slot, "eject found no disc")
        conn.commit()
        job.fail("Slot %d appears to be empty. The catalogue may be out of "
                 "step with the carousel - try Reconcile." % slot)
        return

    job.set_phase(jobs.PRESENTED, "Take the disc from the unit.",
                  window_s=5)

    if not ds.wait_for_take():
        job.set_phase(jobs.RETRACTING, "Not taken - putting it back...")
        ds.retract(progress=job.note)
        ds.park()
        db.log_event(conn, "retracted", disc_id, slot)
        conn.commit()
        job.fail("The disc was not taken in time, so the unit was told to "
                 "take it back into slot %d. The catalogue has not changed."
                 % slot)
        return

    job.set_phase(jobs.PARKING, "Returning to home...")
    ds.park()
    db.mark_out(conn, disc_id)
    job.succeed("A disc was taken from slot %d. The catalogue now lists %s "
                "as checked out and holds the slot for its return."
                % (slot, disc["title"]), disc_id=disc_id)


def run_add(ds, conn, job, slot, disc_id=None):
    """Load a disc into ``slot``.

    ``disc_id`` set means this is a return: the disc is already catalogued and
    its slot was reserved. Otherwise a placeholder row is created once the disc
    is physically in, so the catalogue never loses track of it even if the user
    abandons the metadata form.
    """
    returning = disc_id is not None
    trace = _Trace(job)
    ds.trace = trace.status
    try:
        job.set_phase(jobs.MOVING, "Moving to slot %d..." % slot)
        trace.mark("move to slot %d" % slot)
        ds.move_to(slot, progress=job.note)

        job.set_phase(jobs.AWAITING_INSERT, "Insert the disc now.", window_s=30)
        trace.mark("awaiting insert (watching DISC_WAITING)")
        if not ds.wait_for_insert():
            job.set_phase(jobs.PARKING, "Timed out; returning to home...")
            ds.park()
            job.fail("No disc went in within 30 seconds. Nothing has changed.")
            return

        job.set_phase(jobs.INGESTING, "Taking the disc in...")
        trace.mark("disc seen; settling before 0x1D")
        ds.ingest(progress=job.note)
        trace.mark("0x1D acknowledged")

        job.set_phase(jobs.PARKING, "Returning to home...")
        ds.park()
        trace.mark("parked; watching for 10s to see if it comes back out")

        # The unit has been spitting discs back out after a nominally
        # successful load. Keep watching so the trace captures it.
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            trace.status(ds.status())
    finally:
        ds.trace = None
        trace.mark("end")
        trace.close()

    if returning:
        db.mark_stored(conn, disc_id)
        disc = db.get_disc(conn, disc_id)
        job.succeed("%s is back in slot %d."
                    % (disc["title"] if disc else "Disc", slot),
                    disc_id=disc_id)
    else:
        new_id = db.create_disc(conn, slot, title="Untitled disc (slot %d)" % slot)
        job.succeed("Disc loaded into slot %d. Now give it a name." % slot,
                    disc_id=new_id, needs_details=True)
