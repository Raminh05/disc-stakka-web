"""Job model for long-running hardware operations.

Every carousel operation is slow, physical, and needs the user to act partway
through ("insert the disc now", "take the disc"). The PS3 browser has no usable
AJAX, so the web layer cannot stream progress. Instead each operation becomes a
server-side job with an observable phase, and the browser polls it with
``<meta http-equiv="refresh">`` - a pattern that works on every browser ever
shipped.

A job runs to completion on the device worker thread. The web layer only ever
reads :meth:`Job.snapshot`.
"""

import itertools
import threading
import time

# Phases. `awaiting_*` are the ones where the user has to physically do
# something; they carry a deadline so the page can show a countdown.
MOVING = "moving"
AWAITING_INSERT = "awaiting_insert"
INGESTING = "ingesting"
PRESENTED = "presented"
PARKING = "parking"
RETRACTING = "retracting"
DONE = "done"
FAILED = "failed"

TERMINAL = frozenset((DONE, FAILED))

#: Phases where we are waiting on the human, not the machine. The job page
#: shows these as a prompt rather than a progress message.
PROMPTING = frozenset((AWAITING_INSERT, PRESENTED))

_ids = itertools.count(1)


class Job(object):
    """One hardware operation in flight.

    All mutation goes through the lock because the worker thread writes while
    request threads read.
    """

    def __init__(self, kind, title, disc_id=None, slot=None):
        self.id = "%d-%d" % (int(time.time()), next(_ids))
        self.kind = kind  # 'add' | 'eject' | 'reset' | 'return'
        self.title = title  # human summary, e.g. "Eject slot 18"
        self.disc_id = disc_id
        self.slot = slot

        self._lock = threading.Lock()
        self._phase = MOVING
        self._message = "Starting..."
        self._deadline = None  # monotonic seconds, for countdown phases
        self._error = None
        self._result = {}
        self.created_at = time.time()

    # -- worker side -----------------------------------------------------

    def set_phase(self, phase, message, window_s=None):
        with self._lock:
            self._phase = phase
            self._message = message
            self._deadline = (time.monotonic() + window_s) if window_s else None

    def note(self, message):
        """Update the message without changing phase (progress callback)."""
        with self._lock:
            self._message = message

    def succeed(self, message, **result):
        with self._lock:
            self._phase = DONE
            self._message = message
            self._deadline = None
            self._result.update(result)

    def fail(self, error, **result):
        with self._lock:
            self._phase = FAILED
            self._message = error
            self._error = error
            self._deadline = None
            self._result.update(result)

    # -- web side --------------------------------------------------------

    def snapshot(self):
        """A consistent read of the whole job, safe to hand to a template."""
        with self._lock:
            remaining = None
            if self._deadline is not None:
                remaining = max(0, int(round(self._deadline - time.monotonic())))
            return {
                "id": self.id,
                "kind": self.kind,
                "title": self.title,
                "disc_id": self.disc_id,
                "slot": self.slot,
                "phase": self._phase,
                "message": self._message,
                "remaining": remaining,
                "error": self._error,
                "result": dict(self._result),
                "done": self._phase in TERMINAL,
                "ok": self._phase == DONE,
                "prompting": self._phase in PROMPTING,
            }

    @property
    def done(self):
        with self._lock:
            return self._phase in TERMINAL


class JobRegistry(object):
    """Keeps recent jobs addressable by id so a polling page can find them."""

    def __init__(self, keep=40):
        self._lock = threading.Lock()
        self._jobs = {}
        self._order = []
        self._keep = keep

    def add(self, job):
        with self._lock:
            self._jobs[job.id] = job
            self._order.append(job.id)
            while len(self._order) > self._keep:
                self._jobs.pop(self._order.pop(0), None)

    def get(self, job_id):
        with self._lock:
            return self._jobs.get(job_id)
