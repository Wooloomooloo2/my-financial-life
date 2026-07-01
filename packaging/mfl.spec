# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for My Financial Life (ADR-104).

One spec, both desktop targets (PyInstaller can't cross-compile, so it is
*run* on each OS — macOS produces a .app, Windows a folder+exe). Build via
the per-OS scripts, which stamp build metadata first:

    python packaging/stamp_build_info.py
    pyinstaller --noconfirm --clean packaging/mfl.spec

Critically bundles the two runtime data sets the frozen app needs:
  - mfl_desktop/migrations/*.sql — without these the app can't bootstrap a
    DB (schema.py resolves them via sys._MEIPASS when frozen; ADR-104).
  - assets/icons/*.png — the in-app window/dock icon (resources.py is
    _MEIPASS-aware).
plus ofxtools' small config data files.
"""
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files

ROOT = Path(SPECPATH).resolve().parent  # SPECPATH = packaging/; ROOT = repo root

sys.path.insert(0, str(ROOT))
from mfl_desktop.version import __version__, APP_NAME  # noqa: E402

# ── data files bundled into the app ─────────────────────────────────────────
datas = [
    (str(ROOT / "mfl_desktop" / "migrations"), "mfl_desktop/migrations"),
    (str(ROOT / "assets" / "icons"), "assets/icons"),
]
datas += collect_data_files("ofxtools")
# certifi's cacert.pem — the TLS trust store the frozen app points OpenSSL at
# via SSL_CERT_FILE (ADR-126). Collect it explicitly so it is guaranteed to ship
# (and stays shipped) rather than relying on transitive inclusion.
datas += collect_data_files("certifi")

# ── per-OS bundle icon ──────────────────────────────────────────────────────
_icns = str(ROOT / "assets" / "icons" / "mfl.icns")
_ico = str(ROOT / "assets" / "icons" / "mfl.ico")
if sys.platform == "darwin":
    bundle_icon = _icns
elif sys.platform.startswith("win"):
    bundle_icon = _ico
else:
    bundle_icon = None

block_cipher = None

a = Analysis(
    [str(ROOT / "packaging" / "mfl_main.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=["ofxtools", "certifi"],  # certifi: TLS trust store (ADR-126)
    hookspath=[],
    runtime_hooks=[],
    # Trim the unused legacy v0.1 web stack + heavy optional libs that PyInstaller
    # might otherwise drag in — the desktop app uses none of them (ADR-099).
    excludes=[
        "tkinter", "fastapi", "uvicorn", "starlette", "jinja2",
        "pyoxigraph", "rdflib", "pydantic", "PIL", "numpy", "pytest",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=APP_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,            # windowed app — no terminal
    disable_windowed_traceback=False,
    target_arch=None,         # build native; macOS universal2 handled by the CI runner
    codesign_identity=None,   # signing is done in the build scripts, post-build
    entitlements_file=None,   # macOS signing is script-side: Developer-ID in
                              # build_macos.sh, sandbox entitlements in
                              # build_mas.sh (ADR-125).
    icon=bundle_icon,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name=APP_NAME,
)

if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name=f"{APP_NAME}.app",
        icon=_icns,
        bundle_identifier="life.myfinancial.app",
        version=__version__,
        info_plist={
            "CFBundleName": APP_NAME,
            "CFBundleDisplayName": APP_NAME,
            "CFBundleShortVersionString": __version__,
            "CFBundleVersion": __version__,
            "NSHighResolutionCapable": True,
            # The app manages its own light/dark theme; allow the system
            # appearance so native chrome (menus) matches.
            "NSRequiresAquaSystemAppearance": False,
            "LSApplicationCategoryType": "public.app-category.finance",
            "LSMinimumSystemVersion": "11.0",
            "NSHumanReadableCopyright": "© 2026 My Financial Life",
        },
    )
