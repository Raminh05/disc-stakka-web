# Linux hosts only. Docker Desktop on macOS has no USB pass-through, so macOS
# keeps running app.py natively.
FROM python:3.14-slim AS build

# hidapi has no wheel for every interpreter, and pip then builds it from the
# sdist: that needs a compiler, Python headers, and libusb plus libudev. The same
# goes for Pillow and libjpeg/zlib. None of it belongs in the runtime image.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        python3-dev \
        pkg-config \
        libusb-1.0-0-dev \
        libudev-dev \
        libjpeg-dev \
        zlib1g-dev \
    && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# On Linux this wheel ships two modules: hid (libusb backend) and hidraw (hidraw
# backend). protocol.py imports hid, so the container needs usbfs at /dev/bus/usb,
# which is what docker-compose.yml provisions. If a future release flips hid to
# the hidraw backend, that access becomes useless and nothing would say so - the
# pages would just report the unit as present and not connected. Check the
# symbols, not the library filename: auditwheel renames the vendored libusb.
RUN so="$(python -c 'import hid; print(hid.__file__)')" \
    && if grep -aq libusb_claim_interface "$so"; then \
         echo "hid backend: libusb, needs /dev/bus/usb"; \
       else \
         echo "hid is no longer the libusb backend; the compose file grants usbfs" >&2; \
         echo "access and would never find the unit. Rework the pass-through." >&2; \
         exit 1; \
       fi \
    && python -c "import flask, requests, hid; from PIL import Image"


FROM python:3.14-slim

# The manylinux wheel vendors its own libusb and libudev, so this matters only on
# the sdist path - where leaving it out is an ImportError on the first boot.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libusb-1.0-0 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=build /opt/venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1

# These have to match the owner of the data/ and art/ bind mounts on the host.
ARG APP_UID=1000
ARG APP_GID=1000
RUN groupadd -g $APP_GID stakka \
    && useradd -u $APP_UID -g $APP_GID -M -d /app stakka

WORKDIR /app
# app.py is the only root file the image needs. Everything else - config.py,
# catalog/ and schema.sql - travels inside the package, so a new module cannot
# be left out of a COPY line and fail on first boot.
COPY app.py ./
COPY discstakka/ discstakka/
COPY templates/ templates/
COPY static/ static/
COPY tools/ tools/
COPY docker-entrypoint.sh /usr/local/bin/

RUN mkdir -p data static/art && chown -R $APP_UID:$APP_GID data static/art

USER stakka
EXPOSE 5050

# The preflight execs this, so the server still ends up as the signalled process.
ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]

# Single process, deliberately: the HID handle and the job registry live in
# memory, so a multi-worker server would give each worker its own device.
CMD ["python", "app.py"]
