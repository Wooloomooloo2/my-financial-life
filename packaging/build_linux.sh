#!/usr/bin/env bash
# Build the Linux app (ADR-192): PyInstaller folder -> .deb + portable tar.gz
# (+ an AppImage when appimagetool can be obtained).
#
# Usage:
#   ./packaging/build_linux.sh                 # build everything it can
#   ./packaging/build_linux.sh --deb           # only the .deb
#   ./packaging/build_linux.sh --tarball       # only the portable tar.gz
#   ./packaging/build_linux.sh --appimage      # only the AppImage
#   ./packaging/build_linux.sh --no-appimage   # everything except the AppImage
#
# The Linux counterpart to build_macos.sh / build_windows.ps1, using the SAME
# packaging/mfl.spec. It needs no signing accounts — Linux desktops have no
# Gatekeeper/SmartScreen equivalent — so it produces installable artifacts out
# of the box. All three artifacts are the one PyInstaller folder in different
# wrappers; nothing about the app differs between them.
#
#   dist/my-financial-life_<version>_<arch>.deb
#       Debian/Ubuntu install: the app in /opt, a launcher symlink in /usr/bin,
#       the .desktop entry + hicolor icons so it appears in the applications
#       menu. `sudo apt install ./…deb` then click the icon.
#   dist/my-financial-life-<version>-<arch>.tar.gz
#       Every other distro / no root: extract and run the binary, or run the
#       bundled install.sh for a per-user menu entry (~/.local).
#   dist/My_Financial_Life-<version>-<arch>.AppImage
#       Single portable file, chmod +x and double-click.
#
# Environment:
#   PYTHON               python to build with (default: python3)
#   MFL_DEB_MAINTAINER   Maintainer: field for the .deb
#                        (default: Garelochsoft <support@garelochsoft.com>)
#   APPIMAGETOOL         path to an appimagetool binary (default: look on PATH,
#                        else download to build/ — needs network)
set -euo pipefail
cd "$(dirname "$0")/.."

WANT_DEB=0 WANT_TAR=0 WANT_APPIMAGE=0 EXPLICIT=0
for arg in "$@"; do
  case "$arg" in
    --deb)         WANT_DEB=1; EXPLICIT=1 ;;
    --tarball)     WANT_TAR=1; EXPLICIT=1 ;;
    --appimage)    WANT_APPIMAGE=1; EXPLICIT=1 ;;
    --no-appimage) WANT_DEB=1; WANT_TAR=1; EXPLICIT=1 ;;
    *) echo "Unknown argument: $arg" >&2; exit 2 ;;
  esac
done
if [[ "$EXPLICIT" -eq 0 ]]; then WANT_DEB=1; WANT_TAR=1; WANT_APPIMAGE=1; fi

PYTHON="${PYTHON:-python3}"
SLUG="my-financial-life"                       # binary + package + icon name
APP_NAME="My Financial Life"                   # the name a human sees
DIST="dist"
BUILT="$DIST/$SLUG"                            # what packaging/mfl.spec produces
STAGE="build/linux"                            # scratch space for the wrappers

VERSION="$("$PYTHON" -c 'import sys; sys.path.insert(0, "."); from mfl_desktop.version import __version__; print(__version__)')"
ARCH_DEB="$(dpkg --print-architecture 2>/dev/null || echo amd64)"
ARCH_RAW="$(uname -m)"
echo "==> Building $APP_NAME $VERSION for Linux ($ARCH_RAW)"

# ── 1. freeze ────────────────────────────────────────────────────────────────
echo "==> Stamping build metadata"
"$PYTHON" packaging/stamp_build_info.py

echo "==> Running PyInstaller"
"$PYTHON" -m PyInstaller --noconfirm --clean packaging/mfl.spec

if [[ ! -x "$BUILT/$SLUG" ]]; then
  echo "ERROR: $BUILT/$SLUG was not produced" >&2
  exit 1
fi

rm -rf "$STAGE"
mkdir -p "$STAGE"

# The icon sizes assets/icons/ carries, for the hicolor theme. A desktop
# environment finds the launcher icon by the .desktop file's Icon= name, so all
# three wrappers install these under the $SLUG name.
ICON_SIZES=(16 32 64 128 256 512)

install_icons() {   # install_icons <share-dir>
  local share="$1" size src
  for size in "${ICON_SIZES[@]}"; do
    src="assets/icons/mfl_icon_${size}.png"
    [[ -f "$src" ]] || continue
    install -Dm644 "$src" "$share/icons/hicolor/${size}x${size}/apps/$SLUG.png"
  done
}

# ── 2. .deb ──────────────────────────────────────────────────────────────────
# Dependencies: PyInstaller bundles Python + Qt, but the bundled Qt libraries
# still link against the system's GL/X11/glib stack. dpkg-shlibdeps reads the
# actual ELF NEEDED entries and maps them to packages, which beats a hand-kept
# list going stale; FALLBACK_DEPENDS is used only when dpkg-dev is absent.
# NOTE: the versions it emits come from THIS machine, so a .deb built on a newer
# Ubuntu won't install on an older one — build it on the oldest release you mean
# to support (CI uses ubuntu-22.04).
FALLBACK_DEPENDS="libc6, libglib2.0-0, libgl1, libegl1, libx11-6, libxcb1, libxkbcommon0, libxkbcommon-x11-0, libfontconfig1, libfreetype6, libdbus-1-3, zlib1g"

compute_depends() {   # compute_depends <app-dir> -> a Depends: value
  local appdir deps
  # Absolute: the dpkg-shlibdeps call below runs from $STAGE, where a path
  # relative to the repo root would no longer resolve.
  appdir="$(realpath "$1")"
  command -v dpkg-shlibdeps >/dev/null 2>&1 || { echo "$FALLBACK_DEPENDS"; return; }

  # dpkg-shlibdeps wants a `debian/control` in the working directory, and finds
  # the package tree (to expand the bundle's $ORIGIN RPATHs) by walking up from
  # each binary to a DEBIAN/ directory — so it runs from $STAGE, one level above
  # the staged package root.
  mkdir -p "$STAGE/debian"
  printf 'Source: %s\n\nPackage: %s\nArchitecture: %s\n' "$SLUG" "$SLUG" "$ARCH_DEB" \
    > "$STAGE/debian/control"

  local files=() keep=() f
  mapfile -d '' files < <(
    find "$appdir" -type f \( -name '*.so' -o -name '*.so.*' -o -name "$SLUG" \) -print0
  )
  for f in "${files[@]}"; do
    # Skip anything with an unsatisfiable link (PySide6 ships a Qt TIFF plugin
    # built against libtiff.so.5, gone since Ubuntu 23.04). Such a plugin can't
    # load at runtime whatever we depend on, and dpkg-shlibdeps treats the
    # missing library as a fatal error rather than a warning.
    ldd "$f" 2>/dev/null | grep -q "not found" || keep+=("$f")
  done
  [[ ${#keep[@]} -gt 0 ]] || { echo "$FALLBACK_DEPENDS"; return; }

  # -O prints to stdout instead of debian/substvars; --ignore-missing-info
  # tolerates the bundled Qt/Python libraries, which belong to no package; -l
  # points at those same bundled directories so they resolve.
  deps="$(
    ( cd "$STAGE" && dpkg-shlibdeps -O --ignore-missing-info \
        -l"$appdir/_internal" -l"$appdir/_internal/PySide6/Qt/lib" \
        "${keep[@]}" 2>/dev/null ) | sed -n 's/^shlibs:Depends=//p' | head -1
  )" || true
  rm -rf "$STAGE/debian"

  # Some shlibs templates carry substvars (`libkrb5support0 (= ${binary:Version})`)
  # that only dpkg-gencontrol expands. Writing DEBIAN/control by hand, they would
  # ship literally and make the package uninstallable — so keep the package name
  # and drop the unexpandable constraint.
  deps="$("$PYTHON" - "$deps" <<'PY'
import re, sys
clauses = [c.strip() for c in sys.argv[1].split(",") if c.strip()]
out = [re.sub(r"\s*\(.*\)$", "", c) if "${" in c else c for c in clauses]
print(", ".join(out))
PY
)"
  if [[ -n "$deps" ]]; then echo "$deps"; else echo "$FALLBACK_DEPENDS"; fi
}

build_deb() {
  echo "==> Building .deb"
  command -v dpkg-deb >/dev/null 2>&1 || {
    echo "    dpkg-deb not found — skipping the .deb (install dpkg-dev)."; return; }

  local root="$STAGE/deb"
  rm -rf "$root"
  mkdir -p "$root/DEBIAN" "$root/opt" "$root/usr/bin" \
           "$root/usr/share/applications" "$root/usr/share/doc/$SLUG"

  # The app itself lives in /opt (a self-contained third-party bundle, FHS 3.0
  # §3.13) — NOT scattered through /usr, which is for distro-built binaries.
  cp -a "$BUILT" "$root/opt/$SLUG"
  # A launcher on $PATH. PyInstaller resolves its _internal/ dir from
  # /proc/self/exe, which follows the symlink, so this needs no wrapper script.
  ln -sf "/opt/$SLUG/$SLUG" "$root/usr/bin/$SLUG"

  install -Dm644 "packaging/linux/$SLUG.desktop" \
    "$root/usr/share/applications/$SLUG.desktop"
  install_icons "$root/usr/share"
  install -Dm644 packaging/linux/copyright "$root/usr/share/doc/$SLUG/copyright"

  local depends installed_size maintainer
  depends="$(compute_depends "$root/opt/$SLUG")"
  installed_size="$(du -sk "$root" | cut -f1)"
  maintainer="${MFL_DEB_MAINTAINER:-Garelochsoft <support@garelochsoft.com>}"
  echo "    Depends: $depends"

  cat > "$root/DEBIAN/control" <<EOF
Package: $SLUG
Version: $VERSION
Section: utils
Priority: optional
Architecture: $ARCH_DEB
Maintainer: $maintainer
Installed-Size: $installed_size
Depends: $depends
Homepage: https://garelochsoft.com
Description: $APP_NAME — private, local personal finance
 Track transactions, balances, budgets, investments and net worth in a
 native desktop application. Data is stored in a single SQLite .mfl file
 on this machine and never leaves it — no account, no cloud, no telemetry.
EOF

  # Refresh the menu/icon caches. desktop-file-utils and hicolor-icon-theme both
  # ship dpkg triggers that usually do this already; the explicit calls make the
  # entry appear on desktops where they don't, and are harmless when absent.
  cat > "$root/DEBIAN/postinst" <<'EOF'
#!/bin/sh
set -e
if [ "$1" = "configure" ]; then
    command -v update-desktop-database >/dev/null 2>&1 && \
        update-desktop-database -q /usr/share/applications || true
    command -v gtk-update-icon-cache >/dev/null 2>&1 && \
        gtk-update-icon-cache -qtf /usr/share/icons/hicolor || true
fi
exit 0
EOF
  cat > "$root/DEBIAN/postrm" <<'EOF'
#!/bin/sh
set -e
if [ "$1" = "remove" ] || [ "$1" = "purge" ]; then
    command -v update-desktop-database >/dev/null 2>&1 && \
        update-desktop-database -q /usr/share/applications || true
    command -v gtk-update-icon-cache >/dev/null 2>&1 && \
        gtk-update-icon-cache -qtf /usr/share/icons/hicolor || true
fi
exit 0
EOF
  chmod 755 "$root/DEBIAN/postinst" "$root/DEBIAN/postrm"

  # Uninstall must never touch the user's data: the .mfl file lives in
  # ~/Documents and the sidecars under ~/.local/share + ~/.config (ADR-050/109),
  # none of which this package owns.
  local out="$DIST/${SLUG}_${VERSION}_${ARCH_DEB}.deb"
  # --root-owner-group: files land as root:root without needing fakeroot.
  dpkg-deb --root-owner-group --build "$root" "$out" >/dev/null
  echo "==> Package: $out"
}

# ── 3. portable tarball ──────────────────────────────────────────────────────
# For distros the .deb doesn't serve. Extract it anywhere and run the binary
# (or double-click it in a file manager); install.sh adds the per-user menu
# entry under ~/.local for people who want one.
build_tarball() {
  echo "==> Building portable tarball"
  local name="$SLUG-$VERSION-$ARCH_RAW"
  local root="$STAGE/tar/$name"
  rm -rf "$STAGE/tar"
  mkdir -p "$root"

  cp -a "$BUILT/." "$root/"
  cp "packaging/linux/$SLUG.desktop" "$root/"
  install -Dm755 packaging/linux/install.sh "$root/install.sh"
  mkdir -p "$root/icons"
  local size
  for size in "${ICON_SIZES[@]}"; do
    [[ -f "assets/icons/mfl_icon_${size}.png" ]] && \
      cp "assets/icons/mfl_icon_${size}.png" "$root/icons/"
  done
  cat > "$root/README.txt" <<EOF
$APP_NAME $VERSION — portable Linux build

Run it:        ./$SLUG          (or double-click it in your file manager)
Add to menu:   ./install.sh     (per-user, into ~/.local — no root needed)
Remove:        ./install.sh --uninstall

Nothing else to install: Python and Qt are inside this folder. Your data is
written to ~/Documents/My Financial Life/ and is untouched by installing,
moving or deleting this folder.
EOF

  local out="$DIST/$name.tar.gz"
  tar -czf "$out" -C "$STAGE/tar" "$name"
  echo "==> Tarball: $out"
}

# ── 4. AppImage ──────────────────────────────────────────────────────────────
# The one-file, double-click artifact. appimagetool is not in the distro repos,
# so it is fetched on demand (and run with --appimage-extract-and-run, which
# avoids needing libfuse2 on the build machine — Ubuntu 24.04 has none).
find_appimagetool() {
  if [[ -n "${APPIMAGETOOL:-}" && -x "$APPIMAGETOOL" ]]; then echo "$APPIMAGETOOL"; return; fi
  if command -v appimagetool >/dev/null 2>&1; then command -v appimagetool; return; fi
  local cached="build/appimagetool-$ARCH_RAW.AppImage"
  if [[ -x "$cached" ]]; then echo "$cached"; return; fi
  local url="https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-$ARCH_RAW.AppImage"
  mkdir -p build
  if command -v curl >/dev/null 2>&1 && curl -fsSL -o "$cached" "$url"; then
    chmod +x "$cached"; echo "$cached"; return
  fi
  if command -v wget >/dev/null 2>&1 && wget -qO "$cached" "$url"; then
    chmod +x "$cached"; echo "$cached"; return
  fi
  rm -f "$cached"
  return 1
}

build_appimage() {
  echo "==> Building AppImage"
  local tool
  if ! tool="$(find_appimagetool)"; then
    echo "    appimagetool unavailable (no PATH copy and the download failed)."
    echo "    Skipping the AppImage — the .deb and tarball are unaffected."
    return
  fi

  local appdir="$STAGE/AppDir"
  rm -rf "$appdir"
  mkdir -p "$appdir/usr/bin" "$appdir/usr/share/applications"
  cp -a "$BUILT" "$appdir/usr/bin/$SLUG.dir"
  install_icons "$appdir/usr/share"

  # Exec is resolved inside the mounted AppDir, so it must be the in-AppDir path.
  sed "s|^Exec=.*|Exec=$SLUG|" "packaging/linux/$SLUG.desktop" \
    > "$appdir/usr/share/applications/$SLUG.desktop"
  cp "$appdir/usr/share/applications/$SLUG.desktop" "$appdir/$SLUG.desktop"
  cp "assets/icons/mfl_icon_256.png" "$appdir/$SLUG.png"
  ln -sf "usr/share/icons/hicolor/256x256/apps/$SLUG.png" "$appdir/.DirIcon"

  cat > "$appdir/AppRun" <<EOF
#!/bin/sh
# AppImage entry point. \$APPDIR is the mount point of the extracted image.
HERE="\$(dirname "\$(readlink -f "\$0")")"
exec "\$HERE/usr/bin/$SLUG.dir/$SLUG" "\$@"
EOF
  chmod +x "$appdir/AppRun"

  local pretty="${APP_NAME// /_}"
  local out="$DIST/$pretty-$VERSION-$ARCH_RAW.AppImage"
  rm -f "$out"
  # ARCH is what appimagetool stamps into the image; it can't infer it here.
  if ARCH="$ARCH_RAW" "$tool" --appimage-extract-and-run "$appdir" "$out" >/dev/null 2>&1 \
     || ARCH="$ARCH_RAW" "$tool" "$appdir" "$out"; then
    chmod +x "$out"
    echo "==> AppImage: $out"
  else
    echo "    appimagetool failed — skipping the AppImage." >&2
  fi
}

[[ "$WANT_DEB" -eq 1 ]] && build_deb
[[ "$WANT_TAR" -eq 1 ]] && build_tarball
[[ "$WANT_APPIMAGE" -eq 1 ]] && build_appimage

echo "==> Done. Artifacts in $DIST/:"
ls -1sh "$DIST" | grep -Ei '\.deb|\.tar\.gz|\.AppImage' || true
