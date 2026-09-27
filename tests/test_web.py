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
from discstakka import device, protocol
from discstakka import simulator as fake
from discstakka.catalog import db
from discstakka.config import Config

REFRESH = re.compile(r'http-equiv=["\']refresh["\']', re.I)


class WebTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="discstakka-web-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

        self.io = fake.SimulatedTransport()
        self.unit = self.io.carousel
        self.ds = protocol.DiscStakka(self.io)

        config = Config(data_dir=self.tmp)
        self.controller = device.DeviceController(
            ds=self.ds, db_path=config.db_path, trace_dir=config.trace_dir
        )
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
        for path in (
            "/disc/%d/eject" % disc_id,
            "/disc/%d/return" % disc_id,
            "/device/reset",
            "/device/reconnect",
        ):
            self.assertEqual(self.client.get(path).status_code, 405, path)


class Jobs(WebTest):
    def setUp(self):
        WebTest.setUp(self)
        self.unit.occupied.add(3)
        self.disc_id = self.disc()

    def test_eject_redirects_with_303(self):
        response = self.client.post("/disc/%d/eject" % self.disc_id)
        self.assertEqual(
            response.status_code, 303, "a 302 tells the client to preserve the method"
        )
        self.assertRegex(response.headers["Location"], r"/job/[\w-]+$")

    def test_the_job_page_polls_while_running_and_stops_when_done(self):
        job = self.controller.submit(
            jobs_stub := _Stub(), lambda ds, conn, job, trace: job.hold.wait(5)
        )
        try:
            page = self.body("/job/%s" % job.id)
            self.assertTrue(
                REFRESH.search(page), "a running job must keep the page refreshing"
            )
            self.assertIn(
                '<meta name="job-phase" content="moving">',
                page,
                "enhance.js seeds its idea of the phase from this tag",
            )
        finally:
            jobs_stub.hold.set()
        _wait(job)
        page = self.body("/job/%s" % job.id)
        self.assertFalse(REFRESH.search(page), "a finished job must stop refreshing")
        self.assertNotIn('name="job-phase"', page)

    def test_a_browser_that_polls_the_json_view_gets_only_a_slow_refresh(self):
        # enhance.js cannot cancel a meta refresh the parser has already
        # scheduled, so the server has to stop sending the fast one. The JSON
        # view vouches for the browser, briefly, so a browser that stops
        # polling does not stay on the slow refresh.
        interval = re.compile(r'content="(\d+);url=')
        job = self.controller.submit(
            stub := _Stub(), lambda ds, conn, job, trace: job.hold.wait(5)
        )
        try:
            page = self.body("/job/%s" % job.id)
            self.assertEqual(interval.search(page).group(1), "2")
            cookie = self.client.get("/job/%s.json" % job.id).headers["Set-Cookie"]
            self.assertIn("polls=1", cookie)
            self.assertIn("Max-Age=%d" % app_module.XHR_COOKIE_S, cookie)
            page = self.body("/job/%s" % job.id)
            self.assertEqual(
                interval.search(page).group(1), str(app_module.XHR_FALLBACK_S)
            )
            self.client.delete_cookie("polls")
            self.assertEqual(
                interval.search(self.body("/job/%s" % job.id)).group(1), "2"
            )
        finally:
            stub.hold.set()
        _wait(job)

    def test_an_eject_page_that_polls_still_refreshes_within_the_take_window(self):
        # The take prompt is up for five seconds. A safety net slower than that
        # shows a browser whose polling has died only the retraction.
        interval = re.compile(r'content="(\d+);url=')
        job = self.controller.submit(
            stub := _Stub(), lambda ds, conn, job, trace: job.hold.wait(5)
        )
        job.kind = "eject"
        try:
            self.client.set_cookie("polls", "1")
            page = self.body("/job/%s" % job.id)
            self.assertEqual(
                interval.search(page).group(1), str(app_module.EJECT_FALLBACK_S)
            )
            self.assertLessEqual(
                app_module.EJECT_FALLBACK_S * 1000, protocol.TAKE_WINDOW_MS
            )
        finally:
            stub.hold.set()
        _wait(job)

    def test_a_disc_the_unit_is_working_on_cannot_be_deleted(self):
        job = self.controller.submit(
            stub := _Stub(), lambda ds, conn, job, trace: job.hold.wait(5)
        )
        job.disc_id = self.disc_id
        try:
            response = self.client.post(
                "/disc/%d/delete" % self.disc_id, data={"confirm": "yes"}
            )
            self.assertEqual(response.status_code, 303)
            self.assertRegex(response.headers["Location"], r"/job/%s$" % job.id)
            self.assertIsNotNone(db.get_disc(self.conn, self.disc_id))
        finally:
            stub.hold.set()
        _wait(job)

    def test_a_slot_the_unit_is_working_on_cannot_be_reconciled_by_hand(self):
        job = self.controller.submit(
            stub := _Stub(), lambda ds, conn, job, trace: job.hold.wait(5)
        )
        job.slot = 9
        try:
            response = self.client.post(
                "/reconcile/manual", data={"slot": "9", "title": "Ico"}
            )
            self.assertEqual(response.status_code, 303)
            self.assertIsNone(db.get_by_slot(self.conn, 9))
        finally:
            stub.hold.set()
        _wait(job)

    def test_a_second_job_bounces_to_the_one_already_running(self):
        job = self.controller.submit(
            first := _Stub(), lambda ds, conn, job, trace: job.hold.wait(5)
        )
        try:
            response = self.client.post("/disc/%d/eject" % self.disc_id)
            self.assertEqual(response.status_code, 303)
            self.assertTrue(response.headers["Location"].endswith("/job/%s" % job.id))
        finally:
            first.hold.set()
        _wait(job)

    def test_a_slot_outside_the_carousel_is_refused_not_a_500(self):
        for slot in ("0", "101", "-3"):
            response = self.client.post(
                "/reconcile/manual", data={"slot": slot, "title": "Ico"}
            )
            self.assertEqual(response.status_code, 303, slot)
            self.assertRegex(response.headers["Location"], r"/reconcile$")
        self.assertEqual(db.count_discs(self.conn), 1)

    def test_the_json_view_is_never_cached(self):
        job = self.controller.submit(
            _Stub(), lambda ds, conn, job, trace: job.succeed("done")
        )
        _wait(job)
        response = self.client.get("/job/%s.json" % job.id)
        self.assertEqual(response.headers["Cache-Control"], "no-store")

    def test_the_json_view_matches_the_snapshot(self):
        job = self.controller.submit(
            _Stub(), lambda ds, conn, job, trace: job.succeed("done")
        )
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
