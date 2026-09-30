"""Run the PS3 lint against the simulated server.

tools/ps3lint.py needs a live server, and one of the pages it checks is
/device, which talks to the unit. Until there was a simulated carousel that
page could only be linted with hardware attached.

A running job changes what is served: /device shows that it is busy and the
pages of the disc being moved send the browser to the job. So the standing
pages are linted with the unit at rest, and the job's own pages after.
"""

import os
import socket
import subprocess
import sys
import time
import unittest
from urllib.error import URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_for(url, process, timeout=30.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            return False
        try:
            urlopen(url, timeout=1).read()
            return True
        except (URLError, OSError):
            time.sleep(0.1)
    return False


class Ps3Lint(unittest.TestCase):
    def lint(self, *args, **kwargs):
        lint = subprocess.run(
            [sys.executable, os.path.join("tools", "ps3lint.py")] + list(args),
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=120,
        )
        self.assertEqual(
            lint.returncode, kwargs.get("exits", 0), lint.stdout + lint.stderr
        )
        return lint.stdout

    def test_every_page_passes_including_the_device_and_job_pages(self):
        port = free_port()
        base = "http://127.0.0.1:%d" % port
        server = subprocess.Popen(
            [sys.executable, os.path.join("tools", "fakerun.py"), "--port", str(port)],
            cwd=ROOT,
            # SIGTERM does not flush stdio, so a buffered pipe would lose the
            # output of a server that hangs rather than exits.
            env=dict(os.environ, PYTHONUNBUFFERED="1"),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        try:
            if not wait_for(base + "/", server):
                server.terminate()
                output = server.communicate(timeout=10)[0]
                self.fail(
                    "the simulated server never answered on %s:\n%s" % (base, output)
                )
            at_rest = self.lint(base)
            for path in ("/device", "/disc/1/delete", "/no-such-page"):
                self.assertIn("ok   %s" % path, at_rest)

            # The job page only exists while a job does. This ejects the first
            # seeded disc from the simulated carousel, which moves nothing real,
            # and follows the 303 to the page that polls it.
            eject = urlopen(Request(base + "/disc/1/eject", data=b""), timeout=5)
            job_path = urlparse(eject.geturl()).path
            self.assertRegex(job_path, r"^/job/[\w-]+$")
            busy = self.lint(base, "--only", "/device", job_path)
            self.assertIn("ok   /device", busy)
            self.assertIn("ok   %s" % job_path, busy)
            self.assertNotIn("/add", busy)

            # A page that is not the one asked for passes every markup rule,
            # which is how a template goes unlinted without anyone noticing.
            wrong = self.lint(
                base, "--only", "/disc/99", "/disc/1/delete", "/job/gone", exits=1
            )
            self.assertIn("FAIL /disc/99", wrong)
            self.assertIn("answered 404", wrong)
            self.assertIn("FAIL /disc/1/delete", wrong)
            self.assertIn("redirected to %s" % job_path, wrong)
            self.assertIn("FAIL /job/gone", wrong)
        finally:
            server.terminate()
            try:
                server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                server.kill()
            server.stdout.close()


if __name__ == "__main__":
    unittest.main()
