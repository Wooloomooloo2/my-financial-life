#!/usr/bin/env bash
# Build the Mac App Store submission (ADR-125): .app -> sandbox-sign -> .pkg -> upload.
#
# Usage:
#   ./packaging/build_mas.sh            # real MAS build (needs the certs below)
#   ./packaging/build_mas.sh --adhoc    # LOCAL sandbox test build, no Apple account
#
# This is the App-Store counterpart to build_macos.sh (which makes a direct,
# Developer-ID-notarized .dmg — now a dev-only convenience per ADR-125). It uses
# the SAME packaging/mfl.spec, then signs for the sandbox.
#
# --adhoc: ad-hoc-signs the app (codesign --sign -) WITH the sandbox entitlements,
#   so macOS enforces the App Sandbox on THIS Mac — enough to test bookmarks, the
#   container data library, store-build licensing, and the first-run folder picker
#   without any Apple Developer account. No .pkg, no upload; not notarized and not
#   distributable. The signed app is placed in ~/Applications (right-click > Open
#   the first time to clear Gatekeeper).
#
# Real build (no --adhoc): everything past PyInstaller is OFF unless these are set,
# so out of the box it produces an unsigned .app and tells you what's missing. A
# submission needs the Apple Developer Program (org), a provisioning profile, and:
#   MAS_APP_CERT          "Apple Distribution: NAME (TEAMID)"
#                         (or "3rd Party Mac Developer Application: NAME (TEAMID)")
#   MAS_INSTALLER_CERT    "3rd Party Mac Developer Installer: NAME (TEAMID)"
#   MAS_PROVISION_PROFILE path to the .provisionprofile from the portal
#   MAS_API_KEY           App Store Connect API key id   (optional — auto-upload)
#   MAS_API_ISSUER        App Store Connect API issuer id (optional — auto-upload)
#
# The bundle id signed here must match the App Store Connect app record
# (packaging/mfl.spec: bundle_identifier = "life.myfinancial.app").
set -euo pipefail
cd "$(dirname "$0")/.."

ADHOC=0
for arg in "$@"; do
  case "$arg" in
    --adhoc) ADHOC=1 ;;
    *) echo "Unknown argument: $arg (expected --adhoc or nothing)" >&2; exit 2 ;;
  esac
done

APP_NAME="My Financial Life"
SRC_APP="dist/${APP_NAME}.app"
PKG="dist/${APP_NAME}.pkg"
ENTITLEMENTS="$PWD/packaging/MyFinancialLife.entitlements"

echo "==> Stamping build metadata (store build — licensing dormant, ADR-125)"
python packaging/stamp_build_info.py --store

echo "==> Running PyInstaller (packaging/mfl.spec)"
pyinstaller --noconfirm --clean packaging/mfl.spec

if [[ ! -d "$SRC_APP" ]]; then
  echo "ERROR: $SRC_APP was not produced" >&2
  exit 1
fi

# Sign on a copy in a LOCAL (non-iCloud) staging dir. A build under an
# iCloud-synced folder (~/Documents, ~/Desktop) carries com.apple.FinderInfo +
# com.apple.fileprovider xattrs that the fileprovider daemon re-applies *in
# place*, and codesign rejects them ("resource fork, Finder information, or
# similar detritus not allowed"). $TMPDIR (/var/folders/...) is local, so a
# strip there sticks.
STAGE="$(mktemp -d "${TMPDIR:-/tmp}/mfl-mas.XXXXXX")"
cp -R "$SRC_APP" "$STAGE/"
APP="$STAGE/${APP_NAME}.app"
echo "==> Staged to local dir for signing: $APP"
echo "==> Clearing extended attributes"
xattr -cr "$APP"

# ── choose signing identity ──────────────────────────────────────────────────
if [[ "$ADHOC" -eq 1 ]]; then
  SIGN_ID="-"                 # ad-hoc — no certificate, no account
  TS="--timestamp=none"       # secure timestamps need a real cert + network
  echo "==> AD-HOC local sandbox build (no Apple account; LOCAL TESTING ONLY)"
else
  if [[ -z "${MAS_APP_CERT:-}" ]]; then
    rm -rf "$STAGE"
    cat >&2 <<'EOF'
==> MAS_APP_CERT not set — produced an UNSIGNED .app only (dist/).
    For a submittable build set MAS_APP_CERT / MAS_INSTALLER_CERT /
    MAS_PROVISION_PROFILE (+ MAS_API_KEY/MAS_API_ISSUER to upload).
    To test the sandbox locally without an Apple account, re-run:
        ./packaging/build_mas.sh --adhoc
EOF
    exit 0
  fi
  SIGN_ID="$MAS_APP_CERT"
  TS="--timestamp"
  # The provisioning profile must be inside the bundle BEFORE it is signed.
  if [[ -n "${MAS_PROVISION_PROFILE:-}" ]]; then
    echo "==> Embedding provisioning profile"
    cp "$MAS_PROVISION_PROFILE" "$APP/Contents/embedded.provisionprofile"
  else
    echo "WARNING: MAS_PROVISION_PROFILE not set — App Store Connect will reject the upload without it." >&2
  fi
fi

# ── sign inside-out: nested Mach-O first, then the outer bundle ──────────────
# The Mac App Store rejects `codesign --deep`; only the main bundle carries the
# sandbox entitlements (nested code inherits them at runtime).
echo "==> Signing nested frameworks / dylibs"
find "$APP/Contents" \( -name "*.dylib" -o -name "*.so" \) -type f -print0 |
  while IFS= read -r -d '' f; do
    codesign --force $TS --sign "$SIGN_ID" "$f"
  done
find "$APP/Contents" -name "*.framework" -type d -print0 |
  while IFS= read -r -d '' fw; do
    codesign --force $TS --sign "$SIGN_ID" "$fw"
  done

echo "==> Signing the app bundle with sandbox entitlements"
codesign --force $TS --entitlements "$ENTITLEMENTS" --sign "$SIGN_ID" "$APP"
codesign --verify --deep --strict --verbose=2 "$APP"

if [[ "$ADHOC" -eq 1 ]]; then
  OUT_DIR="$HOME/Applications"
  OUT="$OUT_DIR/${APP_NAME}.app"
  mkdir -p "$OUT_DIR"
  rm -rf "$OUT"
  mv "$APP" "$OUT"
  rm -rf "$STAGE"
  cat <<EOF

==> Ad-hoc sandboxed build ready: $OUT
    Launch it sandboxed:   open "$OUT"
    First launch only:     right-click the app in Finder > Open (clears Gatekeeper).
    Its sandbox container:  ~/Library/Containers/life.myfinancial.app/Data
    LOCAL TESTING ONLY — not notarized, not distributable.
EOF
  exit 0
fi

if [[ -z "${MAS_INSTALLER_CERT:-}" ]]; then
  rm -rf "$STAGE"
  echo "ERROR: MAS_INSTALLER_CERT not set — required to build a submittable .pkg" >&2
  exit 1
fi

echo "==> Building installer package (.pkg)"
rm -f "$PKG"
productbuild --component "$APP" /Applications --sign "$MAS_INSTALLER_CERT" "$PKG"
rm -rf "$STAGE"

if [[ -n "${MAS_API_KEY:-}" && -n "${MAS_API_ISSUER:-}" ]]; then
  echo "==> Uploading to App Store Connect"
  xcrun altool --upload-app -f "$PKG" -t macos \
    --apiKey "$MAS_API_KEY" --apiIssuer "$MAS_API_ISSUER"
else
  echo "==> Upload skipped. Submit $PKG via the Transporter app, or set"
  echo "    MAS_API_KEY + MAS_API_ISSUER to auto-upload."
fi

echo "==> Done: $PKG"
