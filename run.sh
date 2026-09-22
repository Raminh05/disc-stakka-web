#!/bin/sh
# Start the catalogue on the LAN so the PS3 can reach it.
#
# Single process, deliberately: the HID handle and the job registry live in
# memory, so a multi-worker server would give each worker its own device.
cd "$(dirname "$0")"
exec ./.venv/bin/python app.py
