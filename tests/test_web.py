"""The routes, through Flask's test client against a simulated unit.

These cover the rules that keep the PS3 working - 303 after POST, the refresh
tag while a job runs, GET-vs-POST on each route - none of which could be
exercised before, because /device and every job page need a carousel.
"""

import re
import shutil
import tempfile
import unittest

import app as app_module
from catalog import db
from config import Config
from discstakka import device, protocol
from tests import fake_device as fake
from tests.clock import RealClock

REFRESH = re.compile(r'http-equiv=["\']refresh["\']', re.I)


class WebTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="discstakka-web-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

        self.clock = RealClock()
        self.io = fake.SimulatedTransport(self.clock)
        self.unit = self.io.carousel
        self.ds = protocol.DiscStakka(self.io)

        config = Config(data_dir=self.tmp)
        self.controller = device.DeviceController(
            ds=self.ds, db_path=config.db_path, trace_dir=config.trace_dir)
        self.app = app_module.create_app(config, self.controller)
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()

        self.conn = db.connect(config.db_path)
        self.addCleanup(self.conn.close)
        # Registered last, so it runs first: a job still on the worker thread
        # would otherwise outlive the temporary database it is writing to.
        self.addCleanup(self._drain)

    def _drain(self):
        self.io.unplug()
        job = self.controller.current
        if job is not None:
            _wait(job)

    def disc(self, slot=3, title="Katamari Damacy"):
        return db.create_disc(self.conn, slot, title)

    def body(self, path):
        response = self.client.get(path)
        self.assertEqual(response.status_code, 200, path)
        return response.get_data(as_text=True)


class Pages(WebTest):
    def test_the_catalogue_renders(self):
        self.disc()
        self.assertIn("Katamari Damacy", self.body("/"))

    def test_the_device_page_reports_the_unit(self):
        # Needs a carousel, so this page has never been testable before.
        page = self.body("/device")
        self.assertIn("%08x" % self.unit.serial, page)
        self.assertIn("02.17.0079", page)

    def test_the_device_page_still_renders_with_no_unit(self):
        self.io.unplug()
        self.assertEqual(self.client.get("/device").status_code, 200)

    def test_read_only_pages_take_get_and_post(self):
        # The PS3 cannot be relied on to issue anything but a form POST, so a
        # page that only reads must answer both.
        for path in ("/", "/reconcile", "/device"):
            self.assertEqual(self.client.get(path).status_code, 200, path)
            self.assertEqual(self.client.post(path).status_code, 200, path)

    def test_add_renders_on_get_and_submits_on_post(self):
        self.assertEqual(self.client.get("/add").status_code, 200)
        response = self.client.post("/add", data={"slot": "7"})
        self.assertEqual(response.status_code, 303)
        self.assertRegex(response.headers["Location"], r"/job/[\w-]+$")


class Methods(WebTest):
    def test_state_changing_routes_refuse_get(self):
        disc_id = self.disc()
        for path in ("/disc/%d/eject" % disc_id,
                     "/disc/%d/return" % disc_id,
                     "/device/reset",
                     "/device/reconnect"):
            self.assertEqual(self.client.get(path).status_code, 405, path)


class Jobs(WebTest):
    def setUp(self):
        WebTest.setUp(self)
        self.unit.occupied.add(3)
        self.disc_id = self.disc()

    def test_eject_redirects_with_303(self):
        response = self.client.post("/disc/%d/eject" % self.disc_id)
        self.assertEqual(response.status_code, 303,
                         "a 302 tells the client to preserve the method")
        self.assertRegex(response.headers["Location"], r"/job/[\w-]+$")

    def test_the_job_page_polls_while_running_and_stops_when_done(self):
        job = self.controller.submit(
            jobs_stub := _Stub(), lambda ds, conn, job, trace: job.hold.wait(5))
        try:
            page = self.body("/job/%s" % job.id)
            self.assertTrue(REFRESH.search(page),
                            "a running job must keep the page refreshing")
        finally:
            jobs_stub.hold.set()
        _wait(job)
        self.assertFalse(REFRESH.search(self.body("/job/%s" % job.id)),
                         "a finished job must stop refreshing")

    def test_a_second_job_bounces_to_the_one_already_running(self):
        job = self.controller.submit(
            first := _Stub(), lambda ds, conn, job, trace: job.hold.wait(5))
        try:
            response = self.client.post("/disc/%d/eject" % self.disc_id)
            self.assertEqual(response.status_code, 303)
            self.assertTrue(response.headers["Location"].endswith("/job/%s" % job.id))
        finally:
            first.hold.set()
        _wait(job)

    def test_the_json_view_matches_the_snapshot(self):
        job = self.controller.submit(
            _Stub(), lambda ds, conn, job, trace: job.succeed("done"))
        _wait(job)
        payload = self.client.get("/job/%s.json" % job.id).get_json()
        self.assertEqual(payload["id"], job.id)
        self.assertTrue(payload["done"])
        self.assertTrue(payload["ok"])

    def test_a_forgotten_job_sends_you_home_rather_than_to_an_error(self):
        # The registry keeps the last 40, so an old id is expected, not broken.
        response = self.client.get("/job/nope")
        self.assertEqual(response.status_code, 303)
        self.assertTrue(response.headers["Location"].endswith("/"))
        self.assertEqual(self.client.get("/job/nope.json").status_code, 404)


def _Stub():
    import threading

    from discstakka import jobs

    job = jobs.Job("reset", "Reset the unit")
    job.hold = threading.Event()
    return job


def _wait(job, timeout=5.0):
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if job.done:
            return
        time.sleep(0.005)
    raise AssertionError("job did not finish")


if __name__ == "__main__":
    unittest.main()
