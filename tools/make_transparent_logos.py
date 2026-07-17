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

The 1024 is the master: it is knocked out directly, and every smaller size is
derived from it by an alpha-correct ``downscale`` so the edges are properly
anti-aliased (a raw knockout has binary alpha — fine at 1024, a chunky cutout
at 16). See ``downscale`` for why not a plain ``resize``.

Outputs (committed assets):
  - assets/icons/garelochsoft_logo.png   (overwritten, transparent)
  - assets/icons/mfl_icon_{16..1024}.png (overwritten, transparent — ADR-174)
  - assets/icons/mfl.icns                (rebuilt — ADR-174, needs macOS)
  - assets/icons/mfl.ico                 (rebuilt — ADR-174 amendment)
  - assets/icons/mfl_mark.png            (transparent, the 512 under a name)

Idempotent: ``knockout`` flood-fills over the *RGB* channels, which survive an
alpha-zeroed pass unchanged, so re-running finds the same region; the downscales
are deterministic. Verified byte-identical on a second run.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
ICONS = ROOT / "assets" / "icons"

# The committed PNG size set, which `resources.app_icon()` loads into a
# multi-resolution QIcon (the dock icon when running from source). 1024 is the
# master — knocked out directly; the rest are derived from it.
_MASTER = 1024
_ICON_SIZES = (16, 32, 64, 128, 256, 512, 1024)

# The Windows .ico frame set (ADR-174 amendment). Note **48** — Windows uses it
# (Large Icons, alt-tab) and the PNG set has no 48, so it is derived for the
# .ico only and never written to disk as a PNG.
_ICO_SIZES = (16, 32, 48, 64, 128, 256)

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


def downscale(im: Image.Image, size: int) -> Image.Image:
    """Alpha-correct downscale: premultiply → resize → un-premultiply.

    **Why not just `im.resize(...)`.** `knockout` zeroes a pixel's *alpha* and
    leaves its RGB alone, so every transparent pixel still carries the flat
    wash in its colour channels. A plain resize averages those invisible
    wash-coloured pixels into their visible neighbours and paints a light
    fringe back around the edge — the very thing being removed. Premultiplying
    first weights each pixel's colour by its coverage, so a fully transparent
    pixel contributes nothing.

    **And why downscale at all**, when ADR-174 originally knocked out each size
    independently: `knockout` produces a **binary** alpha (0 or 255), so a
    per-size knockout has *no anti-aliasing* on the outer edge. At 1024 that is
    a sub-pixel irrelevance; at 16 it is a visibly chunky, stair-stepped cutout
    with light blocks around the rim. Downscaling the master resolves the hard
    edge into real coverage values — 108 partial-alpha pixels at 16px, against
    zero for a direct knockout — and, done premultiplied, with no fringe.

    ADR-174 measured a *naive* downscale, found the fringe, and concluded
    "knock out each size independently". Right conclusion for the comparison it
    ran; the wrong comparison. See the amendment.
    """
    r, g, b, a = im.split()
    pm = Image.merge("RGBA", (
        ImageChops.multiply(r, a),
        ImageChops.multiply(g, a),
        ImageChops.multiply(b, a),
        a,
    )).resize((size, size), Image.LANCZOS)

    out = Image.new("RGBA", (size, size))
    src, dst = pm.load(), out.load()
    for y in range(size):
        for x in range(size):
            pr, pg, pb, pa = src[x, y]
            if pa == 0:
                # Colourless *and* invisible: leave nothing for a later naive
                # resize (or a careless editor) to drag back into the edges.
                dst[x, y] = (0, 0, 0, 0)
            else:
                dst[x, y] = (
                    min(255, pr * 255 // pa),
                    min(255, pg * 255 // pa),
                    min(255, pb * 255 // pa),
                    pa,
                )
    return out


def build_ico(dest: Path, master: Image.Image) -> None:
    """Rebuild the Windows ``.ico`` from the transparent master (ADR-174
    amendment). Frames: 16/32/48/64/128/256, 32-bit PNG — matching what the
    file already contained.

    Every frame is **pre-built and handed over via ``append_images``**. Pillow's
    ICO writer uses a provided image verbatim when one matches the requested
    size, and only falls back to its own ``thumbnail()`` — a naive, fringing
    resize — when none does. Supplying all six means its resampler never runs.
    The largest frame must be the one ``save`` is called on: the writer skips
    any size larger than *that* image.
    """
    frames = {size: downscale(master, size) for size in _ICO_SIZES}
    biggest = max(_ICO_SIZES)
    rest = [frames[s] for s in sorted(_ICO_SIZES) if s != biggest]
    frames[biggest].save(
        dest, format="ICO",
        sizes=[(s, s) for s in sorted(_ICO_SIZES)],
        append_images=rest,
    )


def build_icns(dest: Path) -> bool:
    """Rebuild the macOS ``.icns`` from the (already transparent) PNG set.

    Uses Apple's ``iconutil`` rather than writing the container by hand: it is
    the format's own compiler, it is on every Mac, and the packaged bundle's
    icon is not the place to find out that a hand-rolled writer got a header
    field wrong.

    Reads the committed PNG set, so it inherits whatever ``main`` wrote —
    ``downscale``'s anti-aliased edges included.

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

    # The dock/taskbar set (ADR-174). Knock out the 1024 master, then derive
    # every smaller size from it so they all carry real anti-aliased edges.
    # In place, and idempotent: re-knocking an already-transparent master is a
    # no-op (the flood-fill reads RGB, which an alpha-zeroing pass leaves
    # alone) and the downscales are deterministic.
    master = knockout(ICONS / f"mfl_icon_{_MASTER}.png")
    master.save(ICONS / f"mfl_icon_{_MASTER}.png")
    for size in _ICON_SIZES:
        if size == _MASTER:
            continue
        downscale(master, size).save(ICONS / f"mfl_icon_{size}.png")
    print(f"Wrote transparent mfl_icon_*.png ({len(_ICON_SIZES)} sizes)")

    # The in-UI mark is the 512 under another name (ADR-117). Kept as a
    # separate asset on purpose — see `resources.brand_mark`.
    downscale(master, 512).save(ICONS / "mfl_mark.png")

    if build_icns(ICONS / "mfl.icns"):
        print("Rebuilt mfl.icns from the transparent set")
    build_ico(ICONS / "mfl.ico", master)
    print("Rebuilt mfl.ico from the transparent set")
    print("Wrote transparent garelochsoft_logo.png + mfl_mark.png")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
