#!/usr/bin/env python3
"""Lint the served pages and stylesheets for PS3-hostile constructs.

The baseline browser is the PlayStation 3's NetFront. Its limits are easy to
violate by accident months later, and the failure mode is silent: the page
renders on your laptop and is unusable on the console. This checks the rules
mechanically.

Usage:  ./.venv/bin/python tools/ps3lint.py [base-url] [--only] [more paths...]

Extra paths are linted as well, and with --only nothing else is. A job page
needs a job, and a running job changes what /device and the disc pages serve,
so tests/test_ps3lint.py lints the standing pages first and then starts a job
and lints what it changes.
"""

import os
import re
import sys
from urllib.error import HTTPError
from urllib.parse import urlparse
from urllib.request import urlopen

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: Every template but job.html. The disc pages assume the first disc still
#: exists, which it does on fakerun's seed; the last path is the error page.
PAGES = [
    "/",
    "/?view=list",
    "/add",
    "/reconcile",
    "/device",
    "/disc/1",
    "/disc/1/edit",
    "/disc/1/delete",
    "/no-such-page",
]

#: The only paths allowed to answer with an error. Anywhere else the error page
#: would be linted in place of the template the path was listed for.
ERRORS = {"/no-such-page": 404}

# (pattern, why it matters). Checked against served HTML.
HTML_RULES = [
    (r"<button\b", "<button> is unreliable in NetFront; use <input type=submit>"),
    (
        r"\bon(click|change|submit|load)\s*=",
        "inline JS handlers; actions must be form POSTs",
    ),
    (r"<meta\s+charset=", "bare <meta charset> shorthand; needs the http-equiv form"),
    (
        r"<(section|article|nav|aside|figure|details|summary)\b",
        "HTML5 sectioning element predating NetFront",
    ),
    (
        r"\btype=\"(email|date|number|range|color|search)\"",
        "HTML5 input type; falls back inconsistently",
    ),
    (r"<(canvas|svg|video|audio)\b", "not renderable on the PS3"),
    (r"\.webp\b", "PS3 cannot decode WebP"),
]

# Checked against base.css only. modern.css is exempt: everything in it lives
# inside @supports, which old parsers skip wholesale.
CSS_RULES = [
    (r"display\s*:\s*(flex|grid|contents)", "flexbox/grid"),
    (r"\bvar\s*\(", "custom properties"),
    (r"\d(rem|vh|vw|ch)\b", "CSS3 length unit"),
    (r"\brgba?\s*\(", "rgba()/rgb() function - a parse failure drops the declaration"),
    (r"@media\b", "media query"),
    (r"@supports\b", "@supports (belongs in modern.css)"),
    (r"\bcalc\s*\(", "calc()"),
    (r":(root|not|nth-child|first-of-type)\b", "CSS3 selector"),
]


def check_html(base, pages):
    bad = 0
    for path in pages:
        try:
            response = urlopen(base + path, timeout=5)
        except HTTPError as exc:
            response = exc
        except Exception as exc:
            print("  ?? %-14s could not fetch: %s" % (path, exc))
            bad += 1
            continue
        body = response.read().decode("utf-8", "replace")

        problems = [why for pat, why in HTML_RULES if re.search(pat, body, re.I)]
        if response.status != ERRORS.get(path, 200):
            problems.append("answered %d, so this is not the page" % response.status)
        landed = urlparse(response.geturl()).path
        if landed != urlparse(base + path).path:
            problems.append("redirected to %s, so that is what was linted" % landed)
        if 'http-equiv="Content-Type"' not in body:
            problems.append("missing http-equiv Content-Type meta")
        if problems:
            bad += 1
            print("  FAIL %-14s" % path)
            for p in problems:
                print("       - %s" % p)
        else:
            print("  ok   %-14s" % path)
    return bad


def check_css():
    bad = 0
    path = os.path.join(HERE, "static", "base.css")
    with open(path) as fh:
        # Strip comments so the explanatory header does not trip the rules.
        css = re.sub(r"/\*.*?\*/", "", fh.read(), flags=re.S)

    problems = [why for pat, why in CSS_RULES if re.search(pat, css, re.I)]
    if problems:
        bad += 1
        print("  FAIL base.css")
        for p in problems:
            print("       - %s" % p)
    else:
        print("  ok   base.css (CSS 2.1 only)")

    # Everything modern must be inside @supports, or the PS3 will see it.
    modern = os.path.join(HERE, "static", "modern.css")
    with open(modern) as fh:
        text = re.sub(r"/\*.*?\*/", "", fh.read(), flags=re.S)
    before = text.split("@supports", 1)[0]
    if before.strip():
        bad += 1
        print("  FAIL modern.css has rules outside @supports:")
        print("       %s" % before.strip()[:120])
    else:
        print("  ok   modern.css (all rules behind @supports)")
    return bad


def main():
    args = [arg for arg in sys.argv[1:] if arg != "--only"]
    pages = [] if "--only" in sys.argv else list(PAGES)
    base = (args[0] if args else "http://127.0.0.1:5050").rstrip("/")
    print("PS3 compatibility lint against %s\n" % base)
    print("stylesheets:")
    bad = check_css()
    print("\npages:")
    bad += check_html(base, pages + args[1:])
    print("\n%s" % ("FAILED (%d)" % bad if bad else "all clear"))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
