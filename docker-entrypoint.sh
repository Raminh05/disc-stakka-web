#!/bin/sh
# Preflight, then hand over to the app.
#
# None of this can be reported from inside the app. hidapi raises a bare IOError
# with no errno, so every reason the unit cannot be opened arrives at the pages
# as the same "present, not connected" and nothing says which one it was.
set -e

VENDOR=0718
PRODUCT=d000

# usb_device. Fixed, unlike hidraw's, so a mismatch here means the kernel is
# unusual rather than that the rule has drifted.
USB_MAJOR=189

say() { echo "preflight: $*"; }

# Fatal: create_app() writes data/secret_key at startup, so the alternative is a
# PermissionError traceback on every restart.
if [ ! -w /app/data ]; then
    say "/app/data is not writable by uid $(id -u). On the host, run:"
    say "    sudo chown -R $(id -u):$(id -g) data"
    exit 1
fi

if [ ! -w /app/static/art ]; then
    say "/app/static/art is not writable, so cover art uploads will fail:"
    say "    sudo chown -R $(id -u):$(id -g) art"
fi

# The supplementary group list is how you see whether group_add landed.
say "$(id)"

# The app opens the unit through libusb, so the node is the usbfs one and its
# device number changes on every replug.
node=""
for dev in /sys/bus/usb/devices/*; do
    [ -f "$dev/idVendor" ] || continue
    [ "$(cat "$dev/idVendor")" = "$VENDOR" ] || continue
    [ "$(cat "$dev/idProduct")" = "$PRODUCT" ] || continue
    node="/dev/bus/usb/$(printf '%03d/%03d' "$(cat "$dev/busnum")" "$(cat "$dev/devnum")")"
    break
done

if [ -z "$node" ]; then
    say "no Disc Stakka on the USB bus: either the unit is unplugged, or /dev/bus/usb is not mounted."
elif [ ! -e "$node" ]; then
    say "the unit is on the bus but $node is missing, so /dev/bus/usb is not the host's."
else
    say "$node $(stat -c 'mode=%a owner=%u:%g major=%t minor=%T' "$node")"

    major=$((0x$(stat -c %t "$node")))
    if [ "$major" != "$USB_MAJOR" ]; then
        say "major is $major, expected $USB_MAJOR. The cgroup rule will not cover it."
    fi

    # Opening a usbfs node claims nothing and sends no control transfer, so this
    # moves nothing. The two denials carry different errnos, and that is the only
    # thing that tells them apart once the app has swallowed them.
    if err=$( (exec 3<>"$node") 2>&1 ); then
        say "opened $node read-write."
    else
        case "$err" in
        *"Operation not permitted"*)
            say "cannot open $node: the device cgroup denies it."
            say "    Check device_cgroup_rules covers major $USB_MAJOR." ;;
        *"Permission denied"*)
            say "cannot open $node: file permissions deny it."
            say "    Check the udev rule's group against group_add in docker-compose.yml." ;;
        *)
            say "cannot open $node: $err" ;;
        esac
    fi
fi

exec "$@"
