#!/bin/sh
#
# Build "RENPHO Ingest.app" as a universal (x86_64 + arm64) binary, so the same
# app runs on both Apple Silicon and Intel Macs.
#
#   sh launcher/build.sh
#
# Two details that matter:
#
#   * Per-slice deployment targets. -mmacosx-version-min is a single global flag,
#     so each arch is compiled separately and merged with lipo. Without this the
#     SDK stamps the current macOS (26.x) as the minimum and the app refuses to
#     launch on older Macs.
#       - x86_64 -> 10.13
#       - arm64  -> 11.0, the earliest macOS that ran on Apple Silicon
#
#   * Re-granting Full Disk Access. The bundle is ad-hoc signed, so its TCC
#     identity is derived from the binary. Rebuilding changes it, and macOS then
#     treats it as a different app: re-add it under System Settings → Privacy &
#     Security → Full Disk Access on every machine that runs the agent.
#
set -eu

HERE=$(cd "$(dirname "$0")" && pwd)
REPO=$(dirname "$HERE")
APP="$REPO/RENPHO Ingest.app"
BIN="$APP/Contents/MacOS/renpho-ingest"
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

CFLAGS="-O2 -Wall -Wextra"

echo "Compiling x86_64 slice (min macOS 10.13) ..."
clang $CFLAGS -arch x86_64 -mmacosx-version-min=10.13 \
      "$HERE/launcher.c" -o "$TMP/renpho-x86_64"

echo "Compiling arm64 slice (min macOS 11.0) ..."
clang $CFLAGS -arch arm64 -mmacosx-version-min=11.0 \
      "$HERE/launcher.c" -o "$TMP/renpho-arm64"

echo "Merging into a universal binary ..."
mkdir -p "$APP/Contents/MacOS"
cp "$HERE/Info.plist" "$APP/Contents/Info.plist"
lipo -create -output "$BIN" "$TMP/renpho-x86_64" "$TMP/renpho-arm64"
chmod +x "$BIN"

echo "Signing (ad-hoc) ..."
codesign --force --sign - "$APP"

echo
echo "Built: $APP"
lipo -archs "$BIN" | sed 's/^/  archs: /'
vtool -show-build "$BIN" 2>/dev/null | grep -E "architecture|minos|version " | sed 's/^/  /'
codesign -dv "$APP" 2>&1 | grep -E "Format|Signature" | sed 's/^/  /'
echo
echo "Reminder: re-grant Full Disk Access to the rebuilt app (see comment above)."
