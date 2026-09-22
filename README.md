# Disc Stakka web catalogue

A replacement for Imation's discontinued **Opditracker**, for the Imation Disc
Stakka USB CD carousel. Browse a catalogue, ask for a disc and the carousel
ejects it, add new discs and it takes them in — from any browser, including the
PlayStation 3's.

Built on the protocol work of the **Disc Stakka Controller for Linux**
project. See [Prior work](#prior-work).

## Running it

```sh
./.venv/bin/python app.py          # http://0.0.0.0:5050
```

Then browse to `http://<this-machine>:5050/` from anywhere on the LAN.
Set `DISCSTAKKA_PORT` to move it. Port 5000 is deliberately avoided:
on macOS that is Control Center's AirPlay Receiver, which answers
every request with a bare 403.

**Run it single-process.** The HID handle and the job registry live in memory,
so a multi-worker WSGI server would hand each worker its own device and they
would fight over it. Run `app.py` directly; do not put gunicorn in front of it.

First run creates `data/catalog.db` and a persistent session key.

## Running it in a container (Linux only)

Not on macOS: Docker Desktop's VM has no USB pass-through, so there running
`app.py` natively remains the only way. It also needs **rootful** Docker — rootless cannot delegate
a device cgroup, and supplementary groups don't cross the user namespace, so both
halves of the permission story below stop working.

```sh
sudo groupadd -g 2718 discstakka
sudo cp deploy/99-discstakka.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules && sudo udevadm trigger
lsusb -d 0718:d000                  # note the bus and device numbers
ls -l /dev/bus/usb/*/*              # that node: group discstakka, mode 660

mkdir -p data art && sudo chown -R 1000:1000 data art
docker compose up -d
```

Then the same `http://<this-machine>:5050/` as before. The first `up` creates an
empty catalogue; `data/` and `art/` are bind-mounted, so the database, the session
key and the cover art outlive the container. Keep `data/` on a local filesystem —
SQLite's WAL needs real shared memory and will not work over NFS or SMB.

Three things here are not the obvious choice, and each is load-bearing:

- **It goes through libusb, not hidraw.** The PyPI `hidapi` wheel ships two
  modules on Linux: `hid`, which is the libusb backend, and a separate `hidraw`.
  `protocol.py` imports `hid`, so the unit is opened as
  `/dev/bus/usb/<bus>/<dev>`, the cgroup rule is on major 189 (`usb_device`), and
  the udev rule matches the USB device rather than a hidraw node. The image build
  asserts this by inspecting the compiled module's symbols, because if a release
  ever flips `hid` to the hidraw backend then the usbfs access becomes useless and
  nothing would say so — the pages would just report the unit as present and not
  connected.
- **The whole `/dev/bus/usb` tree is mounted, not one node.** The unit
  re-enumerates when the host sleeps and comes back with a different device
  number, and `open()` finds it again by VID/PID. A `--device` bind resolves one
  path at container start, so it would work until the first replug and then never
  again — it would manufacture the permanent version of the failure described
  under *After the host sleeps*.
- **Two separate gates decide access, and the narrow one is the udev rule.**
  `device_cgroup_rules` is per *major*, not per device: major 189 is every USB
  device on the host. What stops the app touching the others is ordinary file
  permissions — the rule gives this unit's node group `discstakka` and the
  container joins that group, while every other usbfs node keeps its default
  `root:root` ownership and is not writable by the container's user. This only
  holds because the app runs as a non-root user; run it as root in the container
  and the coarse cgroup rule becomes read/write access to every USB device on the
  machine.

`TZ` is not cosmetic either: `catalog/db.py` stamps rows with a naive
`datetime.now()`, so a container left on UTC writes timestamps four or five hours
off, which the pages then present as local time.

### Two rules

**One owner at a time.** Nothing stops a native `app.py` on the host and the
container from both holding the device, and the libusb backend detaches the kernel HID
driver while it has the unit claimed. The unit also latches its last reply and
repeats it, so two pollers quietly steal each other's answers — intermittent, and
hard to attribute to anything. Publishing port 5050 catches the case where both
use the same port, and nothing catches the rest.

**Don't stop or restart it mid-job.** The job registry is in memory, so the page
you were watching answers 404 afterwards while the carousel finishes whatever it
was doing. The trace in `data/traces/` and the `event` table are what is left.

### When the unit is plugged in but the pages say otherwise

`hid.enumerate` reads sysfs and succeeds on a node the process cannot open, so a
permission problem renders as "present, not connected" with no error anywhere:
hidapi raises a bare `IOError` and no errno reaches Python. The preflight in
`docker-entrypoint.sh` reports at every start what the app is unable to.

```sh
docker compose logs discstakka | grep preflight
```

It prints the container's own uid and groups, the unit's usbfs node with its mode
and owner, and whether that node opens at all. When it does not, the errno says
which gate refused, and they have different fixes:

- *the device cgroup denies it* — `device_cgroup_rules` is missing or does not
  cover major 189.
- *file permissions deny it* — the udev rule did not take, or its group is not the
  one in `group_add`.

To run the preflight without starting the server:

```sh
docker compose run --rm discstakka true
```

Opening the node claims no interface and sends no control transfer, so none of
this moves the carousel. If the preflight finds no unit on the bus at all while it
is plugged in, then `/dev/bus/usb` is not reaching the container;
`docker compose exec -T discstakka python -c "import hid;
print(hid.enumerate(0x0718, 0xd000))"` asks the same question through hidapi.

On a host with SELinux enforcing, a bind-mounted `/dev/bus/usb` is not relabelled
and `container_t` is denied outright; that needs `security_opt: ["label=disable"]`,
which is why it isn't there by default.

## The PS3 constraint, and what it forced

The PS3's NetFront browser gained only "limited HTML5 support" in firmware
4.10, has a badly crippled JS DOM, and its AJAX support was never verified
working. So:

- **Server-rendered HTML, form POSTs, redirect-after-POST.** No JS required
  anywhere.
- **Long hardware operations are jobs polled with `<meta http-equiv="refresh">`.**
  The tag is emitted while a job runs and simply omitted once it finishes, so
  the page stops reloading by itself. This is the pre-AJAX pattern and it works
  on every browser ever shipped.
- **`base.css` is CSS 2.1 only** — no flexbox, grid, custom properties, `rem`,
  media queries or `rgba()`. It is sized for a television at couch distance.
- **`modern.css` lives entirely inside `@supports`**, which old parsers skip
  wholesale, so modern browsers get a real grid layout from the same markup.
- **Redirect-after-POST uses 303, not 302.** A 302 tells the client the method
  is preserved; browsers have ignored that by convention for decades and switch
  to GET, which is why 302 usually "works". Stricter browsers honour the spec
  and re-POST to the target — the Wii U's browser does exactly this, hitting a
  GET-only route and reporting *method not allowed* while the carousel moves
  anyway, because the original POST had already landed. 303 says "follow with
  GET" unambiguously and is HTTP/1.1, so NetFront understands it too.
- **Read-only pages accept POST as well as GET.** Rendering a page is a read;
  the verb is irrelevant. This means even a client that ignores 303 semantics
  entirely gets the page instead of a 405. Cheap insurance for browsers that
  cannot be tested here.
- **`enhance.js` is optional.** It upgrades job polling to XHR where that
  works, and is written so a parse or runtime failure cannot break the page.

Cover art is always re-encoded to baseline JPEG at two fixed sizes. The PS3 has
no WebP and copes badly with progressive JPEG and large images.

## After the host sleeps

USB drops when the machine sleeps, and the unit re-enumerates on wake. Any job
in flight fails with `write failed`, and the old handle is dead — which looks
exactly like a broken unit, because the process can never open it again even
though a fresh process can.

`open()` handles this by re-enumerating (which refreshes hidapi's stale device
cache) and retrying, so it normally self-heals. If it does not, **`/device` has
a Reconnect button** that drops and reopens the handle.

The unit loses its homed state across a power cycle, so the first positional
move after one will home the carousel first. That is automatic.

## Layout

| Path | What |
|---|---|
| `discstakka/transport.py` | The only module that imports `hid` |
| `discstakka/protocol.py` | HID protocol, ported from `discstakka.c` |
| `discstakka/device.py` | Single worker thread owning the device |
| `discstakka/flows.py` | The eject, add and reset flows |
| `discstakka/jobs.py` | Job phases and registry |
| `discstakka/trace.py` | Per-job record of what the unit reported |
| `config.py` | Data directory, database, traces, secret key, port |
| `catalog/db.py` | SQLite catalogue |
| `catalog/taxonomy.py` | The categories, and the console list for games |
| `catalog/art.py` | Cover art ingest (upload + URL), with SSRF guards |
| `templates/job.html` | The meta-refresh polling page |

## Testing without a carousel

`tests/fake_device.py` is a simulated Disc Stakka sitting behind the transport
interface, so the real protocol, flows and routes run against it unchanged.
Its timings and state machine come from the 25 captured runs in `data/traces`,
and one test checks that a simulated load still produces the same status
signature as a recorded one.

```sh
python -m unittest discover -s tests   # ~3 s, no hardware, no network
python tools/fakerun.py                # the app, browsable, with a fake unit
```

`fakerun.py` gives you the physical half on stdin: `i` to insert a disc, `t` to
take one, `u` and `r` to unplug and replug, `b` to switch on the idle blip that
makes commands go missing. It serves from a scratch directory, so the real
catalogue is never involved.

Two things the simulator is honest about. Every captured trace is a load or a
return, so `DISC_IN_BAY` and `ACK_TIMEOUT` are modelled from what the client
expects rather than from evidence - eject is now traced, so a single real
ejection would fix that. And `0x4000` appears in five traces, decoded by
neither this code nor the 2005 daemon; it is reproduced as a latched bit so the
suite can prove no mask is confused by it.

## Protocol notes worth keeping

Four rules in `protocol.py` are not obvious from the 2005 sources. Each was a
real bug before it was a rule:

1. The unit **latches its last reply and repeats it** ~7/sec rather than pushing
   fresh status — callers must re-request state every poll.
2. **Only some replies carry status flags.** A stale `0x1B` version reply
   decodes to `0x0200`, which passes `& ST_BUSY == 0` and silently satisfies any
   wait-for-not-busy. See `CARRIES_STATUS`.
3. **Home before any positional move.** While bit 15 is clear the unit silently
   ignores `0x04` to any slot but 0 — no NAK, no error, just no ack.
4. **Never send while BUSY.** Same silent drop.

Also: `0x06`'s reply carries the status word, which `readme.txt` records as
"Unknown!", and `0x0C` performs a firmware reset — the "define reset api?"
`bugs.txt` asks for.

## Classifying discs

A disc is classified by **what is on it** — *Video games, Music, Movies,
Software, Other* — and a game additionally by **which console it is for**
(`catalog/taxonomy.py` holds both lists). That is the question actually being
asked in front of a hundred discs; the old CD/DVD/Blu-ray/Data field described
the plastic and answered nothing.

Both are closed lists and both are `<select>`s, because the PS3 has no
`<datalist>` and free text typed blind on a television does not group into a
filter worth having. The console is dropped for anything that is not a game, so
a disc reclassified from a game to a film cannot stay a Wii disc.

The catalogue page filters on them with plain links rather than another
`<select>`: one press on a game pad, and the current filter is readable from the
sofa. Consoles only appear as a second row once *Video games* is selected, and
only those that actually hold a disc.

Existing databases migrate on startup (`db._migrate`). `media_type` becomes
`category` + `platform`; only *Data* carries over, as *Software*. CD, DVD and
Blu-ray said nothing about content, so those discs arrive unclassified rather
than guessed at.

## Prior work

None of this would exist without **Disc Stakka Controller for Linux** by Eddie
Cornejo, released in 2005 under the GPL:

> <https://disc-stakka-ctl.sourceforge.net>

Imation never published a protocol. Cornejo put a USB protocol analyser on the
wire and worked it out: the report sizes and packet layout, the opcodes, the
status bits, the message-ID pairing, and the 50 ms poll the unit demands before
it resets itself. `discstakka/protocol.py` here is a port of that work, by way
of the 0.04 tarball's `src/standalone/discstakka.c` and its `readme.txt`, and
the constants in `tests/fake_device.py` are written out from the same source.

Two things in the 0.03 `README` are still the best description of failures this
code has to handle twenty years later: the unit resetting itself roughly every
2.5 seconds when nothing polls it, and it accepting a CD and then ejecting it
again when the host does not say what to do quickly enough. The second is the
whole reason `ingest()` waits before sending `0x1D`.

What this project adds is a catalogue, a web interface old browsers can use,
and the four rules under *Protocol notes worth keeping*, which are not in the
2005 sources.

## Keeping the catalogue honest

The carousel cannot report what is physically in a slot, so the database is a
belief, not a fact. **Reconcile** shows that belief, lets you correct it, and
records discs loaded by hand without moving anything. Every hardware action is
written to the `event` table.

A disc that is checked out **keeps its slot reserved** so it has somewhere to go
back to. "Put this disc back" re-runs the load flow against that slot.

## AI assistance

Parts of this project were written with Claude (Anthropic). `git log` shows
which commits: they carry a `Co-Authored-By` trailer. The first commit is an
import of pre-existing code and only the commit itself was made that way.

What that should and should not buy you:

- **The protocol is not invented.** It is a port of the 2005 reverse
  engineering described under [Prior work](#prior-work), and the four rules in
  `protocol.py` each came from a failure on a real carousel. The evidence is in
  `data/traces`.
- **The simulator is calibrated for loading and returning, not for ejecting.**
  Every captured run on hand is a load or a return, so `DISC_IN_BAY` and
  `ACK_TIMEOUT` are modelled from what the client code expects rather than from
  anything observed. A green suite says the code agrees with the model; for the
  eject path it does not say the model agrees with the hardware. Eject is now
  traced, so one real ejection would settle it.
- **Anything that moves the carousel deserves a run on the real unit** before it
  is trusted, whoever or whatever wrote it.
