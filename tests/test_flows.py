"""The eject, add and reset flows, against a simulated unit.

The flows run on the test thread rather than through submit(), so the virtual
clock advances in one place and nothing races. Threading and ownership are
covered separately in ControllerTest.
"""

import contextlib
import io
import os
import re
import shutil
import tempfile
import unittest

from catalog import db
from discstakka import device, flows, jobs, protocol
from discstakka import trace as trace_module
from discstakka.trace import Trace
from tests import fake_device as fake
from tests.clock import virtual_clock

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
STATUS_LINE = re.compile(r"^\s*[\d.]+s\s+([0-9a-f]{4}) \[")

#: Decoded flag names in the order describe_status emits them, minus BUSY and
#: the undecoded 0x4000. Two runs of the same operation agree on this even when
#: their timings and blip counts do not.
SIGNIFICANT = ((protocol.ST_DISC_WAITING, "DISC_WAITING"),
               (protocol.ST_NEW_DISC_ACK, "NEW_DISC_ACK"),
               (protocol.ST_DISC_IN_BAY, "DISC_IN_BAY"),
               (protocol.ST_ACK_TIMEOUT, "ACK_TIMEOUT"),
               (protocol.ST_HOMED, "HOMED"))


def signature(path):
    """The ordered flag-sets a trace passes through, consecutive dups collapsed."""
    out = []
    with open(path) as fh:
        for line in fh:
            match = STATUS_LINE.match(line)
            if not match:
                continue
            word = int(match.group(1), 16)
            names = tuple(n for bit, n in SIGNIFICANT if word & bit)
            if not out or out[-1] != names:
                out.append(names)
    return tuple(out)


class FlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="discstakka-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

        self.traces = os.path.join(self.tmp, "traces")

        self.db_path = os.path.join(self.tmp, "catalog.db")
        db.init(self.db_path)
        self.conn = db.connect(self.db_path)
        self.addCleanup(self.conn.close)

        ctx = virtual_clock(protocol, flows, trace_module)
        self.clock = ctx.__enter__()
        self.addCleanup(ctx.__exit__, None, None, None)

        self.io = fake.SimulatedTransport(self.clock)
        self.unit = self.io.carousel
        self.ds = protocol.DiscStakka(self.io)
        self.ds.open()
        self.ds.ensure_homed()

    # -- helpers ---------------------------------------------------------

    def job(self, kind, title, when=None, **kw):
        """A job that records its phases, and optionally acts on one.

        ``when={jobs.AWAITING_INSERT: 2_000}`` feeds a disc in two simulated
        seconds after the page asks for one, which is when a person would.
        """
        job = jobs.Job(kind, title, **kw)
        job.phases = []
        original = job.set_phase
        actions = {jobs.AWAITING_INSERT: self.unit.insert_disc,
                   jobs.PRESENTED: self.unit.take_disc}

        def record(phase, message, window_s=None):
            job.phases.append(phase)
            original(phase, message, window_s)
            if when and phase in when:
                actions[phase](at_ms=self.clock.ms + when[phase])

        job.set_phase = record
        job.trace = Trace(self.traces, job)
        self.addCleanup(job.trace.close)
        self.ds.trace = job.trace.status
        return job

    def disc(self, slot, title="Test disc", status=db.STORED):
        disc_id = db.create_disc(self.conn, slot, title)
        if status == db.OUT:
            db.mark_out(self.conn, disc_id)
        return disc_id

    def events(self):
        return [(r["kind"], r["detail"]) for r in db.recent_events(self.conn, 50)]

    def only_trace(self):
        names = os.listdir(self.traces)
        self.assertEqual(len(names), 1, "expected exactly one trace file")
        return os.path.join(self.traces, names[0])


class Reset(FlowTest):
    def test_clears_errors_before_waiting_for_the_unit_to_settle(self):
        # The clear-error pair goes first on purpose: gating it behind a busy
        # wait means a latched error stops the only thing that would clear it.
        job = self.job("reset", "Reset the unit")
        self.unit.ack_timeout = True
        flows.run_reset(self.ds, self.conn, job, job.trace)

        sent = [c for c, _ in self.unit.log]
        first_wait = sent.index(fake.CMD_SET_LED)
        self.assertIn(fake.CMD_CLEAR_ERR_1, sent[:first_wait])
        self.assertIn(fake.CMD_CLEAR_ERR_2, sent[:first_wait])
        self.assertFalse(self.unit.ack_timeout)

    def test_parks_at_home_and_succeeds(self):
        job = self.job("reset", "Reset the unit")
        flows.run_reset(self.ds, self.conn, job, job.trace)
        self.assertEqual(self.unit.position, protocol.HOME)
        self.assertTrue(job.snapshot()["ok"])


class Add(FlowTest):
    def test_a_disc_goes_in_and_is_catalogued(self):
        job = self.job("add", "Load a disc into slot 5", slot=5,
                       when={jobs.AWAITING_INSERT: 3_000})
        flows.run_add(self.ds, self.conn, job, job.trace, 5)

        snap = job.snapshot()
        self.assertTrue(snap["ok"], snap["message"])
        self.assertEqual(job.phases, [jobs.MOVING, jobs.AWAITING_INSERT,
                                      jobs.INGESTING, jobs.PARKING])
        self.assertIn(5, self.unit.occupied)
        self.assertEqual(self.unit.position, protocol.HOME)

        row = db.get_disc(self.conn, snap["result"]["disc_id"])
        self.assertEqual(row["slot"], 5)
        self.assertEqual(row["title"], "Untitled disc (slot 5)")
        self.assertTrue(snap["result"]["needs_details"])

    def test_nothing_inserted_leaves_the_catalogue_alone(self):
        job = self.job("add", "Load a disc into slot 9", slot=9)
        flows.run_add(self.ds, self.conn, job, job.trace, 9)

        snap = job.snapshot()
        self.assertFalse(snap["ok"])
        self.assertIn("No disc went in within 30 seconds", snap["error"])
        self.assertEqual(db.free_slots(self.conn)[:1], [1])
        self.assertIsNone(db.get_disc(self.conn, 1))
        self.assertEqual(self.unit.position, protocol.HOME)

    def test_the_accept_command_is_never_sent_while_the_unit_is_busy(self):
        # Sent early it is silently dropped, the firmware times out waiting to
        # be told what to do, and the disc comes back out.
        job = self.job("add", "Load a disc into slot 2", slot=2,
                       when={jobs.AWAITING_INSERT: 2_000})

        busy_at_accept = []
        real_handle = self.unit.handle

        def watch(msgid, cmd, args):
            if cmd == fake.CMD_ACCEPT_DISC:
                busy_at_accept.append(self.unit.busy)
            return real_handle(msgid, cmd, args)

        self.unit.handle = watch
        flows.run_add(self.ds, self.conn, job, job.trace, 2)

        self.assertTrue(job.snapshot()["ok"])
        self.assertTrue(busy_at_accept, "0x1D was never sent")
        self.assertFalse(any(busy_at_accept),
                         "0x1D was sent mid-motion; the disc would be spat back out")

    def test_accepting_too_early_makes_the_unit_spit_the_disc_out(self):
        # The failure the settle wait prevents, forced at protocol level.
        self.ds.move_to(4)
        self.unit.insert_disc()
        self.assertTrue(self.ds.wait_for_insert())
        self.assertIsNone(self.ds.command(fake.CMD_ACCEPT_DISC),
                          "0x1D was heard mid-motion; it should have been dropped")

        self.assertTrue(self.ds.wait_state(protocol.ST_ACK_TIMEOUT,
                                           protocol.ST_ACK_TIMEOUT, 30_000))
        self.assertTrue(self.ds.disc_in_bay())
        self.assertNotIn(4, self.unit.occupied)

    def test_a_return_restores_the_disc_to_its_reserved_slot(self):
        disc_id = self.disc(12, "Ico")
        db.mark_out(self.conn, disc_id)
        job = self.job("return", "Return Ico to slot 12", disc_id=disc_id, slot=12,
                       when={jobs.AWAITING_INSERT: 2_000})
        flows.run_add(self.ds, self.conn, job, job.trace, 12, disc_id)

        snap = job.snapshot()
        self.assertTrue(snap["ok"], snap["message"])
        self.assertIn("Ico is back in slot 12", snap["message"])
        self.assertEqual(db.get_disc(self.conn, disc_id)["status"], db.STORED)
        self.assertIn("returned", [k for k, _ in self.events()])

    def test_the_run_is_traced(self):
        job = self.job("add", "Load a disc into slot 1", slot=1,
                       when={jobs.AWAITING_INSERT: 2_000})
        flows.run_add(self.ds, self.conn, job, job.trace, 1)

        with open(self.only_trace()) as fh:
            body = fh.read()
        # "end" is the controller's, not the flow's; see ControllerTest.
        for marker in ("move to slot 1", "awaiting insert", "disc seen",
                       "0x1D acknowledged", "parked"):
            self.assertIn(marker, body)

    def test_the_simulated_run_matches_a_captured_one(self):
        # 23 of the 25 traces in data/traces share this signature. If the
        # simulator drifts from the hardware, this is what notices.
        job = self.job("add", "Load a disc into slot 4", slot=4,
                       when={jobs.AWAITING_INSERT: 4_000})
        flows.run_add(self.ds, self.conn, job, job.trace, 4)

        captured = signature(os.path.join(FIXTURES, "add-1789349798-4.log"))
        self.assertEqual(signature(self.only_trace()), captured)


class Eject(FlowTest):
    def setUp(self):
        FlowTest.setUp(self)
        self.disc_id = self.disc(18, "Shadow of the Colossus")
        self.unit.occupied.add(18)

    def test_a_disc_is_presented_and_taken(self):
        job = self.job("eject", "Eject slot 18", disc_id=self.disc_id, slot=18,
                       when={jobs.PRESENTED: 1_500})
        flows.run_eject(self.ds, self.conn, job, job.trace, self.disc_id)

        snap = job.snapshot()
        self.assertTrue(snap["ok"], snap["message"])
        self.assertIn(jobs.PRESENTED, job.phases)

        row = db.get_disc(self.conn, self.disc_id)
        self.assertEqual(row["status"], db.OUT)
        self.assertEqual(row["slot"], 18, "a checked-out disc keeps its slot")
        self.assertNotIn(18, self.unit.occupied)

    def test_an_empty_slot_is_reported_as_drift(self):
        self.unit.occupied.discard(18)
        job = self.job("eject", "Eject slot 18", disc_id=self.disc_id, slot=18)
        flows.run_eject(self.ds, self.conn, job, job.trace, self.disc_id)

        snap = job.snapshot()
        self.assertFalse(snap["ok"])
        self.assertIn("Reconcile", snap["error"])
        self.assertIn(("failed", "eject found no disc"), self.events())
        self.assertEqual(db.get_disc(self.conn, self.disc_id)["status"], db.STORED)

    def test_a_disc_left_in_the_bay_is_put_back(self):
        job = self.job("eject", "Eject slot 18", disc_id=self.disc_id, slot=18)
        flows.run_eject(self.ds, self.conn, job, job.trace, self.disc_id)  # nobody takes it

        snap = job.snapshot()
        self.assertFalse(snap["ok"])
        self.assertIn(jobs.RETRACTING, job.phases)
        self.assertIn(fake.CMD_RETRACT, [c for c, _ in self.unit.log])
        self.assertIn("retracted", [k for k, _ in self.events()])
        self.assertEqual(db.get_disc(self.conn, self.disc_id)["status"], db.STORED)
        self.assertIn(18, self.unit.occupied, "the disc was not put back")

    def test_a_missing_disc_is_not_a_hardware_error(self):
        job = self.job("eject", "Eject slot 18", disc_id=self.disc_id, slot=18)
        db.delete_disc(self.conn, self.disc_id)
        flows.run_eject(self.ds, self.conn, job, job.trace, self.disc_id)
        self.assertIn("no longer in the catalogue", job.snapshot()["error"])


class ControllerTest(FlowTest):
    def controller(self):
        return device.DeviceController(ds=self.ds, db_path=self.db_path,
                                       trace_dir=self.traces)

    def test_one_job_at_a_time(self):
        control = self.controller()
        first = jobs.Job("reset", "Reset the unit")
        control.submit(first, lambda ds, conn, job, trace: job.succeed("done")
                       if self.block.wait(5) else None)
        try:
            with self.assertRaises(device.Busy) as caught:
                control.submit(jobs.Job("reset", "Reset again"),
                               lambda ds, conn, job, trace: job.succeed("done"))
            self.assertIs(caught.exception.job, first)
        finally:
            self.block.set()
        self.wait_for(first)

    def test_a_lost_unit_is_reported_as_such(self):
        control = self.controller()
        job = jobs.Job("reset", "Reset the unit")

        def pull_the_plug(ds, conn, job, trace):
            self.io.unplug()
            ds.status()

        control.submit(job, pull_the_plug)
        self.wait_for(job)
        self.assertIn("Lost the Disc Stakka", job.snapshot()["error"])

    def test_a_runner_that_reports_nothing_still_ends_the_job(self):
        control = self.controller()
        job = jobs.Job("reset", "Reset the unit")
        control.submit(job, lambda ds, conn, job, trace: None)
        self.wait_for(job)
        self.assertIn("without reporting a result", job.snapshot()["error"])

    def test_the_trace_is_closed_off_even_when_a_job_fails(self):
        control = self.controller()
        job = jobs.Job("reset", "Reset the unit")
        # The last-resort guard prints the traceback, which is right in
        # production and only noise here.
        with contextlib.redirect_stderr(io.StringIO()):
            control.submit(job, lambda ds, conn, job, trace: 1 / 0)
            self.wait_for(job)
        with open(self.only_trace()) as fh:
            self.assertIn("-- end", fh.read())

    def test_info_never_raises_and_reports_the_simulated_unit(self):
        control = self.controller()
        info = control.info()
        self.assertTrue(info["present"])
        self.assertTrue(info["connected"])
        self.assertEqual(info["serial"], "%08x" % self.unit.serial)
        self.assertEqual(info["firmware"], "02.17.0079")

    def setUp(self):
        FlowTest.setUp(self)
        import threading
        self.block = threading.Event()

    def wait_for(self, job, timeout=5.0):
        import time as real_time
        deadline = real_time.monotonic() + timeout
        while real_time.monotonic() < deadline:
            if job.done:
                return
            real_time.sleep(0.005)
        self.fail("job did not finish")


if __name__ == "__main__":
    unittest.main()
