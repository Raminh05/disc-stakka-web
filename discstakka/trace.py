"""Per-job record of what the unit actually reported.

The load sequence depends on firmware state transitions that are only partly
documented, so when a run misbehaves the status stream is the only evidence of
what happened. Cheap to keep on permanently.
"""

import os
import time

from .protocol import describe_status


class Trace(object):
    def __init__(self, directory, job):
        self.path = os.path.join(directory, "%s-%s.log" % (job.kind, job.id))
        self.t0 = time.monotonic()
        self.last = None
        os.makedirs(directory, exist_ok=True)
        self.fh = open(self.path, "w", encoding="utf-8")
        self._write("# %s  slot=%s\n" % (job.title, job.slot))

    def _write(self, line):
        # Evidence is never a reason to fail a job. A full disk, or a title
        # the file cannot take, must not stop the runner reaching a terminal
        # phase - and the controller marks the end after the job's own guard.
        try:
            self.fh.write(line)
            self.fh.flush()
        except (OSError, ValueError):
            pass

    def status(self, st):
        if st != self.last:
            self._write(
                "%7.2fs  %s\n" % (time.monotonic() - self.t0, describe_status(st))
            )
            self.last = st

    def mark(self, label):
        self._write("%7.2fs  -- %s\n" % (time.monotonic() - self.t0, label))

    def close(self):
        try:
            self.fh.close()
        except Exception:
            pass


class NullTrace(object):
    """Records nothing. For callers that have nowhere to write."""

    path = None

    def status(self, st):
        pass

    def mark(self, label):
        pass

    def close(self):
        pass
