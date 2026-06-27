"""Knock the flat light background out of the brand logo art → transparent PNGs
(ADR-117).

The supplied logo art (``garelochsoft_logo.png``, ``mfl_icon_*.png``) ships on a
flat light-blue-grey background. That's invisible on the app's light surfaces but
shows as an ugly light box on the dark-theme sidebar / status bar. This tool
flood-fills the background from the image edges (so internal light details — the
gold coin, light-teal facets — are never touched) and writes transparent copies
used by the in-app brand chrome.

Run from the repo root with a Pillow-capable interpreter:

    python3 tools/make_transparent_logos.py

Outputs (committed assets):
  - assets/icons/garelochsoft_logo.png   (overwritten, transparent)
  - assets/icons/mfl_mark.png            (new, transparent, from mfl_icon_512)

The dock/taskbar ``mfl_icon_*`` set is deliberately left untouched — that's the
packaged app icon, a separate concern from the in-UI mark.
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
ICONS = ROOT / "assets" / "icons"

# Flood-fill colour tolerance. High enough to eat the anti-aliased ring between
# the logo and its flat background (no light halo), low enough to never bite into
# the teal/gold artwork.
_THRESH = 46
_SENTINEL = (255, 0, 255)   # an impossible colour to mark filled background


def knockout(src: Path) -> Image.Image:
    """Return ``src`` with its flat edge background made fully transparent."""
    im = Image.open(src).convert("RGBA")
    w, h = im.size
    rgb = im.convert("RGB")
    for corner in ((0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1)):
        ImageDraw.floodfill(rgb, corner, _SENTINEL, thresh=_THRESH)
    px, rp = im.load(), rgb.load()
    for y in range(h):
        for x in range(w):
            if rp[x, y] == _SENTINEL:
                r, g, b, _ = px[x, y]
                px[x, y] = (r, g, b, 0)
    return im


def main() -> int:
    knockout(ICONS / "garelochsoft_logo.png").save(ICONS / "garelochsoft_logo.png")
    knockout(ICONS / "mfl_icon_512.png").save(ICONS / "mfl_mark.png")
    print("Wrote transparent garelochsoft_logo.png + mfl_mark.png")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
