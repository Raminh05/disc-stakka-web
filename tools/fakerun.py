#!/usr/bin/env python
"""Run the real application against a simulated carousel.

No hardware, no risk to the real catalogue: the whole app is pointed at a
scratch directory seeded with a few discs. Everything except the unit itself is
the code that ships.

You are the other half of the machine. Type a key and press return:

    i   insert a disc      (when the page asks for one)
    t   take the disc      (when the unit presents one)
    u   unplug the unit    r   plug it back in
    b   toggle the idle busy blip that makes commands go missing
    s   print the unit's state
    q   quit

Usage: python tools/fakerun.py [--port N] [--host H] [--keep DIR]

Binds to localhost by default. --host 0.0.0.0 is what you want inside a
container, or to show the fake to a PS3 on the LAN.
"""

import os
import shutil
import sys
import tempfile
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as app_module
from discstakka import device, protocol
from discstakka.catalog import db
from discstakka.config import Config
from discstakka.simulator import SimulatedTransport

SEED = [
    (1, "Shadow of the Colossus", "Team Ico", "Video games", "PlayStation 2"),
    (2, "Katamari Damacy", "Namco", "Video games", "PlayStation 2"),
    (7, "Ico", "Team Ico", "Video games", "PlayStation 2"),
    (18, "Spirited Away", "Studio Ghibli", "Movies", None),
]


def seed(path):
    conn = db.connect(path)
    try:
        for slot, title, subtitle, category, platform in SEED:
            db.create_disc(
                conn,
                slot,
                title,
                subtitle=subtitle,
                category=category,
                platform=platform,
            )
    finally:
        conn.close()
    return {slot for slot, _, _, _, _ in SEED}


def console(unit, transport):
    actions = {
        "i": lambda: (unit.insert_disc(), "disc pushed into the aperture"),
        "t": lambda: (unit.take_disc(), "disc taken from the bay"),
        "u": lambda: (transport.unplug(), "unplugged"),
        "r": lambda: (transport.replug(), "plugged back in"),
        "s": lambda: (
            None,
            "position %d  homed=%s  occupied=%s  %s"
            % (
                unit.position,
                unit.homed,
                sorted(unit.occupied),
                protocol.describe_status(unit.status()),
            ),
        ),
    }
    for line in sys.stdin:
        key = line.strip().lower()[:1]
        if key == "q":
            os._exit(0)
        if key == "b":
            unit.blips = not unit.blips
            print("   blips %s" % ("on" if unit.blips else "off"))
        elif key in actions:
            print("   %s" % actions[key]()[1])
        elif key:
            print("   ? one of i t u r b s q")


def main(argv):
    port = int(argv[argv.index("--port") + 1]) if "--port" in argv else 5051
    host = argv[argv.index("--host") + 1] if "--host" in argv else "127.0.0.1"
    keep = argv[argv.index("--keep") + 1] if "--keep" in argv else None

    data_dir = keep or tempfile.mkdtemp(prefix="discstakka-fake-")
    config = Config(data_dir=data_dir, port=port)
    os.makedirs(data_dir, exist_ok=True)
    db.init(config.db_path)
    occupied = seed(config.db_path)

    transport = SimulatedTransport()
    transport.carousel.occupied |= occupied
    unit = transport.carousel

    controller = device.DeviceController(
        ds=protocol.DiscStakka(transport),
        db_path=config.db_path,
        trace_dir=config.trace_dir,
    )
    app_module.create_app(config, controller)

    print(__doc__.split("Usage:")[0].strip())
    print("\ndata     %s" % data_dir)
    print("serial   %08x   firmware 02.17.0079" % unit.serial)
    print("discs    %s" % ", ".join("%d" % s for s in sorted(occupied)))
    print("\n  http://%s:%d/\n" % (host, port))

    threading.Thread(target=console, args=(unit, transport), daemon=True).start()
    try:
        app_module.serve(host, port)
    finally:
        if keep is None:
            shutil.rmtree(data_dir, ignore_errors=True)


if __name__ == "__main__":
    main(sys.argv[1:])
