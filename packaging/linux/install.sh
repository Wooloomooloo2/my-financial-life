#!/usr/bin/env bash
# Per-user installer for the portable Linux build (ADR-192).
#
# Ships inside the tarball, next to the binary. Copies the app into
# ~/.local/opt, puts a launcher on PATH at ~/.local/bin, and registers the
# .desktop entry + icons under ~/.local/share so "My Financial Life" appears in
# the applications menu. No root, nothing outside $HOME.
#
#   ./install.sh              install (or upgrade in place)
#   ./install.sh --uninstall  remove everything this script installed
#
# Your data is NOT touched by either: the .mfl file lives in
# ~/Documents/My Financial Life/, with logs, snapshots and settings under
# ~/.local/share/MFL and ~/.config/MFL (ADR-050/109).
set -euo pipefail

SLUG="my-financial-life"
APP_NAME="My Financial Life"
SRC="$(cd "$(dirname "$0")" && pwd)"

PREFIX="${XDG_DATA_HOME:-$HOME/.local/share}"
OPT_DIR="$HOME/.local/opt/$SLUG"
BIN_DIR="$HOME/.local/bin"
DESKTOP_DIR="$PREFIX/applications"
ICON_DIR="$PREFIX/icons/hicolor"

refresh_caches() {
  command -v update-desktop-database >/dev/null 2>&1 && \
    update-desktop-database -q "$DESKTOP_DIR" || true
  command -v gtk-update-icon-cache >/dev/null 2>&1 && \
    gtk-update-icon-cache -qtf "$ICON_DIR" || true
}

uninstall() {
  echo "==> Removing $APP_NAME"
  rm -rf "$OPT_DIR"
  rm -f "$BIN_DIR/$SLUG" "$DESKTOP_DIR/$SLUG.desktop"
  find "$ICON_DIR" -name "$SLUG.png" -delete 2>/dev/null || true
  refresh_caches
  echo "==> Removed. Your data in ~/Documents/$APP_NAME was left untouched."
}

if [[ "${1:-}" == "--uninstall" ]]; then uninstall; exit 0; fi
if [[ $# -gt 0 ]]; then echo "Usage: $0 [--uninstall]" >&2; exit 2; fi

if [[ ! -x "$SRC/$SLUG" ]]; then
  echo "ERROR: $SRC/$SLUG not found — run this from inside the extracted folder." >&2
  exit 1
fi

echo "==> Installing $APP_NAME to $OPT_DIR"
rm -rf "$OPT_DIR"
mkdir -p "$OPT_DIR" "$BIN_DIR" "$DESKTOP_DIR"
# Everything except this installer and its companions — the app folder should
# hold the app.
tar -cf - -C "$SRC" --exclude=install.sh --exclude=README.txt \
    --exclude="$SLUG.desktop" --exclude=icons . | tar -xf - -C "$OPT_DIR"

ln -sf "$OPT_DIR/$SLUG" "$BIN_DIR/$SLUG"

for size in 16 32 64 128 256 512; do
  src="$SRC/icons/mfl_icon_${size}.png"
  [[ -f "$src" ]] || continue
  install -Dm644 "$src" "$ICON_DIR/${size}x${size}/apps/$SLUG.png"
done

# An absolute Exec — ~/.local/bin is on PATH in most shells but not reliably in
# the session environment the desktop menu launches from.
sed "s|^Exec=.*|Exec=$OPT_DIR/$SLUG|" "$SRC/$SLUG.desktop" \
  > "$DESKTOP_DIR/$SLUG.desktop"
chmod 644 "$DESKTOP_DIR/$SLUG.desktop"

refresh_caches

echo "==> Installed."
echo "    Menu:     look for \"$APP_NAME\" (may need a re-login on some desktops)"
echo "    Terminal: $SLUG   (if $BIN_DIR is on your PATH)"
echo "    Remove:   $SRC/install.sh --uninstall"
