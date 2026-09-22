"""Disc Stakka web catalogue.

A replacement for Imation's discontinued Opditracker, built to work on browsers
from the PlayStation 3's NetFront upward. That constraint shapes everything:
server-rendered HTML, form POSTs, redirect-after-POST, and no required
JavaScript anywhere. Long hardware operations are jobs polled with
``<meta http-equiv="refresh">``.

Run single-process. See discstakka/device.py for why.
"""

import math
import os

from flask import (Flask, abort, flash, redirect, render_template, request,
                   session, url_for)

from discstakka.catalog import art, db, taxonomy
from discstakka.config import Config
from discstakka import device, flows, jobs
from discstakka.protocol import DeviceError
from discstakka.slots import SLOT_MAX, SLOT_MIN

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = art.MAX_BYTES + (1 << 20)
app.config["TEMPLATES_AUTO_RELOAD"] = True

#: Set by create_app. There is exactly one of each per process, which is the
#: same constraint that stops this app running under a multi-worker server.
config = None
controller = None


def create_app(cfg=None, ctrl=None):
    """Wire the application up and return it.

    Importing this module does nothing but define routes. Everything with a
    side effect - creating the data directory, the secret key, the database and
    the device controller - happens here, so a test or tools/fakerun.py can
    point the whole application somewhere harmless.
    """
    global config, controller
    config = cfg if cfg is not None else Config()
    os.makedirs(config.data_dir, exist_ok=True)
    app.secret_key = _secret_key(config.secret_key_path)
    db.init(config.db_path)
    controller = ctrl if ctrl is not None else device.DeviceController(
        db_path=config.db_path, trace_dir=config.trace_dir)
    return app


def _secret_key(path):
    """Persist a key so sessions (and therefore flash messages) survive a
    restart. Regenerating each boot would log everyone out mid-job."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if not os.path.exists(path):
        with open(path, "wb") as fh:
            fh.write(os.urandom(32))
        os.chmod(path, 0o600)
    with open(path, "rb") as fh:
        return fh.read()


# -- helpers -------------------------------------------------------------

def conn():
    return db.connect(config.db_path)


@app.context_processor
def inject_globals():
    return {
        "device_info": controller.info(),
        "active_job": controller.current,
        "SLOT_MAX": SLOT_MAX,
        "CATEGORIES": taxonomy.CATEGORIES,
        "PLATFORMS": taxonomy.PLATFORMS,
        "GAMES": taxonomy.GAMES,
    }


def start_job(job, runner, *args):
    """Submit a job, or bounce to the one already running."""
    try:
        controller.submit(job, runner, *args)
    except device.Busy as exc:
        flash("The Disc Stakka is already busy with: %s" % exc.job.title, "warn")
        return see_other(url_for("job_page", job_id=exc.job.id))
    return see_other(url_for("job_page", job_id=job.id))


def see_other(target):
    """Redirect after POST with 303, not Flask's default 302.

    A 302 tells the client the method is preserved; browsers have ignored that
    by convention for decades and switch to GET, which is why 302 usually
    "works". Stricter browsers - the Wii U's among them - honour the spec and
    re-POST to the target, hitting a GET-only route and getting 405 Method Not
    Allowed. 303 says "follow this with GET" unambiguously, and is HTTP/1.1 so
    even NetFront on the PS3 understands it.
    """
    return redirect(target, code=303)


def _int_arg(name, default):
    try:
        return int(request.args.get(name, default))
    except (TypeError, ValueError):
        return default


# -- catalogue -----------------------------------------------------------

VIEWS = ("covers", "list")


@app.route("/", methods=["GET", "POST"])
def index():
    page = max(1, _int_arg("page", 1))
    query = (request.args.get("q") or "").strip()
    status = request.args.get("status") or None
    order = request.args.get("order") or "slot"

    if request.args.get("view") in VIEWS:
        session["view"] = request.args["view"]
    view = session.get("view", "covers")
    per_page = db.PER_PAGE_LIST if view == "list" else db.PER_PAGE

    # Both come back canonical or None, so an unknown ?category= filters on
    # nothing rather than silently returning an empty catalogue. The console is
    # dropped unless the category is games - it means nothing anywhere else.
    category = taxonomy.category(request.args.get("category"))
    platform = taxonomy.platform(category, request.args.get("platform"))

    c = conn()
    try:
        total = db.count_discs(c, query or None, status, category, platform)
        discs = db.list_discs(c, page, per_page, query or None, status, order,
                              category, platform)
        return render_template(
            "catalog.html", discs=discs, page=page,
            pages=max(1, int(math.ceil(total / float(per_page)))),
            total=total, query=query, status=status, order=order,
            category=category, platform=platform, view=view,
            category_counts=db.category_counts(c),
            platform_counts=db.platform_counts(c),
            stats=db.stats(c))
    finally:
        c.close()


@app.route("/disc/<int:disc_id>", methods=["GET", "POST"])
def disc_page(disc_id):
    c = conn()
    try:
        disc = db.get_disc(c, disc_id)
        if disc is None:
            abort(404)
        return render_template("disc.html", disc=disc)
    finally:
        c.close()


@app.route("/disc/<int:disc_id>/edit", methods=["GET", "POST"])
def disc_edit(disc_id):
    c = conn()
    try:
        disc = db.get_disc(c, disc_id)
        if disc is None:
            abort(404)

        if request.method == "POST":
            title = (request.form.get("title") or "").strip()
            if not title:
                flash("A title is required.", "error")
                return render_template("edit.html", disc=disc)

            category = taxonomy.category(request.form.get("category"))
            fields = {
                "title": title,
                "subtitle": (request.form.get("subtitle") or "").strip() or None,
                "category": category,
                "platform": taxonomy.platform(category,
                                              request.form.get("platform")),
                "notes": (request.form.get("notes") or "").strip() or None,
            }
            try:
                stem = art.ingest(request.files.get("art_file"),
                                  request.form.get("art_url"),
                                  replacing=disc["art_path"])
                if stem:
                    fields["art_path"] = stem
            except art.ArtError as exc:
                flash(str(exc), "error")
                return render_template("edit.html", disc=disc)

            db.update_disc(c, disc_id, **fields)
            flash("Saved.", "ok")
            return see_other(url_for("disc_page", disc_id=disc_id))

        return render_template("edit.html", disc=disc)
    finally:
        c.close()


@app.route("/disc/<int:disc_id>/delete", methods=["GET", "POST"])
def disc_delete(disc_id):
    c = conn()
    try:
        disc = db.get_disc(c, disc_id)
        if disc is None:
            abort(404)
        # Only the confirmation form's field deletes, so a stray or repeated
        # POST lands on the confirmation page instead.
        if request.method != "POST" or request.form.get("confirm") != "yes":
            back = (url_for("index") if request.values.get("from") == "list"
                    else url_for("disc_page", disc_id=disc_id))
            return render_template("confirm_delete.html", disc=disc, back=back)
        db.delete_disc(c, disc_id)
        art.remove(disc["art_path"])
        if disc["status"] == db.OUT:
            detail = ("It was checked out, so slot %d is no longer held for "
                      "its return." % disc["slot"])
        else:
            detail = ("Nothing was ejected, so anything physically in slot %d "
                      "is still there, but the catalogue now lists the slot "
                      "as free." % disc["slot"])
        flash("Removed “%s” from the catalogue. %s" % (disc["title"], detail),
              "warn")
        return see_other(url_for("index"))
    finally:
        c.close()


# -- hardware ------------------------------------------------------------

@app.route("/disc/<int:disc_id>/eject", methods=["POST"])
def disc_eject(disc_id):
    c = conn()
    try:
        disc = db.get_disc(c, disc_id)
        if disc is None:
            abort(404)
        if disc["status"] == db.OUT:
            flash("That disc is already checked out.", "warn")
            return see_other(url_for("disc_page", disc_id=disc_id))
        job = jobs.Job("eject", "Eject %s (slot %d)" % (disc["title"], disc["slot"]),
                       disc_id=disc_id, slot=disc["slot"])
    finally:
        c.close()
    return start_job(job, flows.run_eject, disc_id)


@app.route("/disc/<int:disc_id>/return", methods=["POST"])
def disc_return(disc_id):
    c = conn()
    try:
        disc = db.get_disc(c, disc_id)
        if disc is None:
            abort(404)
        if disc["status"] != db.OUT:
            flash("That disc is not checked out.", "warn")
            return see_other(url_for("disc_page", disc_id=disc_id))
        job = jobs.Job("return", "Return %s to slot %d"
                       % (disc["title"], disc["slot"]),
                       disc_id=disc_id, slot=disc["slot"])
    finally:
        c.close()
    return start_job(job, flows.run_add, disc["slot"], disc_id)


@app.route("/add", methods=["GET", "POST"])
def add():
    c = conn()
    try:
        if request.method == "POST":
            try:
                slot = int(request.form.get("slot", ""))
            except ValueError:
                flash("Pick a slot.", "error")
                return see_other(url_for("add"))
            if not (SLOT_MIN <= slot <= SLOT_MAX):
                flash("Slots run from %d to %d." % (SLOT_MIN, SLOT_MAX), "error")
                return see_other(url_for("add"))
            if db.get_by_slot(c, slot) is not None:
                flash("Slot %d is already spoken for." % slot, "error")
                return see_other(url_for("add"))
            job = jobs.Job("add", "Load a disc into slot %d" % slot, slot=slot)
            return start_job(job, flows.run_add, slot)

        return render_template("add.html", free=db.free_slots(c),
                               suggested=db.next_free_slot(c))
    finally:
        c.close()


@app.route("/job/<job_id>", methods=["GET", "POST"])
def job_page(job_id):
    job = controller.registry.get(job_id)
    if job is None:
        flash("That job is no longer being tracked.", "warn")
        return see_other(url_for("index"))
    return render_template("job.html", job=job.snapshot())


@app.route("/device", methods=["GET", "POST"])
def device_page():
    result = error = None
    try:
        result = controller.probe()
    except device.Busy as exc:
        error = "Busy with: %s" % exc.job.title
    except DeviceError as exc:
        error = str(exc)
    c = conn()
    try:
        events = db.recent_events(c, 25)
    finally:
        c.close()
    return render_template("device.html", probe=result, error=error,
                           events=events)


@app.route("/device/reconnect", methods=["POST"])
def device_reconnect():
    """Drop and reopen the USB handle.

    Needed after the host sleeps: the unit re-enumerates on wake and this
    process's handle is dead, which looks exactly like a broken unit until
    the handle is replaced.
    """
    try:
        controller.reconnect()
        flash("Reconnected to the unit.", "ok")
    except device.Busy as exc:
        flash("Busy with: %s" % exc.job.title, "warn")
    except DeviceError as exc:
        flash("Still cannot reach the unit: %s" % exc, "error")
    return see_other(url_for("device_page"))


@app.route("/device/reset", methods=["POST"])
def device_reset():
    return start_job(jobs.Job("reset", "Reset the unit"), flows.run_reset)


# -- reconcile -----------------------------------------------------------

@app.route("/reconcile", methods=["GET", "POST"])
def reconcile():
    c = conn()
    try:
        by_slot = {row["slot"]: row for row in db.list_discs(c, 1, SLOT_MAX)}
        slots = [(n, by_slot.get(n)) for n in range(SLOT_MIN, SLOT_MAX + 1)]
        return render_template("reconcile.html", slots=slots)
    finally:
        c.close()


@app.route("/reconcile/manual", methods=["POST"])
def reconcile_manual():
    """Record a disc the user loaded by hand, without moving the carousel.

    Needed whenever the catalogue drifts from physical reality - a disc placed
    in by hand, or a job that failed after the carousel had already acted.
    """
    c = conn()
    try:
        try:
            slot = int(request.form.get("slot", ""))
        except ValueError:
            flash("Pick a slot.", "error")
            return see_other(url_for("reconcile"))
        title = (request.form.get("title") or "").strip()
        if not title:
            flash("A title is required.", "error")
            return see_other(url_for("reconcile"))
        if db.get_by_slot(c, slot) is not None:
            flash("Slot %d already has an entry." % slot, "error")
            return see_other(url_for("reconcile"))

        disc_id = db.create_disc(c, slot, title, kind="manual")
        flash("Recorded “%s” in slot %d." % (title, slot), "ok")
        return see_other(url_for("disc_edit", disc_id=disc_id))
    finally:
        c.close()


# -- optional JSON, only for enhance.js on modern browsers ---------------

@app.route("/job/<job_id>.json")
def job_json(job_id):
    job = controller.registry.get(job_id)
    if job is None:
        return {"error": "unknown job"}, 404
    return job.snapshot()


@app.errorhandler(404)
def not_found(_exc):
    return render_template("error.html", code=404,
                           message="No such page."), 404


@app.errorhandler(413)
def too_large(_exc):
    return render_template("error.html", code=413,
                           message="That upload is too large."), 413


if __name__ == "__main__":
    # 0.0.0.0 so the PS3 can reach it. threaded=True lets a poll request be
    # served while a job runs; the device itself stays single-owner.
    #
    # Not port 5000: on macOS that belongs to Control Center's AirPlay
    # Receiver, which answers every request with a bare 403.
    created = create_app()
    created.run(host="0.0.0.0", port=config.port, threaded=True, debug=False)
