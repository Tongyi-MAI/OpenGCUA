#!/bin/bash
# Build a drag-to-Applications DMG; optionally notarize the enclosed app.
set -euo pipefail
cd "$(dirname "$0")/.."
build_dir="${1:-build}"
app="$build_dir/OpenGCUA.app"
archive="$build_dir/OpenGCUA-macOS-arm64.dmg"
stage="$(mktemp -d "${TMPDIR:-/tmp}/generalist-dmg.XXXXXX")"
trap 'rm -rf -- "$stage"' EXIT
codesign --verify --deep --strict "$app"
if [[ -n "${NOTARY_PROFILE:-}" ]]; then
  if ! codesign -dv "$app" 2>&1 | rg -q 'Authority=Developer ID Application:'; then
    echo 'Notarization requires a Developer ID build: set SIGNING_IDENTITY and rebuild.' >&2
    exit 1
  fi
  submission="$stage/OpenGCUA-notarization.zip"
  ditto -c -k --sequesterRsrc --keepParent "$app" "$submission"
  xcrun notarytool submit "$submission" --keychain-profile "$NOTARY_PROFILE" --wait
  xcrun stapler staple "$app"
  xcrun stapler validate "$app"
  spctl --assess --type execute --verbose "$app"
fi
ditto "$app" "$stage/OpenGCUA.app"
ln -s /Applications "$stage/Applications"
hdiutil create -volname "OpenGCUA" -srcfolder "$stage" -fs HFS+ \
  -format UDZO -imagekey zlib-level=9 -ov "$archive"
hdiutil verify "$archive"
(cd "$(dirname "$archive")" && shasum -a 256 "$(basename "$archive")") > "$archive.sha256"
echo "$(cd "$(dirname "$archive")" && pwd)/$(basename "$archive")"
