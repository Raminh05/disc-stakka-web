# Linux hosts only. Docker Desktop on macOS has no USB pass-through, so macOS
# keeps running app.py natively.
FROM python:3.14-slim AS build

# Pinned like any other build input. Upgrading uv is a deliberate act.
COPY --from=ghcr.io/astral-sh/uv:0.12.17 /uv /bin/uv

# Installed from uv.lock, not from requirements.txt, so the image gets exactly
# the resolution the suite ran against and there is no export left to go stale.
# `package = false` in pyproject means this installs the dependencies and does
# not try to build the application, which has no wheel to build.
ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

# --no-build: every locked dependency is either pure Python or ships a cp314
# manylinux wheel for x86_64 and aarch64, so there is no compiler, no Python
# headers and no libusb/libjpeg dev packages in this stage. If a future version
# bump lands on something without a wheel, this fails here saying so, rather
# than quietly compiling from an sdist. Putting build-essential, python3-dev,
# pkg-config, libusb-1.0-0-dev, libudev-dev, libjpeg-dev and zlib1g-dev back is
# the fix if that day comes.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-build

ENV PATH=/opt/venv/bin:$PATH

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

# No libusb package here either. The manylinux wheel vendors its own, renamed by
# auditwheel, and ldd on hid's .so resolves libusb and libudev inside
# site-packages/hidapi.libs - nothing system-wide. That used to matter on the
# sdist path, which --no-build in the build stage has closed off.
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
