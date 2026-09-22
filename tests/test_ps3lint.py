"""Run the PS3 lint against the simulated server.

tools/ps3lint.py needs a live server, and one of the four pages it checks is
/device, which talks to the unit. Until there was a simulated carousel that
page could only be linted with hardware attached.
"""

import os
import socket
import subprocess
import sys
import time
import unittest
from urllib.error import URLError
from urllib.request import urlopen

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
    def test_every_page_passes_including_the_device_page(self):
        port = free_port()
        base = "http://127.0.0.1:%d" % port
        server = subprocess.Popen(
            [sys.executable, os.path.join("tools", "fakerun.py"), "--port", str(port)],
            cwd=ROOT,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        try:
            self.assertTrue(
                wait_for(base + "/", server),
                "the simulated server did not start:\n%s"
                % (server.stdout.read() if server.poll() else ""),
            )
            lint = subprocess.run(
                [sys.executable, os.path.join("tools", "ps3lint.py"), base],
                cwd=ROOT,
                capture_output=True,
                text=True,
                timeout=120,
            )
            self.assertEqual(lint.returncode, 0, lint.stdout + lint.stderr)
            self.assertIn("/device", lint.stdout)
        finally:
            server.terminate()
            try:
                server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                server.kill()
            server.stdout.close()


if __name__ == "__main__":
    unittest.main()
