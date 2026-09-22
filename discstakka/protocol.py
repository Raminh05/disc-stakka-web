"""HID protocol for the Imation Disc Stakka.

A port of ``disc-stakka-ctl-0.04/src/standalone/discstakka.c``, which was itself
derived from the 2005 reverse-engineering in that tarball's ``readme.txt`` and
``src/client/DiscStakka.py``.

Four behaviours in here are not obvious from the original sources. Each was a
real bug before it became a rule, so none of them should be "simplified" away:

1. The unit latches its last reply and repeats it ~7/sec rather than pushing
   fresh status. Callers must re-request state on every poll.
2. Only some replies carry status flags in X2/X3. A stale ``0x1B`` version reply
   decodes to ``0x0200``, which passes ``& ST_BUSY == 0`` and will silently
   satisfy any wait-for-not-busy. See :data:`CARRIES_STATUS`.
3. While bit 15 is clear the carousel's position is unknown and the unit
   silently ignores ``0x04`` to any slot except 0. Home first.
4. While BUSY the unit ignores every command - no NAK, no error reply, just no
   ack at all. Never send mid-motion.
"""

import time
from typing import Callable, Optional

from .slots import HOME, SLOT_MAX, SLOT_MIN
from .transport import PID, VID, HidTransport

# Command codes (readme.txt; DiscStakka.py:14-22)
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
CMD_NEW_UNIT = 0xCC  # device -> host only: unit is unacknowledged

#: Replies whose X2/X3 actually hold status flags. ``0x06`` is an addition to
#: readme.txt, which records its response as "Unknown!" - it returns the same
#: status word the others do.
CARRIES_STATUS = frozenset(
    (CMD_REQUEST_STATE, CMD_SET_POS, CMD_SET_LED, CMD_CLEAR_ERR_2, CMD_ACCEPT_DISC)
)

# Status bits, taken from the (X2 << 8) | X3 word (DiscStakka.py:24-32)
ST_BUSY = 0x8080  # readme bits 0 and 8, both "unit busy"
ST_DISC_WAITING = 0x1000  # readme bit 3, a disc is at the loading aperture
ST_NEW_DISC_ACK = 0x0800  # readme bit 4, "New CD in slot awaiting ack"
ST_DISC_IN_BAY = 0x0400  # readme bit 5, a disc is presented in the bay
ST_ACK_TIMEOUT = 0x0200  # readme bit 6, "Timeout awaiting ack, cd removed"
ST_HOMED = 0x0001  # readme bit 15 "? Ok?" - carousel position is known

SETTLE_MS = 30_000  # waitTillState's default: 300 polls at 0.1s
ACK_MS = 3_000  # writeRead's: 30 polls at 0.1s
POLL_MS = 100
NOW_MS = 500  # "check the current state once"
INSERT_WINDOW_MS = 30_000  # DiscStakka.py:166
TAKE_WINDOW_MS = 5_000  # DiscStakka.py:278

Progress = Optional[Callable[[str], None]]


class DeviceError(Exception):
    """Any failure talking to or commanding the unit."""


class NotConnected(DeviceError):
    pass


class Packet(object):
    """An 8-byte inbound report."""

    __slots__ = ("tag", "unit", "msgid", "cmd", "x1", "x2", "x3", "x4")

    def __init__(self, raw):
        (self.tag, self.unit, self.msgid, self.cmd,
         self.x1, self.x2, self.x3, self.x4) = raw[:8]

    @property
    def status(self):
        return (self.x2 << 8) | self.x3

    def __repr__(self):
        return "<Packet cmd=%02x msgid=%02x %02x %02x %02x %02x>" % (
            self.cmd, self.msgid, self.x1, self.x2, self.x3, self.x4)


def describe_status(st):
    """Render a status word the way the C tool's ``print_flags`` does."""
    names = []
    if st & ST_BUSY:
        names.append("BUSY")
    if st & ST_DISC_WAITING:
        names.append("DISC_WAITING")
    if st & ST_NEW_DISC_ACK:
        names.append("NEW_DISC_ACK")
    if st & ST_DISC_IN_BAY:
        names.append("DISC_IN_BAY")
    if st & ST_ACK_TIMEOUT:
        names.append("ACK_TIMEOUT")
    if st & ST_HOMED:
        names.append("HOMED")
    return "%04x [%s]" % (st, " ".join(names))


def _now_ms():
    return int(time.monotonic() * 1000)


class DiscStakka(object):
    """A single Disc Stakka unit.

    Not thread-safe by design. Exactly one owner thread should hold an instance;
    see :mod:`discstakka.device`.
    """

    def __init__(self, transport=None):
        self._io = transport if transport is not None else HidTransport()
        self._open = False
        self.unit = 0
        self.serial = None
        self.firmware = None
        #: Optional callable(status_word). Invoked on every status observation
        #: so a caller can record exactly what the unit reported and when.
        self.trace = None

    # -- connection ------------------------------------------------------

    def open(self, attempts=3):
        if self._open:
            return

        last = None
        for attempt in range(attempts):
            try:
                self._io.open()
            except (IOError, OSError) as exc:
                last = exc
            else:
                self._open = True
                try:
                    self._handshake()
                    return
                except Exception as exc:
                    last = exc
                    self.close()
            if attempt + 1 < attempts:
                time.sleep(0.4)

        raise NotConnected(
            "cannot open Disc Stakka (%04x:%04x): %s" % (VID, PID, last))

    def reconnect(self):
        """Drop the handle and open a fresh one. The manual recovery path."""
        self.close()
        self.open()

    def close(self):
        self._open = False
        self.serial = None
        self.firmware = None
        self._io.close()

    @property
    def connected(self):
        return self._open

    def present(self):
        """True if a unit is on the bus, without opening it."""
        return self._io.present()

    # -- wire ------------------------------------------------------------

    def _read(self, timeout_ms=POLL_MS):
        if not self._open:
            raise NotConnected("device is not open")
        try:
            raw = self._io.read(64, timeout_ms)
        except (IOError, OSError) as exc:
            self.close()
            raise NotConnected("read failed: %s" % exc)
        if not raw:
            return None
        if len(raw) < 8:
            raise DeviceError("short packet: %d bytes, need 8" % len(raw))
        return Packet(raw)

    def _write(self, msgid, cmd, x1=0, x2=0, x3=0, x4=0, x5=0):
        if not self._open:
            raise NotConnected("device is not open")
        # Leading 0x00 is hidapi's report-ID slot; the unit uses unnumbered
        # reports so hidapi strips it. The 0x01 after it is a constant payload
        # byte (core.h calls it ReportID, but core.cpp:148 sends report ID 0).
        buf = bytes([0x00, 0x01, self.unit, msgid, cmd, x1, x2, x3, x4, x5])
        try:
            if self._io.write(buf) < 0:
                raise DeviceError("write failed")
        except (IOError, OSError) as exc:
            self.close()
            raise NotConnected("write failed: %s" % exc)

    def command(self, cmd, x1=0, x2=0, x3=0, x4=0, x5=0, timeout_ms=ACK_MS):
        """Send one command and return its reply.

        MessageID is a one-up counter used to pair replies with commands
        (DiscStakka.py:388): read to learn the last one, send with +1, then wait
        for the packet carrying it back.

        Returns None if the unit never acks. That is not always an error - it is
        how the unit declines a command it will not perform (a positional move
        while un-homed, or anything at all while BUSY).
        """
        seed = self._read(timeout_ms)
        if seed is None:
            raise DeviceError("unit is silent; it should stream continuously")
        msgid = (seed.msgid + 1) & 0xFF
        self._write(msgid, cmd, x1, x2, x3, x4, x5)

        deadline = _now_ms() + timeout_ms
        while _now_ms() < deadline:
            reply = self._read(POLL_MS)
            if reply is not None and reply.msgid == msgid:
                return reply
        return None

    def require(self, cmd, *args, **kwargs):
        """:meth:`command`, retried, raising only if it never lands.

        A dropped command is not always the caller's fault. The unit blips BUSY
        for ~0.3s roughly every 3s even at rest, and anything arriving during a
        blip is silently discarded - so a command can be checked-idle, sent, and
        lost purely on timing. Measured on hardware: that is about a 1-in-10
        chance per move, which showed up as moves failing at random.

        Retrying is safe for every command used here. The unit acks what it
        processes, so no ack means it was never processed - there is nothing to
        do twice.
        """
        tries = kwargs.pop("tries", 3)
        for attempt in range(tries):
            reply = self.command(cmd, *args, **kwargs)
            if reply is not None:
                return reply
            if attempt + 1 < tries:
                self.wait_idle(2_000)  # ride out the blip, then try again
        raise DeviceError(
            "unit did not acknowledge command %02x (%d attempts)" % (cmd, tries))

    # -- state -----------------------------------------------------------

    def status(self):
        return self.require(CMD_REQUEST_STATE).status

    def position(self):
        """Current slot. ``0x14``'s reply carries position in X1."""
        return self.require(CMD_CLEAR_ERR_2).x1

    def wait_state(self, mask, want, timeout_ms=SETTLE_MS):
        """Poll until ``status & mask == want``. True on success.

        Re-requests state every cycle rather than reading the stream, because
        the unit repeats its last reply and a stale non-status packet would
        otherwise be decoded as status.
        """
        deadline = _now_ms() + timeout_ms
        while True:
            reply = self.command(CMD_REQUEST_STATE)
            if reply is not None and reply.cmd in CARRIES_STATUS:
                if self.trace is not None:
                    self.trace(reply.status)
                if (reply.status & mask) == want:
                    return True
            if _now_ms() >= deadline:
                return False

    def wait_idle(self, timeout_ms=SETTLE_MS):
        return self.wait_state(ST_BUSY, 0, timeout_ms)

    def ensure_homed(self, progress=None):
        """Home the carousel if its position is unknown.

        Without this a positional move is silently swallowed - the unit does not
        NAK, it simply never acks - and the caller sees a bare timeout with no
        clue why. This is why readme.txt's startup sequence ends with ``04``.
        """
        if self.status() & ST_HOMED:
            return
        if not self.wait_idle():
            raise DeviceError("unit is busy; cannot home it")
        _report(progress, "Homing carousel...")
        self.require(CMD_SET_POS, HOME)
        if not self.wait_idle():
            raise DeviceError("timed out homing the carousel")
        # Confirm rather than assume: a dropped move also leaves the unit idle,
        # so wait_idle() alone cannot tell success from silence.
        if not self.status() & ST_HOMED:
            raise DeviceError("the carousel did not home")

    # -- handshake -------------------------------------------------------

    def _handshake(self):
        """Ack a freshly connected unit and learn its serial (core.cpp:20-34).

        A new unit reports command code ``0xCC`` and ignores everything until it
        is acked with ``0x1C``, whose reply carries the serial.
        """
        deadline = _now_ms() + 5_000
        while _now_ms() < deadline:
            pkt = self._read(POLL_MS)
            if pkt is None:
                continue
            self.unit = pkt.unit

            if pkt.cmd == CMD_NEW_UNIT:
                self._write((pkt.msgid + 1) & 0xFF, CMD_GET_SERIAL)
                continue

            if pkt.cmd == CMD_GET_SERIAL:
                self.serial = _u32(pkt)
                break

            reply = self.command(CMD_GET_SERIAL)
            if reply is None:
                raise DeviceError("unit would not report its serial")
            self.serial = _u32(reply)
            break
        else:
            raise DeviceError("no packets from the unit during handshake")

        self.firmware = self._read_firmware()

    def _read_firmware(self):
        """Version bytes are BCD: 0x1B -> 00 02 00 79 and 0x1A -> 01 04 23 17
        gave readme.txt's "02.17.0079", so format them as hex."""
        a = self.command(CMD_VERSION_A)
        b = self.command(CMD_VERSION_B)
        if a is None or b is None:
            return None
        return "%02x.%02x.%02x%02x" % (b.x2, a.x4, b.x3, b.x4)

    # -- operations ------------------------------------------------------

    def reset(self, progress=None):
        """Clear errors, LED solid, park at home.

        The clear-error pair goes *before* the busy wait, unlike
        DiscStakka.py:58-64 which gates the only thing that clears a latched
        error behind a wait the error itself prevents from passing.
        """
        _report(progress, "Clearing errors...")
        self.command(CMD_REQUEST_STATE)
        self.command(CMD_CLEAR_ERR_1)
        self.command(CMD_CLEAR_ERR_2)
        if not self.wait_idle():
            raise DeviceError("unit stayed busy; it may need a power cycle")

        self.command(CMD_SET_LED, 1, 1)
        _report(progress, "Homing carousel...")
        self.require(CMD_SET_POS, HOME)
        if not self.wait_idle():
            raise DeviceError("timed out returning to home")

    def move_to(self, slot, eject=False, progress=None):
        """Rotate to ``slot``. With ``eject`` the disc there is presented."""
        if not (HOME <= slot <= SLOT_MAX):
            raise ValueError("slot %r out of range 0-%d" % (slot, SLOT_MAX))
        if not self.wait_idle():
            raise DeviceError("unit is busy")
        if slot != HOME:
            self.ensure_homed(progress)
        self.require(CMD_SET_POS, slot, 1 if eject else 0)
        if not self.wait_idle():
            raise DeviceError("timed out moving to slot %d" % slot)

    def park(self, progress=None):
        _report(progress, "Returning to home...")
        try:
            self.move_to(HOME)
        except DeviceError:
            pass  # best effort; never mask the original failure

    def wait_for_insert(self, timeout_ms=INSERT_WINDOW_MS):
        """Wait for a disc to appear at the loading aperture."""
        return self.wait_state(ST_DISC_WAITING, ST_DISC_WAITING, timeout_ms)

    def wait_for_take(self, timeout_ms=TAKE_WINDOW_MS):
        """Wait for a presented disc to be removed from the bay."""
        return self.wait_state(ST_DISC_IN_BAY, 0, timeout_ms)

    def disc_in_bay(self):
        return bool(self.status() & ST_DISC_IN_BAY)

    def ingest(self, progress=None):
        """Acknowledge and take in the disc waiting at the aperture.

        The settle wait is **not** cosmetic. Feeding a disc in makes the unit
        BUSY drawing it off the aperture, and a command sent while BUSY is
        silently dropped. Send ``0x1D`` too early and the firmware never hears
        it, times out waiting to be told what to do, and spits the disc back
        out - readme.txt status bit 5, "Timeout awaiting ack, cd ejecting, in
        bay". DiscStakka.py:173 waits here too, commented "wait till the disc
        is fully retracted".

        The firmware also checks the disc-detect sensor inside its own ``0x1D``
        handler and declines to actuate without it, so there is no host-side
        way to force this.
        """
        _report(progress, "Disc detected, drawing it in...")
        if not self.wait_idle():
            raise DeviceError("the disc never settled at the aperture")

        _report(progress, "Taking disc in...")
        self.require(CMD_ACCEPT_DISC)
        if not self.wait_idle():
            raise DeviceError("timed out taking the disc in")

    def retract(self, progress=None):
        _report(progress, "Retracting disc...")
        self.wait_idle(NOW_MS)  # best effort; retract anyway if still busy
        self.command(CMD_RETRACT)
        self.wait_idle()

    def set_led(self, on_time=1, period=1):
        self.command(CMD_SET_LED, on_time, period)


def _u32(pkt):
    return (pkt.x1 << 24) | (pkt.x2 << 16) | (pkt.x3 << 8) | pkt.x4


def _report(progress, message):
    if progress is not None:
        progress(message)


def _main(argv):
    """Parity check against the C tool: ``python -m discstakka.protocol``."""
    seconds = int(argv[1]) if len(argv) > 1 else 5
    ds = DiscStakka()
    ds.open()
    print("serial     %08x  (unit %d)" % (ds.serial, ds.unit))
    print("firmware   %s" % ds.firmware)
    print("position   %d" % ds.position())
    print("\nwatching status for %ds (nothing is commanded to move)\n" % seconds)
    last = None
    deadline = _now_ms() + seconds * 1000
    while _now_ms() < deadline:
        st = ds.status()
        if st != last:
            print("  status %s" % describe_status(st))
            last = st
    ds.close()
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(_main(sys.argv))
