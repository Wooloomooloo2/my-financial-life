"""The dock/taskbar icon is a hexagon, not a hexagon in a white box (ADR-174).

Owner-reported: the macOS dock icon had a white background behind the hexagon.
It was baked in — every pixel of ``mfl_icon_*.png`` was alpha 255, with an
opaque ``#E9EDEF`` wash out to the corners. ADR-117 had built the knockout tool
and *deliberately* pointed it away from this set ("the packaged app icon, a
separate concern from the in-UI mark"); macOS draws that icon on the user's
desktop, which is not a surface we control, so the wash showed there exactly as
it had on the dark sidebar.

Two surfaces feed the dock and both are guarded here:

- **the packaged .app** → ``assets/icons/mfl.icns`` (via ``packaging/mfl.spec``)
- **running from source** → ``resources.app_icon()``, a QIcon over the PNG set

Qt, not Pillow: Pillow is a *tool* dependency (``tools/make_transparent_logos``
needs it) and is not installed in the app's venv, so a test that imported it
would simply not run. Qt reads png/icns/ico natively.

    QT_QPA_PLATFORM=offscreen .venv/bin/python tests/test_app_icon_transparent.py
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from PySide6.QtCore import QSize
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication

_app = QApplication.instance() or QApplication([])

from mfl_desktop.resources import app_icon, app_pixmap, asset_path, brand_mark

_SIZES = (16, 32, 64, 128, 256, 512, 1024)

# The flat wash the art shipped on. Anything near it, opaque, at an edge, means
# the box is back.
_WASH = (233, 237, 239)


def _corners(img: QImage):
    w, h = img.width(), img.height()
    return [(0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1)]


def _is_wash(c, tol: int = 14) -> bool:
    return (
        abs(c.red() - _WASH[0]) <= tol
        and abs(c.green() - _WASH[1]) <= tol
        and abs(c.blue() - _WASH[2]) <= tol
    )


def test_every_icon_png_has_transparent_corners() -> None:
    for size in _SIZES:
        p = asset_path("icons", f"mfl_icon_{size}.png")
        assert p.exists(), p
        img = QImage(str(p))
        assert not img.isNull(), p
        assert img.hasAlphaChannel(), f"{p.name} has no alpha channel at all"
        for x, y in _corners(img):
            c = img.pixelColor(x, y)
            assert c.alpha() == 0, (
                f"{p.name} corner ({x},{y}) is opaque {c.name()} — the white "
                f"box is back"
            )


def test_no_icon_png_is_fully_opaque() -> None:
    """The sharpest form of the bug: alpha present but 255 everywhere, which is
    what the set looked like before ADR-174 (`hasAlphaChannel()` was already
    True — it was the *values* that were wrong, so the channel's existence
    proves nothing on its own)."""
    for size in _SIZES:
        img = QImage(str(asset_path("icons", f"mfl_icon_{size}.png")))
        w, h = img.width(), img.height()
        step = max(1, w // 48)
        alphas = {
            img.pixelColor(x, y).alpha()
            for y in range(0, h, step) for x in range(0, w, step)
        }
        assert 0 in alphas, f"mfl_icon_{size}.png has no transparent pixels"
        assert 255 in alphas, f"mfl_icon_{size}.png has no opaque pixels"


def test_the_hexagon_itself_survived_the_knockout() -> None:
    """A flood-fill that ate the artwork would also pass the corner tests. The
    centre must still be the badge's dark teal, and a healthy share of the
    canvas must still be opaque."""
    img = QImage(str(asset_path("icons", "mfl_icon_256.png")))
    w, h = img.width(), img.height()
    centre = img.pixelColor(w // 2, h // 2)
    assert centre.alpha() == 255, "the middle of the badge went transparent"

    step = 2
    total = opaque = 0
    for y in range(0, h, step):
        for x in range(0, w, step):
            total += 1
            if img.pixelColor(x, y).alpha() > 0:
                opaque += 1
    frac = opaque / total
    # A hexagon inscribed in the art's ~78% box covers roughly half the canvas.
    assert 0.45 < frac < 0.75, (
        f"{frac:.0%} of the canvas is opaque — the knockout ate the art, or "
        f"barely ran"
    )


def test_no_opaque_wash_pixels_remain_at_the_edges() -> None:
    """The wash could survive as an opaque fringe just inside the corners even
    when the corners themselves are clear."""
    img = QImage(str(asset_path("icons", "mfl_icon_1024.png")))
    w, h = img.width(), img.height()
    band = []
    for x in range(0, w, 4):
        band += [(x, 0), (x, 3), (x, h - 1), (x, h - 4)]
    for y in range(0, h, 4):
        band += [(0, y), (3, y), (w - 1, y), (w - 4, y)]
    for x, y in band:
        c = img.pixelColor(x, y)
        assert not (c.alpha() > 24 and _is_wash(c)), (
            f"opaque wash {c.name()} still at ({x},{y})"
        )


def test_app_icon_is_transparent_at_every_size() -> None:
    """The dock icon when running from source: `QApplication.setWindowIcon`
    takes this QIcon, so it is the real artefact, not the PNGs on disk."""
    icon = app_icon()
    assert not icon.isNull()
    for size in (16, 32, 64, 128, 256, 512, 1024):
        img = icon.pixmap(QSize(size, size)).toImage()
        assert not img.isNull(), f"no pixmap at {size}"
        c = img.pixelColor(0, 0)
        assert c.alpha() == 0, (
            f"app_icon() at {size}px has an opaque corner {c.name()}"
        )


def test_the_in_ui_mark_and_the_app_pixmap_agree() -> None:
    """ADR-117 introduced `brand_mark` *because* `app_pixmap` carried the box.
    That reason is gone, so the two must now agree — if this ever fails, one of
    them has regrown a background and the other hasn't."""
    for pm in (brand_mark(64), app_pixmap(64)):
        assert not pm.isNull()
        assert pm.toImage().pixelColor(0, 0).alpha() == 0


def test_the_ico_frames_are_transparent() -> None:
    """The Windows .exe + installer icon (``packaging/mfl.spec``,
    ``installer.iss``). ADR-174 shipped without this and left the Windows
    packaged icon boxed while its taskbar icon — from the shared PNG set — was
    already clean; the amendment closed it.

    Qt's ico reader returns the largest frame, so the per-frame check goes
    through Pillow when it is available (the tool's own dependency) and falls
    back to the single-frame check when it isn't.
    """
    ico = asset_path("icons", "mfl.ico")
    assert ico.exists()
    img = QImage(str(ico))
    assert not img.isNull(), "Qt could not read mfl.ico"
    assert img.pixelColor(0, 0).alpha() == 0, "mfl.ico has an opaque corner"

    try:
        from PIL import Image as _PILImage
    except ModuleNotFoundError:
        return
    im = _PILImage.open(ico)
    sizes = sorted(im.info["sizes"])
    # 48 is Windows-only (Large Icons, alt-tab) and has no PNG in the set —
    # it exists solely because the .ico asks for it, so it is the one most
    # likely to be quietly dropped by a future rebuild.
    assert (48, 48) in sizes, f"the .ico lost its 48px frame: {sizes}"
    for s in sizes:
        im.size = s
        frame = im.convert("RGBA")
        assert frame.getpixel((0, 0))[3] == 0, (
            f"mfl.ico {s[0]}px frame has an opaque corner"
        )


def test_small_icons_are_anti_aliased() -> None:
    """`knockout` alone yields **binary** alpha — a hard, stair-stepped cutout
    with light blocks around the rim, which is what ADR-174 first shipped. The
    sizes are derived from the master by an alpha-correct downscale precisely
    to get real coverage values at the edge, so partial alpha is the evidence
    that the downscale ran and wasn't replaced by a per-size knockout."""
    for size in (16, 32, 64):
        img = QImage(str(asset_path("icons", f"mfl_icon_{size}.png")))
        w, h = img.width(), img.height()
        partial = sum(
            1
            for y in range(h) for x in range(w)
            if 0 < img.pixelColor(x, y).alpha() < 255
        )
        assert partial > 0, (
            f"mfl_icon_{size}.png has no partial alpha — the edge is a hard "
            f"cutout, not anti-aliased"
        )


def test_the_icns_slots_are_transparent() -> None:
    """The dock icon of the *packaged* .app. Read via Qt's icns plugin, which
    gives the largest representation; `iconutil` is used to check every slot
    when it's available (macOS)."""
    icns = asset_path("icons", "mfl.icns")
    assert icns.exists()
    img = QImage(str(icns))
    assert not img.isNull(), "Qt could not read mfl.icns"
    assert img.pixelColor(0, 0).alpha() == 0, "mfl.icns has an opaque corner"

    if not shutil.which("iconutil"):
        return
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "mfl.iconset"
        subprocess.run(
            ["iconutil", "-c", "iconset", str(icns), "-o", str(out)],
            check=True,
        )
        slots = sorted(out.glob("*.png"))
        assert len(slots) == 10, f"expected 10 icns slots, got {len(slots)}"
        for p in slots:
            si = QImage(str(p))
            assert si.pixelColor(0, 0).alpha() == 0, (
                f"icns slot {p.name} has an opaque corner"
            )


if __name__ == "__main__":
    import traceback
    failures = 0
    for name, fn in sorted(list(globals().items())):
        if not name.startswith("test_") or not callable(fn):
            continue
        try:
            fn()
            print(f"ok   {name}")
        except Exception:
            failures += 1
            print(f"FAIL {name}")
            traceback.print_exc()
    print("\n" + ("all passed" if not failures else f"{failures} failed"))
    sys.exit(1 if failures else 0)
