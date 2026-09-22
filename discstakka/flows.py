"""The hardware flows.

Each runner blocks on the device worker thread, updating job phase as it goes.
The web layer polls Job.snapshot() and renders whatever phase it finds.

Everything a runner needs arrives as an argument: the unit, a database
connection, the job to report through, and a trace to record the status stream.
Nothing here opens a device, a database or a file, which is what lets the whole
set run against a simulated carousel.
"""

import time

from . import jobs
from .catalog import db


def run_reset(ds, conn, job, trace):
    job.set_phase(jobs.MOVING, "Resetting the unit...")
    ds.reset(progress=job.note)
    job.succeed("Unit reset. Carousel is at home.")


def run_eject(ds, conn, job, trace, disc_id):
    disc = db.get_disc(conn, disc_id)
    if disc is None:
        job.fail("That disc is no longer in the catalogue.")
        return
    slot = disc["slot"]

    job.set_phase(jobs.MOVING, "Fetching slot %d..." % slot)
    trace.mark("eject slot %d" % slot)
    ds.move_to(slot, eject=True, progress=job.note)

    # With a working sensor this tells us the disc really made it to the bay.
    if not ds.disc_in_bay():
        job.set_phase(jobs.PARKING, "Nothing came out; returning to home...")
        trace.mark("nothing in the bay")
        ds.park()
        db.log_event(conn, "failed", disc_id, slot, "eject found no disc")
        conn.commit()
        job.fail(
            "Slot %d appears to be empty. The catalogue may be out of "
            "step with the carousel - try Reconcile." % slot
        )
        return

    job.set_phase(jobs.PRESENTED, "Take the disc from the unit.", window_s=5)
    trace.mark("presented; waiting for it to be taken")

    if not ds.wait_for_take():
        job.set_phase(jobs.RETRACTING, "Not taken - putting it back...")
        trace.mark("not taken; retracting")
        ds.retract(progress=job.note)
        ds.park()
        db.log_event(conn, "retracted", disc_id, slot)
        conn.commit()
        job.fail(
            "The disc was not taken in time, so the unit was told to "
            "take it back into slot %d. The catalogue has not changed." % slot
        )
        return

    job.set_phase(jobs.PARKING, "Returning to home...")
    trace.mark("taken; returning to home")
    ds.park()
    db.mark_out(conn, disc_id)
    job.succeed(
        "A disc was taken from slot %d. The catalogue now lists %s "
        "as checked out and holds the slot for its return." % (slot, disc["title"]),
        disc_id=disc_id,
    )


def run_add(ds, conn, job, trace, slot, disc_id=None):
    """Load a disc into ``slot``.

    ``disc_id`` set means this is a return: the disc is already catalogued and
    its slot was reserved. Otherwise a placeholder row is created once the disc
    is physically in, so the catalogue never loses track of it even if the user
    abandons the metadata form.
    """
    returning = disc_id is not None

    job.set_phase(jobs.MOVING, "Moving to slot %d..." % slot)
    trace.mark("move to slot %d" % slot)
    ds.move_to(slot, progress=job.note)

    job.set_phase(jobs.AWAITING_INSERT, "Insert the disc now.", window_s=30)
    trace.mark("awaiting insert (watching DISC_WAITING)")
    if not ds.wait_for_insert():
        job.set_phase(jobs.PARKING, "Timed out; returning to home...")
        ds.park()
        job.fail("No disc went in within 30 seconds. Nothing has changed.")
        return

    job.set_phase(jobs.INGESTING, "Taking the disc in...")
    trace.mark("disc seen; settling before 0x1D")
    ds.ingest(progress=job.note)
    trace.mark("0x1D acknowledged")

    job.set_phase(jobs.PARKING, "Returning to home...")
    ds.park()
    trace.mark("parked; watching for 10s to see if it comes back out")

    # The unit has been spitting discs back out after a nominally successful
    # load. Keep watching so the trace captures it.
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        trace.status(ds.status())

    if returning:
        db.mark_stored(conn, disc_id)
        disc = db.get_disc(conn, disc_id)
        job.succeed(
            "%s is back in slot %d." % (disc["title"] if disc else "Disc", slot),
            disc_id=disc_id,
        )
    else:
        new_id = db.create_disc(conn, slot, title="Untitled disc (slot %d)" % slot)
        job.succeed(
            "Disc loaded into slot %d. Now give it a name." % slot,
            disc_id=new_id,
            needs_details=True,
        )
