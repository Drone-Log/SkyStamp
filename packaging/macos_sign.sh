#!/bin/bash
# Sign SkyStamp.app with a Developer ID, build the .dmg, notarize and staple it.
#
#   packaging/macos_sign.sh "Developer ID Application: Name (TEAMID)" dist/SkyStamp.app SkyStamp.dmg
#
# Notarization credentials (pick one):
#   NOTARY_PROFILE=name                      a profile saved with `xcrun notarytool store-credentials`
#   ASC_KEY_PATH, ASC_KEY_ID, ASC_ISSUER_ID  an App Store Connect API key (used in CI)
# Without either, the dmg is signed but not notarized.
set -euo pipefail

IDENTITY="$1"
APP="$2"
DMG="$3"
HERE="$(cd "$(dirname "$0")" && pwd)"
ENT="$HERE/entitlements.plist"

sign() {
  codesign --force --timestamp --options runtime --entitlements "$ENT" --sign "$IDENTITY" "$1"
}

echo "Signing nested binaries…"
# Every Mach-O inside the bundle (Python libs, Qt frameworks, bundled ffmpeg), innermost first
find "$APP/Contents" -type f -print0 | while IFS= read -r -d '' f; do
  if file -b "$f" | grep -q "Mach-O"; then
    sign "$f"
  fi
done
# Framework bundles, then the app itself
find "$APP/Contents" -depth -type d -name "*.framework" -print0 | while IFS= read -r -d '' fw; do
  sign "$fw"
done
sign "$APP"
codesign --verify --deep --strict --verbose=2 "$APP"

echo "Building dmg…"
rm -f "$DMG"
python "$HERE/make_dmg.py" "$APP" "$DMG"
codesign --force --timestamp --sign "$IDENTITY" "$DMG"

if [[ -n "${NOTARY_PROFILE:-}" ]]; then
  xcrun notarytool submit "$DMG" --keychain-profile "$NOTARY_PROFILE" --wait
elif [[ -n "${ASC_KEY_PATH:-}" ]]; then
  xcrun notarytool submit "$DMG" --key "$ASC_KEY_PATH" --key-id "$ASC_KEY_ID" --issuer "$ASC_ISSUER_ID" --wait
else
  echo "No notarization credentials: dmg is signed but NOT notarized."
  exit 0
fi
# Apple's ticket service sometimes drops the connection right after notarization: retry
for attempt in 1 2 3 4 5 6; do
  if xcrun stapler staple "$DMG"; then
    break
  fi
  if [[ $attempt == 6 ]]; then
    echo "Stapling failed after $attempt attempts." >&2
    exit 1
  fi
  echo "Stapling failed (attempt $attempt), retrying in $((attempt * 15))s…"
  sleep $((attempt * 15))
done
spctl --assess --type open --context context:primary-signature --verbose=2 "$DMG"
echo "Done: $DMG is signed, notarized and stapled."
