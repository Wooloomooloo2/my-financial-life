#!/usr/bin/env bash
# Build the Mac App Store submission (ADR-125): .app -> sandbox-sign -> .pkg -> upload.
#
# Usage:  ./packaging/build_mas.sh
#
# This is the App-Store counterpart to build_macos.sh (which makes a direct,
# Developer-ID-notarized .dmg — now a dev-only convenience per ADR-125). It uses
# the SAME packaging/mfl.spec, then signs for the sandbox and wraps the app in a
# signed installer .pkg for App Store Connect.
#
# Everything past the PyInstaller step is OFF unless the env vars below are set,
# so out of the box this just produces a working *unsigned* .app and tells you
# what's missing. A real submission needs the Apple Developer Program (org), an
# App Store provisioning profile, and the two MAS certificates:
#
#   MAS_APP_CERT          "Apple Distribution: NAME (TEAMID)"
#                         (or "3rd Party Mac Developer Application: NAME (TEAMID)")
#   MAS_INSTALLER_CERT    "3rd Party Mac Developer Installer: NAME (TEAMID)"
#   MAS_PROVISION_PROFILE path to the .provisionprofile downloaded from the portal
#   MAS_API_KEY           App Store Connect API key id   (optional — auto-upload)
#   MAS_API_ISSUER        App Store Connect API issuer id (optional — auto-upload)
#
# The bundle id signed here must match the App Store Connect app record
# (packaging/mfl.spec: bundle_identifier = "life.myfinancial.app").
set -euo pipefail
cd "$(dirname "$0")/.."

APP_NAME="My Financial Life"
APP="dist/${APP_NAME}.app"
PKG="dist/${APP_NAME}.pkg"
ENTITLEMENTS="packaging/MyFinancialLife.entitlements"

echo "==> Stamping build metadata (store build — licensing dormant, ADR-125)"
python packaging/stamp_build_info.py --store

echo "==> Running PyInstaller (packaging/mfl.spec)"
pyinstaller --noconfirm --clean packaging/mfl.spec

if [[ ! -d "$APP" ]]; then
  echo "ERROR: $APP was not produced" >&2
  exit 1
fi

if [[ -z "${MAS_APP_CERT:-}" ]]; then
  cat >&2 <<'EOF'
==> MAS_APP_CERT not set — produced an UNSIGNED .app only.
    A Mac App Store .pkg requires sandbox signing; set the env vars and re-run:
      MAS_APP_CERT          "Apple Distribution: NAME (TEAMID)"
      MAS_INSTALLER_CERT    "3rd Party Mac Developer Installer: NAME (TEAMID)"
      MAS_PROVISION_PROFILE path/to/app.provisionprofile
      MAS_API_KEY + MAS_API_ISSUER  (optional, to auto-upload)
EOF
  exit 0
fi

# The provisioning profile must be inside the bundle BEFORE it is signed.
if [[ -n "${MAS_PROVISION_PROFILE:-}" ]]; then
  echo "==> Embedding provisioning profile"
  cp "$MAS_PROVISION_PROFILE" "$APP/Contents/embedded.provisionprofile"
else
  echo "WARNING: MAS_PROVISION_PROFILE not set — App Store Connect will reject the upload without it." >&2
fi

# Sign inside-out: every nested Mach-O first, then the outer bundle. The Mac App
# Store rejects `codesign --deep`, and only the main bundle carries the sandbox
# entitlements (nested code inherits them at runtime).
echo "==> Signing nested frameworks / dylibs"
find "$APP/Contents" \( -name "*.dylib" -o -name "*.so" \) -type f -print0 |
  while IFS= read -r -d '' f; do
    codesign --force --timestamp --sign "$MAS_APP_CERT" "$f"
  done
find "$APP/Contents" -name "*.framework" -type d -print0 |
  while IFS= read -r -d '' fw; do
    codesign --force --timestamp --sign "$MAS_APP_CERT" "$fw"
  done

echo "==> Signing the app bundle with sandbox entitlements"
codesign --force --timestamp --entitlements "$ENTITLEMENTS" \
  --sign "$MAS_APP_CERT" "$APP"
codesign --verify --deep --strict --verbose=2 "$APP"

if [[ -z "${MAS_INSTALLER_CERT:-}" ]]; then
  echo "ERROR: MAS_INSTALLER_CERT not set — required to build a submittable .pkg" >&2
  exit 1
fi

echo "==> Building installer package (.pkg)"
rm -f "$PKG"
productbuild --component "$APP" /Applications --sign "$MAS_INSTALLER_CERT" "$PKG"

if [[ -n "${MAS_API_KEY:-}" && -n "${MAS_API_ISSUER:-}" ]]; then
  echo "==> Uploading to App Store Connect"
  xcrun altool --upload-app -f "$PKG" -t macos \
    --apiKey "$MAS_API_KEY" --apiIssuer "$MAS_API_ISSUER"
else
  echo "==> Upload skipped. Submit $PKG via the Transporter app, or set"
  echo "    MAS_API_KEY + MAS_API_ISSUER to auto-upload."
fi

echo "==> Done: $PKG"
