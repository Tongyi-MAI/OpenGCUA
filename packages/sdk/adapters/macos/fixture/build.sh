#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/../../.."
fixture_app="$PWD/artifacts/macos/MAI Fixture.app"
mkdir -p "$fixture_app/Contents/MacOS"
rm -f "$fixture_app/Contents/MacOS/mai-fixture"
swiftc adapters/macos/fixture/main.swift -o "$fixture_app/Contents/MacOS/mai-fixture" -framework Cocoa
cat > "$fixture_app/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?><!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd"><plist version="1.0"><dict><key>CFBundleExecutable</key><string>mai-fixture</string><key>CFBundleIdentifier</key><string>dev.mai-ui.fixture</string><key>CFBundleName</key><string>MAI Fixture</string><key>CFBundlePackageType</key><string>APPL</string><key>NSHighResolutionCapable</key><true/></dict></plist>
PLIST
codesign --force --sign - "$fixture_app"
