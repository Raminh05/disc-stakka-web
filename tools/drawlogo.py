#!/usr/bin/env python3
"""Draw the masthead logo and favicon into static/.

The PS3 renders neither SVG nor canvas, so the logo is a PNG. It is drawn here
in code rather than kept as a hand-edited binary, so it can be changed and
redrawn. Run from the project root:  uv run tools/drawlogo.py
"""

import os

from PIL import Image, ImageDraw

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC = os.path.join(HERE, "static")

W, H = 56, 48
SUPER = 16

# The masthead background from base.css. The PNG is flattened onto it: an
# alpha channel is one more thing for NetFront to get wrong.
BAR = (13, 15, 18)

FACE = (214, 220, 229)
EDGE = (128, 138, 154)
RIM = (92, 101, 116)


def draw(scale):
    k = scale * SUPER
    img = Image.new("RGBA", (W * k, H * k), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    def oval(cx, cy, rx, ry, **kw):
        d.ellipse([(cx - rx) * k, (cy - ry) * k, (cx + rx) * k, (cy + ry) * k], **kw)

    cx, rx, ry, thick = W / 2, 26, 8.5, 3.5
    tops = [12 + 7 * i for i in range(4)]
    for cy in reversed(tops):
        oval(cx, cy + thick, rx, ry, fill=EDGE, outline=BAR, width=k)
        d.rectangle([(cx - rx) * k, cy * k, (cx + rx) * k, (cy + thick) * k], fill=EDGE)
        oval(cx, cy, rx, ry, fill=FACE, outline=RIM, width=k)

    cy = tops[0]
    sheen = Image.new("RGBA", img.size, (0, 0, 0, 0))
    s = ImageDraw.Draw(sheen)
    face = [(cx - rx + 2) * k, (cy - ry + 1) * k, (cx + rx - 2) * k, (cy + ry - 1) * k]
    s.pieslice(face, 200, 250, fill=(255, 255, 255, 110))
    s.pieslice(face, 20, 70, fill=(255, 255, 255, 70))
    img.alpha_composite(sheen)

    oval(cx, cy, 8, 8 * ry / rx, fill=(186, 194, 206))
    oval(cx, cy, 3, 3 * ry / rx, fill=BAR)

    return img.resize((W * scale, H * scale), Image.LANCZOS)


def main():
    for scale, name in ((1, "logo.png"), (2, "logo@2x.png")):
        logo = draw(scale)
        flat = Image.new("RGB", logo.size, BAR)
        flat.paste(logo, mask=logo)
        flat.save(os.path.join(STATIC, name), optimize=True)
        print("wrote static/%s" % name)

    icon = draw(4)
    side = max(icon.size)
    square = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    square.paste(icon, ((side - icon.width) // 2, (side - icon.height) // 2))
    square.save(
        os.path.join(STATIC, "favicon.ico"), sizes=[(16, 16), (32, 32), (48, 48)]
    )
    print("wrote static/favicon.ico")


if __name__ == "__main__":
    main()
