#!/usr/bin/env python3
"""Lint the served pages and stylesheets for PS3-hostile constructs.

The baseline browser is the PlayStation 3's NetFront. Its limits are easy to
violate by accident months later, and the failure mode is silent: the page
renders on your laptop and is unusable on the console. This checks the rules
mechanically.

Usage:  ./.venv/bin/python tools/ps3lint.py [base-url]
"""

import os
import re
import sys
from urllib.request import urlopen

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

PAGES = ["/", "/add", "/reconcile", "/device"]

# (pattern, why it matters). Checked against served HTML.
HTML_RULES = [
    (r"<button\b", "<button> is unreliable in NetFront; use <input type=submit>"),
    (r"\bon(click|change|submit|load)\s*=", "inline JS handlers; actions must be form POSTs"),
    (r"<meta\s+charset=", "bare <meta charset> shorthand; needs the http-equiv form"),
    (r"<(section|article|nav|aside|figure|details|summary)\b",
     "HTML5 sectioning element predating NetFront"),
    (r"\btype=\"(email|date|number|range|color|search)\"",
     "HTML5 input type; falls back inconsistently"),
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


def check_html(base):
    bad = 0
    for path in PAGES:
        try:
            body = urlopen(base + path, timeout=5).read().decode("utf-8", "replace")
        except Exception as exc:
            print("  ?? %-14s could not fetch: %s" % (path, exc))
            bad += 1
            continue

        problems = [why for pat, why in HTML_RULES
                    if re.search(pat, body, re.I)]
        if "http-equiv=\"Content-Type\"" not in body:
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
    base = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:5050").rstrip("/")
    print("PS3 compatibility lint against %s\n" % base)
    print("stylesheets:")
    bad = check_css()
    print("\npages:")
    bad += check_html(base)
    print("\n%s" % ("FAILED (%d)" % bad if bad else "all clear"))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
