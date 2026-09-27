"""The hidapi thread in transport.py, without hidapi.

On macOS hidapi ties its device manager to the first thread that calls it, and
a crash mid-eject came from that being a request thread that had exited. The
only thing that can be checked without hardware is the rule itself: every call
lands on one thread, and that thread outlives its callers.
"""

import threading
import unittest

from discstakka import transport


class HidapiThread(unittest.TestCase):
    def setUp(self):
        self.hidapi = transport._HidapiThread()

    def test_calls_from_short_lived_threads_share_one_live_thread(self):
        seen = []

        def caller():
            seen.append(self.hidapi.call(threading.current_thread))

        for _ in range(5):
            thread = threading.Thread(target=caller)
            thread.start()
            thread.join()

        self.assertEqual(len(set(seen)), 1)
        self.assertTrue(seen[0].is_alive())
        self.assertTrue(seen[0].daemon)

    def test_an_error_reaches_the_caller_and_the_thread_carries_on(self):
        def fail():
            raise OSError("open failed")

        with self.assertRaises(OSError):
            self.hidapi.call(fail)
        self.assertEqual(self.hidapi.call(lambda: 42), 42)


if __name__ == "__main__":
    unittest.main()
