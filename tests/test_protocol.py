"""The wire protocol, against a simulated unit.

These are the tests for the four rules in protocol.py's module docstring. Each
rule was a real bug before it was a rule, and none of them can be checked on
hardware without moving a real carousel.
"""

import unittest

from discstakka import protocol
from discstakka.slots import SLOT_MAX, SLOT_MIN
from tests import fake_device as fake
from tests.clock import virtual_clock


class ProtocolTest(unittest.TestCase):
    def setUp(self):
        ctx = virtual_clock(protocol)
        self.clock = ctx.__enter__()
        self.addCleanup(ctx.__exit__, None, None, None)
        self.io = fake.SimulatedTransport(self.clock)
        self.unit = self.io.carousel
        self.ds = protocol.DiscStakka(self.io)

    def opened(self):
        self.ds.open()
        return self.ds

    def sent(self, cmd):
        return [args for c, args in self.unit.log if c == cmd]


class Handshake(ProtocolTest):
    def test_new_unit_is_acknowledged_and_reports_its_serial(self):
        self.unit.serial = 0xDEADBEEF
        self.opened()
        self.assertEqual(self.ds.serial, 0xDEADBEEF)
        self.assertEqual(self.ds.unit, 0)
        self.assertIn(fake.CMD_GET_SERIAL, [c for c, _ in self.unit.log])

    def test_firmware_is_the_documented_string(self):
        # readme.txt's worked example: 0x1B -> 00 02 00 79, 0x1A -> 01 04 23 17.
        self.opened()
        self.assertEqual(self.ds.firmware, "02.17.0079")

    def test_already_acknowledged_unit_is_asked_for_its_serial(self):
        # No 0xCC announcement: the handshake has to fall through to asking.
        self.io.open()
        self.io._latched = self.io._packet(7, fake.CMD_REQUEST_STATE, (0, 0, 0, 0))
        self.io.close()
        original = self.io.open

        def open_without_announcement():
            original()
            self.io._latched = self.io._packet(7, fake.CMD_REQUEST_STATE, (0, 0, 0, 0))

        self.io.open = open_without_announcement
        self.ds.open()
        self.assertEqual(self.ds.serial, self.unit.serial)


class MessageIdPairing(ProtocolTest):
    def test_reply_must_echo_the_message_id(self):
        self.opened()
        before = self.io._latched[2]
        reply = self.ds.command(fake.CMD_REQUEST_STATE)
        self.assertIsNotNone(reply)
        self.assertEqual(reply.msgid, (before + 1) & 0xFF)

    def test_a_stale_latched_reply_is_not_mistaken_for_an_answer(self):
        self.opened()
        # An actuating command is not processed while busy, so the latched
        # packet keeps its old message id and must not satisfy it.
        self.unit._work_for(5_000)
        self.assertIsNone(self.ds.command(fake.CMD_SET_LED, 1, 1, timeout_ms=500))

    def test_message_id_wraps_at_255(self):
        self.opened()
        self.io._latched = self.io._packet(0xFF, fake.CMD_REQUEST_STATE, (0, 0, 0, 0))
        reply = self.ds.command(fake.CMD_REQUEST_STATE)
        self.assertEqual(reply.msgid, 0x00)


class RuleOneRerequestEveryPoll(ProtocolTest):
    def test_wait_state_asks_again_on_every_cycle(self):
        # While the unit is busy each request goes unanswered, so a cycle costs
        # the full ACK_MS rather than one poll interval: a 20 s wait is about
        # seven requests, not two hundred.
        self.opened()
        self.io.writes[:] = []
        self.unit._work_for(20_000)
        self.ds.wait_idle()
        asks = len([c for c, _ in self.io.writes if c == fake.CMD_REQUEST_STATE])
        self.assertGreaterEqual(
            asks,
            5,
            "the unit repeats its last reply; polling the stream without "
            "re-asking reads stale data",
        )

    def test_a_change_is_only_seen_after_a_fresh_request(self):
        self.opened()
        self.ds.require(fake.CMD_REQUEST_STATE)
        latched = self.io._latched
        self.unit.homed = True
        # Nothing re-read the stream, so the latched packet still says un-homed.
        self.assertEqual(self.io._latched, latched)
        self.assertTrue(self.ds.status() & protocol.ST_HOMED)


class RuleTwoCarriesStatus(ProtocolTest):
    def test_a_version_reply_decodes_to_a_plausible_idle_status(self):
        # This is why the guard exists: 0x1B's payload reads as 0x0200, which
        # passes "not busy" and would satisfy any wait-for-idle.
        self.opened()
        reply = self.ds.command(fake.CMD_VERSION_B)
        self.assertEqual(reply.status, 0x0200)
        self.assertEqual(reply.status & protocol.ST_BUSY, 0)
        self.assertNotIn(reply.cmd, protocol.CARRIES_STATUS)

    def test_wait_idle_ignores_a_reply_that_carries_no_status(self):
        self.opened()

        # A unit that answers a status request with a version opcode. Contrived,
        # but it is exactly the shape the guard defends against, and without the
        # guard wait_idle returns true while the carousel is still moving.
        class EchoesVersion(object):
            def __init__(self, inner):
                self.inner = inner

            def __getattr__(self, name):
                return getattr(self.inner, name)

            def write(self, buf):
                result = self.inner.write(buf)
                latched = self.inner._latched
                if latched[3] == fake.CMD_REQUEST_STATE:
                    self.inner._latched = latched[:3] + (
                        fake.CMD_VERSION_B,
                        0x00,
                        0x02,
                        0x00,
                        0x79,
                    )
                return result

        self.ds._io = EchoesVersion(self.io)
        self.unit._work_for(3_000)
        self.assertFalse(
            self.ds.wait_idle(2_000),
            "a non-status reply was accepted as proof of idleness",
        )


class RuleThreeHomeFirst(ProtocolTest):
    def test_a_positional_move_is_ignored_while_the_position_is_unknown(self):
        self.opened()
        self.assertFalse(self.unit.homed)
        self.unit.log[:] = []
        self.assertIsNone(self.ds.command(fake.CMD_SET_POS, 5, timeout_ms=500))
        self.assertIn((fake.CMD_SET_POS, [5, 0, 0, 0, 0]), self.unit.dropped)

    def test_move_to_homes_first(self):
        self.opened()
        self.ds.move_to(7)
        moves = self.sent(fake.CMD_SET_POS)
        self.assertEqual(moves[0][0], protocol.HOME, "did not home before moving")
        self.assertEqual(moves[-1][0], 7)
        self.assertEqual(self.unit.position, 7)

    def test_homing_that_does_not_take_is_reported(self):
        self.opened()
        # The unit acks the move but never reports itself homed, which is what a
        # dropped move also looks like - hence the confirm step.
        real_arrived = self.unit._cmd_04

        def ack_but_do_not_home(args):
            reply = real_arrived(args)
            self.unit._events[:] = []
            self.unit.homed = False
            return reply

        self.unit._cmd_04 = ack_but_do_not_home
        with self.assertRaises(protocol.DeviceError) as caught:
            self.ds.ensure_homed()
        self.assertIn("did not home", str(caught.exception))


class RuleFourNeverSendWhileBusy(ProtocolTest):
    def test_a_command_sent_while_busy_is_simply_unanswered(self):
        # Status requests are still answered - the unit reports BUSY rather than
        # going silent, which is how wait_idle sees anything at all. It is the
        # commands that actuate something that vanish.
        self.opened()
        self.unit._work_for(5_000)
        self.assertIsNone(self.ds.command(fake.CMD_SET_LED, 1, 1, timeout_ms=500))
        busy = self.ds.command(fake.CMD_REQUEST_STATE, timeout_ms=500)
        self.assertIsNotNone(busy)
        self.assertTrue(busy.status & protocol.ST_BUSY)

    def test_require_gives_up_after_three_attempts(self):
        self.opened()
        self.unit._work_for(120_000)
        with self.assertRaises(protocol.DeviceError) as caught:
            self.ds.require(fake.CMD_SET_POS, 0, timeout_ms=500)
        self.assertIn("did not acknowledge command 04", str(caught.exception))
        self.assertIn("3 attempts", str(caught.exception))

    def test_require_rides_out_the_idle_blip(self):
        # The unit blips busy for ~0.3s every ~3s even at rest, and anything
        # arriving during a blip is discarded. Measured at about one move in ten.
        self.opened()
        self.ds.ensure_homed()
        self.unit.blips = True
        for slot in range(1, 51):
            self.ds.move_to(slot)
            self.assertEqual(self.unit.position, slot)
        self.assertTrue(
            self.unit.dropped,
            "no command was ever lost to a blip, so this proved nothing",
        )


class WireErrors(ProtocolTest):
    def test_a_silent_unit_is_an_error_not_a_timeout(self):
        self.opened()
        self.io.read = lambda size, timeout_ms: None
        with self.assertRaises(protocol.DeviceError) as caught:
            self.ds.command(fake.CMD_REQUEST_STATE)
        self.assertIn("silent", str(caught.exception))

    def test_a_short_packet_is_rejected(self):
        self.opened()
        self.io.read = lambda size, timeout_ms: [0x01, 0x00, 0x00]
        with self.assertRaises(protocol.DeviceError) as caught:
            self.ds.status()
        self.assertIn("short packet", str(caught.exception))

    def test_an_io_error_invalidates_the_handle(self):
        self.opened()
        self.assertTrue(self.ds.connected)
        self.io.unplug()
        with self.assertRaises(protocol.NotConnected):
            self.ds.status()
        self.assertFalse(
            self.ds.connected,
            "a failed transfer must drop the handle, not keep using it",
        )


class Connection(ProtocolTest):
    def test_open_retries_and_recovers(self):
        self.io.fail_opens(2)
        self.ds.open()
        self.assertTrue(self.ds.connected)
        self.assertEqual(self.io.opens, 1)

    def test_open_gives_up_with_the_vid_and_pid(self):
        self.io.fail_opens(3)
        with self.assertRaises(protocol.NotConnected) as caught:
            self.ds.open()
        self.assertIn("0718:d000", str(caught.exception))

    def test_open_is_idempotent(self):
        self.opened()
        self.ds.open()
        self.assertEqual(self.io.opens, 1)

    def test_present_does_not_need_the_handle(self):
        self.assertTrue(self.ds.present())
        self.io.unplug()
        self.assertFalse(self.ds.present())


class Positioning(ProtocolTest):
    def test_position_comes_from_the_clear_error_reply(self):
        self.opened()
        self.ds.move_to(42)
        self.assertEqual(self.ds.position(), 42)

    def test_slot_bounds(self):
        self.opened()
        self.ds.move_to(SLOT_MIN)
        self.ds.move_to(SLOT_MAX)
        self.ds.move_to(protocol.HOME)
        for bad in (-1, SLOT_MAX + 1):
            with self.assertRaises(ValueError):
                self.ds.move_to(bad)


class UndecodedBit(ProtocolTest):
    def test_an_unknown_high_bit_confuses_nothing(self):
        # 0x4000 is set in five of the captured traces and decoded by neither
        # the 2005 daemon nor protocol.py. Every mask must ignore it.
        self.opened()
        self.unit.unknown_bit = True
        self.ds.move_to(3)
        self.assertEqual(self.unit.position, 3)
        self.assertTrue(self.ds.status() & fake.UNKNOWN)
        self.assertTrue(self.ds.wait_idle(1_000))


if __name__ == "__main__":
    unittest.main()
