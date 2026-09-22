"""A software Disc Stakka.

Implements the transport interface, so the real protocol.py drives it: the
message-ID pairing, the latched reply stream, the CARRIES_STATUS filter and the
retry in require() all run against this exactly as they run against hardware.

The opcodes and status bits here are written out from the 2005 reverse
engineering (disc-stakka-ctl-0.03) rather than imported from protocol.py. A mock
that shares its constants with the code under test cannot disagree with it, and
disagreeing is the whole job.

Timings come from data/traces/*.log, 25 real runs of this application:

  seek        ~1.5 s plus ~0.056 s per slot   (home->1 1.5 s, home->60 4.8 s)
  idle blip   ~0.30 s every ~3 s, even at rest
  ingest      disc at aperture, 1.5 s busy drawing it off, then NEW_DISC_ACK
              for 1.8 s more before the unit goes idle and will hear 0x1D

23 of those 25 traces share one status signature - HOMED, DISC_WAITING+HOMED,
NEW_DISC_ACK+HOMED, HOMED - which is what test_flows calibrates against.

Two bits are modelled from the client's expectations rather than from a capture,
because every trace on hand is a load or a return and none is an eject:
DISC_IN_BAY (0x0400) and ACK_TIMEOUT (0x0200).
"""

import heapq
import random

VID = 0x0718
PID = 0xD000

CMD_REQUEST_STATE = 0x01
CMD_CLEAR_ERR_1 = 0x03
CMD_SET_POS = 0x04
CMD_RETRACT = 0x05
CMD_SET_LED = 0x06
CMD_CLEAR_ERR_2 = 0x14
CMD_VERSION_A = 0x1A
CMD_VERSION_B = 0x1B
CMD_GET_SERIAL = 0x1C
CMD_ACCEPT_DISC = 0x1D
CMD_NEW_UNIT = 0xCC

#: Opcodes whose reply carries the status word in x2/x3. Anything else that puts
#: bytes there is a trap for a caller that forgets to check.
WITH_STATUS = frozenset(
    (CMD_REQUEST_STATE, CMD_SET_POS, CMD_SET_LED, CMD_CLEAR_ERR_2, CMD_ACCEPT_DISC)
)

#: Answered even while the unit is busy. Rule 4 is about commands that actuate
#: something; a unit that stayed silent under load could never report BUSY at
#: all, and the captured traces are full of BUSY status words. 0x14 is not here
#: because it clears a latched error as well as reporting position.
ALWAYS_ANSWERED = frozenset(
    (CMD_REQUEST_STATE, CMD_VERSION_A, CMD_VERSION_B, CMD_GET_SERIAL)
)

#: 0x0080 is set whenever the unit is busy; 0x8000 is set as well while it is
#: working on a command. Idle blips show 0x0081, commanded motion 0x8081.
BUSY_IDLE = 0x0080
BUSY_WORKING = 0x8000
DISC_WAITING = 0x1000
NEW_DISC_ACK = 0x0800
DISC_IN_BAY = 0x0400
ACK_TIMEOUT = 0x0200
#: Undecoded by the 2005 daemon and by protocol.py, but real: it latched part
#: way through one captured load and stayed set for every run of that session.
#: Modelled so the suite can prove an unknown high bit confuses no mask.
UNKNOWN = 0x4000
HOMED = 0x0001

REPEAT_MS = 143  # the unit repeats its last reply about seven times a second

SEEK_BASE_MS = 1_500
SEEK_PER_SLOT_MS = 56
BLIP_FOR_MS = 300
#: protocol.py's require() records the measured loss as about one move in ten.
#: The blip is modelled as that chance per actuating command rather than as a
#: fixed period, because a periodic one can never be hit: wait_idle returns the
#: instant a blip ends, leaving the command a clear run until the next.
BLIP_CHANCE = 0.1
APERTURE_MS = 1_500  # disc seen -> drawn off the aperture, NEW_DISC_ACK set
SETTLE_MS = 1_800  # NEW_DISC_ACK set -> unit idle and able to hear 0x1D
INGEST_MS = 1_800  # 0x1D acknowledged -> disc in the slot
BAY_MS = 1_200  # retract or present
#: How long the firmware waits to be told what to do with a disc it has drawn
#: in. Past this it gives up and puts the disc back out - the failure the 0.03
#: README describes as "accepts the CD, then ejects it soon after".
ACK_WINDOW_MS = 10_000


class Unplugged(OSError):
    """No unit on the bus."""


class Carousel(object):
    """The firmware's own view of the machine.

    Tests drive the physical half through :meth:`insert_disc` and
    :meth:`take_disc`, scheduled on the clock rather than fired from the test
    thread, so a flow running on the worker thread never races them.
    """

    def __init__(self, clock, occupied=(), serial=0x0DECAF01, unit=0):
        self.clock = clock
        self.occupied = set(occupied)
        self.serial = serial
        self.unit = unit

        self.position = 0
        self.homed = False
        self.disc_waiting = False
        self.new_disc_ack = False
        self.disc_in_bay = False
        self.ack_timeout = False
        self.unknown_bit = False

        self.blips = False
        self.blip_chance = BLIP_CHANCE
        self._rng = random.Random(20050209)  # the 0.03 tarball's date; any seed
        self.busy_until = 0
        self._working = True
        self._events = []
        self._seq = 0

        #: Every command the unit actually processed, for tests that assert on
        #: what went over the wire. Dropped commands never reach it.
        self.log = []
        #: Commands that arrived while busy or otherwise unheard.
        self.dropped = []

    # -- time ------------------------------------------------------------

    @property
    def now(self):
        return self.clock.ms

    def at(self, when_ms, fn):
        self._seq += 1
        heapq.heappush(self._events, (when_ms, self._seq, fn))

    def after(self, delay_ms, fn):
        self.at(self.now + delay_ms, fn)

    def _run_due(self):
        while self._events and self._events[0][0] <= self.now:
            _, _, fn = heapq.heappop(self._events)
            fn()

    def _work_for(self, ms, working=True):
        self.busy_until = max(self.busy_until, self.now + ms)
        self._working = working

    @property
    def busy(self):
        return self.now < self.busy_until

    # -- state -----------------------------------------------------------

    def status(self):
        st = 0
        if self.busy:
            st |= BUSY_IDLE | BUSY_WORKING if self._working else BUSY_IDLE
        if self.disc_waiting:
            st |= DISC_WAITING
        if self.new_disc_ack:
            st |= NEW_DISC_ACK
        if self.disc_in_bay:
            st |= DISC_IN_BAY
        if self.ack_timeout:
            st |= ACK_TIMEOUT
        if self.unknown_bit:
            st |= UNKNOWN
        if self.homed:
            st |= HOMED
        return st

    def seek_ms(self, target):
        return SEEK_BASE_MS + SEEK_PER_SLOT_MS * abs(target - self.position)

    # -- the physical half, driven by tests ------------------------------

    def insert_disc(self, at_ms=None):
        """A human feeds a disc into the aperture."""
        when = self.now if at_ms is None else at_ms
        self.at(when, self._disc_arrives)

    def take_disc(self, at_ms=None):
        """A human takes the disc the unit has presented."""
        when = self.now if at_ms is None else at_ms

        def taken():
            self.disc_in_bay = False

        self.at(when, taken)

    def _disc_arrives(self):
        self.disc_waiting = True
        self._work_for(APERTURE_MS)
        self.after(APERTURE_MS, self._disc_drawn_off)

    def _disc_drawn_off(self):
        self.disc_waiting = False
        self.new_disc_ack = True
        self._work_for(SETTLE_MS)
        deadline = self.now + SETTLE_MS + ACK_WINDOW_MS
        self.at(deadline, self._ack_expired)

    def _ack_expired(self):
        # Nothing acknowledged the disc, so the firmware puts it back out.
        if not self.new_disc_ack:
            return
        self.new_disc_ack = False
        self.ack_timeout = True
        self.disc_in_bay = True
        self._work_for(BAY_MS)

    # -- opcode handling -------------------------------------------------

    def handle(self, msgid, cmd, args):
        """Process one command. Returns the four reply bytes, or None if the
        unit does not hear it.

        Both silent-drop rules live here. The unit never NAKs; a command it will
        not perform simply produces no reply, which is what the caller's retry
        and homing logic exist to cope with.
        """
        self._run_due()

        if self.busy and cmd not in ALWAYS_ANSWERED:
            self.dropped.append((cmd, args))
            return None
        if (
            self.blips
            and cmd not in ALWAYS_ANSWERED
            and self._rng.random() < self.blip_chance
        ):
            # Housekeeping happened to start just as this arrived.
            self._work_for(BLIP_FOR_MS, working=False)
            self.dropped.append((cmd, args))
            return None
        if cmd == CMD_SET_POS and args[0] != 0 and not self.homed:
            # Position is unknown, so a move to anywhere but home is ignored.
            self.dropped.append((cmd, args))
            return None

        self.log.append((cmd, args))
        handler = getattr(self, "_cmd_%02x" % cmd, None)
        if handler is None:
            return self._status_reply()
        return handler(args)

    def _status_reply(self):
        st = self.status()
        return (self.position, (st >> 8) & 0xFF, st & 0xFF, 0)

    def _cmd_01(self, args):
        return self._status_reply()

    def _cmd_03(self, args):
        self.ack_timeout = False
        return (0, 0, 0, 0)  # no status in this reply; see WITH_STATUS

    def _cmd_04(self, args):
        target, eject = args[0], args[1]
        travel = self.seek_ms(target)
        self._work_for(travel)

        def arrived():
            self.position = target
            self.homed = True
            if eject and target in self.occupied:
                self.occupied.discard(target)
                self.disc_in_bay = True

        self.after(travel, arrived)
        return self._status_reply()

    def _cmd_05(self, args):
        self._work_for(BAY_MS)

        def retracted():
            if self.disc_in_bay:
                self.disc_in_bay = False
                self.occupied.add(self.position)

        self.after(BAY_MS, retracted)
        return (0, 0, 0, 0)

    def _cmd_06(self, args):
        return self._status_reply()

    def _cmd_14(self, args):
        self.ack_timeout = False
        return self._status_reply()

    def _cmd_1a(self, args):
        return (0x01, 0x04, 0x23, 0x17)

    def _cmd_1b(self, args):
        # Deliberately decodes to 0x0200 as a status word, which passes
        # "not busy". A caller that skips the CARRIES_STATUS check is caught
        # here rather than on hardware.
        return (0x00, 0x02, 0x00, 0x79)

    def _cmd_1c(self, args):
        s = self.serial
        return ((s >> 24) & 0xFF, (s >> 16) & 0xFF, (s >> 8) & 0xFF, s & 0xFF)

    def _cmd_1d(self, args):
        if not self.new_disc_ack:
            # The firmware checks its own disc sensor inside this handler and
            # declines without it, so the host cannot force a load.
            return self._status_reply()
        self.new_disc_ack = False
        self._work_for(INGEST_MS)
        slot = self.position

        def stored():
            self.occupied.add(slot)

        self.after(INGEST_MS, stored)
        return self._status_reply()


class SimulatedTransport(object):
    """A Carousel wired up as a transport for DiscStakka."""

    def __init__(self, clock, carousel=None, present=True):
        self.clock = clock
        self.carousel = carousel if carousel is not None else Carousel(clock)
        self._present = present
        self._opened = False
        self._fail_opens = 0
        #: The reply the unit repeats until it processes something new.
        self._latched = None
        self._msgid = 0
        #: Every command written, whether or not the unit heard it. Counting
        #: these is how a test tells re-requesting from reading the stream.
        self.writes = []
        #: Counts of the calls protocol.py makes, for tests that assert on
        #: re-enumeration and handle lifetime.
        self.opens = 0
        self.closes = 0
        self.enumerations = 0

    # -- knobs -----------------------------------------------------------

    def unplug(self):
        self._present = False
        self._opened = False

    def replug(self):
        self._present = True
        self._latched = None

    def fail_opens(self, n):
        self._fail_opens = n

    # -- transport interface ---------------------------------------------

    def present(self):
        self.enumerations += 1
        return self._present

    def open(self):
        self.enumerations += 1
        if self._fail_opens > 0:
            self._fail_opens -= 1
            raise Unplugged("simulated open failure")
        if not self._present:
            raise Unplugged("no Disc Stakka on the bus")
        self.opens += 1
        self._opened = True
        # A unit that has not been acknowledged announces itself and ignores
        # everything else until the host answers with 0x1C.
        self._latched = self._packet(self._msgid, CMD_NEW_UNIT, (0, 0, 0, 0))

    def close(self):
        if self._opened:
            self.closes += 1
        self._opened = False

    def read(self, size, timeout_ms):
        if not self._opened:
            raise Unplugged("device is not open")
        # The host waits for the next interrupt report, which arrives at the
        # unit's repeat cadence. Advancing here is what moves simulated time.
        self.clock.advance_ms(min(timeout_ms, REPEAT_MS))
        self.carousel._run_due()
        if not self._present:
            raise Unplugged("unit vanished from the bus")
        return list(self._latched)

    def write(self, buf):
        if not self._opened:
            raise Unplugged("device is not open")
        if not self._present:
            raise Unplugged("unit vanished from the bus")
        buf = list(buf)
        # protocol.py writes hidapi's report-id slot first; the unit sees the
        # nine bytes after it.
        _, tag, unit, msgid, cmd = buf[0], buf[1], buf[2], buf[3], buf[4]
        args = buf[5:10]
        if tag != 0x01 or unit != self.carousel.unit:
            return len(buf)

        self.writes.append((cmd, args))
        reply = self.carousel.handle(msgid, cmd, args)
        if reply is not None:
            self._msgid = msgid
            self._latched = self._packet(msgid, cmd, reply)
        return len(buf)

    def _packet(self, msgid, cmd, payload):
        return (0x01, self.carousel.unit, msgid, cmd) + tuple(payload)
