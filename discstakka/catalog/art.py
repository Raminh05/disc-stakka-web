"""Cover art ingest.

Two entry points - an uploaded file and a pasted URL - converge on one
pipeline: decode, flatten to RGB, resize to two fixed sizes, re-encode as
baseline JPEG with metadata stripped.

Re-encoding is not optional. It normalises whatever was supplied into something
the PlayStation 3 browser can actually render (it has no WebP, and copes badly
with progressive JPEG and large images), and it means we never serve bytes a
stranger's server handed us.
"""

import io
import ipaddress
import os
import socket
import uuid
from urllib.parse import urlparse

import requests
from PIL import Image

from ..config import Config

THUMB_PX = 160
FULL_PX = 500
JPEG_QUALITY = 85

MAX_BYTES = 8 * 1024 * 1024
FETCH_TIMEOUT = 10
MAX_REDIRECTS = 3

#: Where cover art is written. Config owns it because it has to match the
#: folder Flask serves and the compose bind mount, neither of which follows
#: DISCSTAKKA_DATA. Same fallback-at-import shape as db.DEFAULT_PATH.
ART_DIR = Config().art_dir


class ArtError(Exception):
    """Anything that stops us producing usable art. Message is user-facing."""


# -- fetching ------------------------------------------------------------


def _check_public(host):
    """Reject anything resolving to a non-public address.

    Guards the obvious SSRF: this server can reach the user's LAN and its own
    loopback, and a pasted URL is attacker-controlled input in the general case.

    Not airtight - a hostile DNS server could answer differently here than at
    connect time (rebinding). Closing that needs connecting to a pinned IP with
    an explicit Host header, which is more machinery than a home-LAN catalogue
    warrants. Documented rather than silently ignored.
    """
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise ArtError("Could not resolve %s" % host) from exc

    for info in infos:
        addr = ipaddress.ip_address(info[4][0])
        if (
            addr.is_private
            or addr.is_loopback
            or addr.is_link_local
            or addr.is_reserved
            or addr.is_multicast
            or addr.is_unspecified
        ):
            raise ArtError(
                "That URL points at a private or local address (%s), which "
                "this server will not fetch." % addr
            )


def fetch_url(url):
    """Download an image URL into bytes, with the guards above applied."""
    seen = 0
    while True:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            raise ArtError("Only http:// and https:// URLs are supported.")
        if not parsed.hostname:
            raise ArtError("That does not look like a URL.")
        _check_public(parsed.hostname)

        resp = requests.get(
            url,
            stream=True,
            timeout=FETCH_TIMEOUT,
            allow_redirects=False,
            headers={"User-Agent": "discstakka-web/1.0"},
        )

        if resp.is_redirect or resp.is_permanent_redirect:
            seen += 1
            if seen > MAX_REDIRECTS:
                raise ArtError("Too many redirects.")
            url = resp.headers.get("Location", "")
            resp.close()
            if not url:
                raise ArtError("Redirect without a destination.")
            continue  # re-validate the new host before following

        if resp.status_code != 200:
            raise ArtError("That URL returned HTTP %d." % resp.status_code)

        ctype = resp.headers.get("Content-Type", "").split(";")[0].strip()
        if not ctype.startswith("image/"):
            raise ArtError(
                "That URL is %s, not an image." % (ctype or "an unknown type")
            )

        data = bytearray()
        for chunk in resp.iter_content(64 * 1024):
            data.extend(chunk)
            if len(data) > MAX_BYTES:
                resp.close()
                raise ArtError(
                    "That image is larger than %d MB." % (MAX_BYTES // (1024 * 1024))
                )
        resp.close()
        if not data:
            raise ArtError("That URL returned an empty response.")
        return bytes(data)


# -- rendering -----------------------------------------------------------


def _flatten(img):
    """RGB on white. JPEG has no alpha, and a black fill looks broken."""
    if img.mode in ("RGBA", "LA", "P"):
        img = img.convert("RGBA")
        bg = Image.new("RGB", img.size, (255, 255, 255))
        bg.paste(img, mask=img.split()[-1])
        return bg
    return img.convert("RGB")


def store(data, stem=None):
    """Write thumb + full JPEGs and return the stem to record in the DB.

    The stem carries a random suffix so the filename changes whenever art is
    replaced. Old browsers cache aggressively and the PS3 is no exception;
    a new name sidesteps that entirely.
    """
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
    except Exception as exc:
        raise ArtError("That file is not an image this server can read.") from exc

    img = _flatten(img)
    os.makedirs(ART_DIR, exist_ok=True)
    stem = stem or uuid.uuid4().hex[:12]

    for suffix, px in (("thumb", THUMB_PX), ("full", FULL_PX)):
        copy = img.copy()
        copy.thumbnail((px, px), Image.LANCZOS)
        # No exif= argument, so metadata is dropped on the way out.
        copy.save(
            os.path.join(ART_DIR, "%s_%s.jpg" % (stem, suffix)),
            "JPEG",
            quality=JPEG_QUALITY,
            optimize=True,
            progressive=False,
        )
    return stem


def remove(stem):
    if not stem:
        return
    for suffix in ("thumb", "full"):
        path = os.path.join(ART_DIR, "%s_%s.jpg" % (stem, suffix))
        try:
            os.remove(path)
        except OSError:
            pass


def ingest(upload=None, url=None, replacing=None):
    """Take whichever source was supplied and return a new stem, or None.

    ``upload`` is a Werkzeug FileStorage; ``url`` a string. If both are empty
    the caller's art is left alone.
    """
    data = None
    if upload is not None and getattr(upload, "filename", ""):
        data = upload.read(MAX_BYTES + 1)
        if len(data) > MAX_BYTES:
            raise ArtError(
                "That file is larger than %d MB." % (MAX_BYTES // (1024 * 1024))
            )
    elif url:
        data = fetch_url(url.strip())

    if not data:
        return None

    stem = store(data)
    if replacing:
        remove(replacing)
    return stem
