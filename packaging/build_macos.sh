#!/usr/bin/env bash
# Build the macOS app (ADR-104): .app -> (optional sign + notarize) -> .dmg.
#
# Usage:  ./packaging/build_macos.sh
#
# Signing + notarization are OFF unless the env vars below are set, so this
# produces a working *unsigned* .app/.dmg out of the box (Gatekeeper will warn
# until signed — that needs the Apple Developer account, ADR-078 K1):
#   MACOS_SIGN_IDENTITY   "Developer ID Application: Name (TEAMID)"
#   AC_NOTARY_PROFILE     a `xcrun notarytool store-credentials` profile name
set -euo pipefail
cd "$(dirname "$0")/.."

APP_NAME="My Financial Life"
APP="dist/${APP_NAME}.app"
DMG="dist/${APP_NAME}.dmg"

echo "==> Stamping build metadata"
python packaging/stamp_build_info.py

echo "==> Running PyInstaller"
pyinstaller --noconfirm --clean packaging/mfl.spec

if [[ ! -d "$APP" ]]; then
  echo "ERROR: $APP was not produced" >&2
  exit 1
fi

if [[ -n "${MACOS_SIGN_IDENTITY:-}" ]]; then
  echo "==> Code-signing (hardened runtime)"
  codesign --deep --force --options runtime --timestamp \
    --sign "$MACOS_SIGN_IDENTITY" "$APP"
  codesign --verify --strict --verbose=2 "$APP"
else
  echo "==> MACOS_SIGN_IDENTITY not set — skipping signing (UNSIGNED build)."
fi

echo "==> Building DMG"
rm -f "$DMG"
hdiutil create -volname "$APP_NAME" -srcfolder "$APP" -ov -format UDZO "$DMG"

if [[ -n "${MACOS_SIGN_IDENTITY:-}" && -n "${AC_NOTARY_PROFILE:-}" ]]; then
  echo "==> Notarizing DMG"
  xcrun notarytool submit "$DMG" --keychain-profile "$AC_NOTARY_PROFILE" --wait
  xcrun stapler staple "$DMG"
else
  echo "==> Notarization skipped (need MACOS_SIGN_IDENTITY + AC_NOTARY_PROFILE)."
fi

echo "==> Done: $DMG"
