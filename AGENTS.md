# AGENTS.md

Guidance for coding agents working in this repository. Read `README.md` first:
it explains the design and why it is the way it is.

## What this is

A Flask web catalogue for the Imation Disc Stakka USB CD carousel. You can
browse discs, eject one, or load a new one from any browser on the LAN. The
oldest browser it has to support is the PlayStation 3's NetFront.

| Path | What |
|---|---|
| `app.py` | Flask routes, and `create_app()` |
| `pyproject.toml` | Ruff config and the dev dependency group |
| `discstakka/config.py` | Where state lives: data dir, db, traces, art, port |
| `discstakka/transport.py` | The only module that imports `hid` |
| `discstakka/simulator.py` | The simulated carousel, calibrated from `data/traces` |
| `discstakka/protocol.py` | HID wire protocol (a port of `discstakka.c`) |
| `discstakka/slots.py` | `SLOT_MIN`, `SLOT_MAX`, `HOME` |
| `discstakka/device.py` | The single worker thread that owns the device |
| `discstakka/flows.py` | The eject, add and reset flows |
| `discstakka/trace.py` | Per-job record of the status stream, and a null one |
| `discstakka/jobs.py` | Job phases and the in-memory registry |
| `discstakka/catalog/db.py` | SQLite access and startup migrations |
| `discstakka/catalog/taxonomy.py` | The fixed category and console lists |
| `discstakka/catalog/art.py` | Cover art ingest (upload or URL), with SSRF guards |
| `discstakka/catalog/schema.sql` | Schema. Every statement is `IF NOT EXISTS` |
| `static/base.css` | Baseline styles, CSS 2.1 only |
| `static/modern.css` | Enhancements, all inside `@supports` |
| `static/enhance.js` | Optional XHR polling. Nothing depends on it |
| `tools/ps3lint.py` | Checks served pages and CSS for things the PS3 can't handle |
| `tools/fakerun.py` | The real app against a simulated carousel, no hardware |
| `tests/clock.py` | Virtual clock, so the real 30 s timeouts cost no wall time |
| `.github/workflows/ci.yml` | Suite, suite-without-hidapi, and the image build |
| `Dockerfile` | Container image, Linux hosts only |
| `docker-compose.yml` | Devices, volumes, and the hidraw cgroup rule |
| `docker-entrypoint.sh` | Preflight: permissions and device diagnosis, then exec |
| `deploy/99-discstakka.rules` | Host udev rule for `0718:d000`, installed by hand |

## Running and checking

Use a virtual environment at `.venv` built from **Python 3.14**. Don't use an
operating system's bundled Python.

```sh
python3.14 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m pip install --group dev    # Ruff
```

Run everything below from the project root with that environment's interpreter,
either by joining it (`. .venv/bin/activate`) or by calling `.venv/bin/python`
directly. `python` below means whichever you chose.

```sh
python -m unittest discover -s tests -t .   # no hardware, no network, ~3 s
ruff check . && ruff format --check .
python tools/fakerun.py                # the app against a simulated carousel
python app.py              # serves on 0.0.0.0:5050; DISCSTAKKA_PORT changes the port
python tools/ps3lint.py    # needs the server running
```

- Talking to the unit needs USB HID access for the account running the app.
  On Linux that usually means a udev rule for `0718:d000`.
- Run the suite after any change. It needs no hardware and does not touch
  `data/`; `DISCSTAKKA_DATA` points the whole app at a scratch directory.
- After a UI change also run `ps3lint.py`, either against `fakerun.py` or
  against a real server. `tests/test_ps3lint.py` does this for you.
- The simulated carousel is calibrated against the captured runs in
  `data/traces`. If you change its timings or state machine, keep
  `test_the_simulated_run_matches_a_captured_one` passing - it is the only
  thing tying the mock to the real hardware.
- `simulator.py` transcribes the opcodes and status bits rather than importing
  them from `protocol.py`. A mock that shares its constants with the code under
  test cannot disagree with it. Keep it that way.
- Nothing above `discstakka/transport.py` may import `hid`. The `no-hidapi` CI
  job exists to catch that.
- **Do not trigger hardware actions without asking.** That covers eject,
  return, add, reset, and `python -m discstakka.protocol`, because they move a
  real carousel. Browsing pages, `/device`, and the lint are safe.
- Leave `data/` alone (`catalog.db`, `secret_key`, `traces/`). It holds the
  user's real catalogue and device logs.

## Hard constraints

These are load-bearing. Breaking one fails silently, on hardware or on a
console you can't see.

**Process model**
- Run single-process. The HID handle and the job registry live in memory.
  Never add multi-worker WSGI configs.
- Web requests never touch the device directly. They submit a `jobs.Job` via
  `controller.submit` and redirect to the job page, which polls it.
- The container runs the same way: its `CMD` is `python app.py`. Never put
  gunicorn or uwsgi in the image.

**PS3 / old-browser compatibility**
- Pages are server-rendered, forms use POST, and no JavaScript is required
  anywhere. `enhance.js` has to stay optional and must not throw.
- Redirect after a POST with `see_other()` (303), never plain `redirect()`.
- Read-only routes accept both `GET` and `POST`. Routes that change state
  accept `POST` only.
- Long operations poll with `<meta http-equiv="refresh">`. The tag is left out
  once the job is done.
- Markup rules:
  - Use `<input type="submit">`, not `<button>`.
  - No inline `on*=` handlers.
  - No HTML5 sectioning elements and no HTML5 input types.
  - No `<canvas>`, `<svg>`, `<video>`, or `<audio>`.
  - Declare the charset with the `http-equiv` meta, not `<meta charset>`.
  - Lay pages out with tables.
- `base.css` must stay CSS 2.1. That rules out flex, grid, `var()`, `rem`,
  `vh`, `vw`, `rgb()`, `rgba()`, `calc()`, media queries, and CSS3 selectors.
  Modern CSS goes in `modern.css`, inside `@supports`. `base.css` has to work
  on its own.
- Cover art is always re-encoded to baseline JPEG through `art.store`. Never
  serve WebP or progressive JPEG.
- Categories and consoles are closed lists and use `<select>`. Pass input
  through `taxonomy.category()` and `taxonomy.platform()`.

**Protocol**
- The four rules at the top of `protocol.py` each came from a real bug. Do not
  "simplify" them away:
  - Re-request state on every poll.
  - Check `CARRIES_STATUS`.
  - Home before any positional move.
  - Never send while BUSY.
- Keep the retries in `require()`, the settle wait in `ingest()`, and the
  re-enumeration in `open()`.

**Database**
- A slot is occupied when a row references it. A disc that is checked out
  keeps its slot.
- Schema changes need a migration in `db._migrate`, because `schema.sql` never
  alters an existing table.
- Log hardware actions to the `event` table.

**Container**
- Linux hosts only, and rootful Docker. Docker Desktop on macOS cannot pass the
  unit through, and rootless cannot delegate the device cgroup.
- The device arrives through **libusb, not hidraw**. On Linux the `hidapi` wheel
  ships two modules, and `protocol.py` imports `hid`, which is the libusb one - so
  the node is `/dev/bus/usb/<bus>/<dev>` and the cgroup rule is major 189. The
  build asserts it from the compiled module's symbols, not from a library name:
  auditwheel renames the vendored libusb, so matching on the filename never fires.
  If a release flips `hid` to hidraw, the usbfs access is useless and silent. Fix
  the pass-through; don't drop the assertion.
- The whole `/dev/bus/usb` tree is mounted so a node that re-enumerates after a
  sleep is visible at all. `device_cgroup_rules` permits major 189 as a *class*, so
  the udev rule's group on this unit's node is what narrows it to the carousel -
  which only works while the app runs non-root.
- Cover art mounts at `static/art`. Mounting over `static/` hides `base.css` and
  `modern.css`, and every page loses its styling.
- Set `TZ`. `catalog/db.py` stamps rows with a naive `datetime.now()`, so a
  container on UTC writes times the pages then present as local.
- `schema.sql` is package data at `discstakka/catalog/`, read through
  `importlib.resources` on every `db.init()`. `COPY discstakka/` carries it;
  there is no separate line to forget.
- Keep `init: true`. Python as PID 1 is never sent a default-action SIGTERM, so
  without it every `compose stop` stalls to the timeout and then SIGKILLs.
- Templates and static files are baked in, so `TEMPLATES_AUTO_RELOAD` does
  nothing there. Editing either needs a rebuild.
- One owner at a time. A native `app.py` and the container will both open the
  device, and the libusb backend detaches the kernel driver while it holds it.
- `docker-entrypoint.sh` is a preflight, not a supervisor. It reports what the app
  cannot - hidapi loses the errno, so a wrong group and a missing cgroup rule are
  indistinguishable from inside - and then `exec`s the server, which is what keeps
  signals reaching it. Only an unwritable `data/` is fatal. Opening the node there
  claims nothing and sends no transfer, so it is not a hardware action.
- `docker compose run --rm discstakka true` runs the preflight and stops, which is
  the safe way to check permissions without starting anything.

## Code style

Match the surrounding code:
- `%`-formatting.
- Plain `sqlite3` with `?` parameters, and `with conn:` for writes.
- Open connections in `try`/`finally`.
- Flash categories are `ok`, `warn`, and `error`.
- User-facing messages are plain full sentences.

Only `app` and `discstakka` are importable at the top level. Keep it that
way: anything new belongs inside the package.

Ruff's `UP` rules are deliberately not enabled - they would rewrite the
`%`-formatting and `class X(object)` this file mandates. Don't add them.

No new dependencies without a clear need. Pin them; the container build
asserts hidapi is still the libusb backend. The dataset is at most 100 rows, so
choose readable over clever.

## Comments: do not over-comment

**Do not over-comment the code.** Most lines need no comment at all.

- Write a comment only when the *why* is not obvious from the code. Examples:
  a hardware quirk, a browser limitation, a bug that turned into a rule, or a
  security trade-off.
- Don't narrate what the code does. Don't restate the function name, and don't
  label obvious steps (`# open the connection`, `# loop over slots`).
- Don't add docstrings to small or self-explanatory functions. One line is
  usually enough when a docstring is warranted.
- No commented-out code. No change-log comments (`# changed X to Y`,
  `# fixed bug`, `# new:`). No TODOs unless asked. That history belongs in the
  commit message or your reply.
- Don't add section-divider comments beyond the existing `# -- name ---`
  headers.
- When you edit code, don't pile comments onto it. If a clearer name removes
  the need for a comment, rename instead.
- Keep the existing comments that record hardware or browser behaviour (the
  protocol rules, why 303, why re-encode). They are the kind worth having. Just
  don't add more of that length for ordinary code.
