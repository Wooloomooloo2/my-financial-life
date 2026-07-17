"""Knock the flat light background out of the brand logo art → transparent PNGs
(ADR-117, extended by ADR-174).

The supplied logo art (``garelochsoft_logo.png``, ``mfl_icon_*.png``) ships on a
flat light-blue-grey background. That's invisible on the app's light surfaces but
shows as an ugly light box on the dark-theme sidebar / status bar. This tool
flood-fills the background from the image edges (so internal light details — the
gold coin, light-teal facets — are never touched) and writes transparent copies
used by the in-app brand chrome.

**ADR-174: the dock/taskbar ``mfl_icon_*`` set is now knocked out too.** ADR-117
deliberately left it alone — "a separate concern from the in-UI mark" — which
was true of the *reason* (a dark sidebar) and wrong about the *art*: macOS draws
the dock icon on the desktop, not on a surface we control, so the flat
background reads as a white box behind the hexagon there just as it did in the
sidebar. The whole icon set is knocked out, and ``mfl.icns`` is rebuilt from it.

Run from the repo root with a Pillow-capable interpreter (the ``.icns`` step
needs macOS's ``iconutil`` and is skipped elsewhere with a warning):

    python3 tools/make_transparent_logos.py

Outputs (committed assets):
  - assets/icons/garelochsoft_logo.png   (overwritten, transparent)
  - assets/icons/mfl_icon_{16..1024}.png (overwritten, transparent — ADR-174)
  - assets/icons/mfl.icns                (rebuilt from the above — ADR-174)
  - assets/icons/mfl_mark.png            (transparent, from mfl_icon_512)

Idempotent: ``knockout`` flood-fills over the *RGB* channels, which survive an
alpha-zeroed pass unchanged, so re-running finds the same region and is a no-op.

``mfl.ico`` (Windows) is **not** rebuilt here — see ADR-174.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
ICONS = ROOT / "assets" / "icons"

# The committed PNG size set, which `resources.app_icon()` loads into a
# multi-resolution QIcon (the dock icon when running from source).
_ICON_SIZES = (16, 32, 64, 128, 256, 512, 1024)

# macOS .iconset slot -> the source size that fills it. `iconutil` requires
# these exact names; an @2x slot is simply the next size up.
_ICNS_SLOTS = {
    "icon_16x16.png": 16,
    "icon_16x16@2x.png": 32,
    "icon_32x32.png": 32,
    "icon_32x32@2x.png": 64,
    "icon_128x128.png": 128,
    "icon_128x128@2x.png": 256,
    "icon_256x256.png": 256,
    "icon_256x256@2x.png": 512,
    "icon_512x512.png": 512,
    "icon_512x512@2x.png": 1024,
}

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


def build_icns(dest: Path) -> bool:
    """Rebuild the macOS ``.icns`` from the (already transparent) PNG set.

    Uses Apple's ``iconutil`` rather than writing the container by hand: it is
    the format's own compiler, it is on every Mac, and the packaged bundle's
    icon is not the place to find out that a hand-rolled writer got a header
    field wrong.

    Each size is knocked out **independently** rather than downscaled from a
    transparent master. That reads backwards — a clean master ought to give
    cleaner children — but Pillow's ``resize`` does not premultiply alpha, so
    downscaling bleeds the background colour still sitting in the RGB of
    transparent pixels back into the edges. Measured: downscaling left light
    fringe pixels at 32px where a direct knockout left none.

    Returns False (with a warning) off macOS, where ``iconutil`` doesn't exist.
    """
    if not shutil.which("iconutil"):
        print(
            "  ! iconutil not found (not macOS?) — skipped mfl.icns.\n"
            "    The PNG set is still transparent; re-run this on a Mac to "
            "rebuild the bundle icon.",
            file=sys.stderr,
        )
        return False
    with tempfile.TemporaryDirectory() as tmp:
        iconset = Path(tmp) / "mfl.iconset"
        iconset.mkdir()
        for slot, size in _ICNS_SLOTS.items():
            src = ICONS / f"mfl_icon_{size}.png"
            im = Image.open(src).convert("RGBA")
            # The slot's pixel size is its name's base × its @2x factor — which
            # is exactly the source size, but assert it rather than trust it:
            # a mismatched slot silently ships a blurry icon.
            want = int(slot.split("x")[0].removeprefix("icon_"))
            want *= 2 if "@2x" in slot else 1
            if im.size != (want, want):
                raise SystemExit(
                    f"{src.name} is {im.size}, but {slot} needs {want}x{want}"
                )
            im.save(iconset / slot)
        subprocess.run(
            ["iconutil", "-c", "icns", str(iconset), "-o", str(dest)],
            check=True,
        )
    return True


def main() -> int:
    knockout(ICONS / "garelochsoft_logo.png").save(ICONS / "garelochsoft_logo.png")

    # The dock/taskbar set (ADR-174). In place, and idempotent.
    for size in _ICON_SIZES:
        p = ICONS / f"mfl_icon_{size}.png"
        knockout(p).save(p)
    print(f"Knocked out mfl_icon_*.png ({len(_ICON_SIZES)} sizes)")

    # The in-UI mark rides off the (now transparent) 512.
    knockout(ICONS / "mfl_icon_512.png").save(ICONS / "mfl_mark.png")

    if build_icns(ICONS / "mfl.icns"):
        print("Rebuilt mfl.icns from the transparent set")
    print("Wrote transparent garelochsoft_logo.png + mfl_mark.png")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
