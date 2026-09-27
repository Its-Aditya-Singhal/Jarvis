#!/usr/bin/env bash
# Build JARVIS.app and a drag-to-Applications .dmg on an Apple Silicon Mac.
#
#   scripts/build_dmg.sh            # -> dist/JARVIS-<version>-arm64.dmg
#
# Steps: pin model checksums -> backend sidecar (PyInstaller) -> Tauri app bundle ->
# backend copied into Contents/Resources/backend -> ad-hoc signature with the
# entitlements (camera, microphone, Apple Events) and the hardened runtime -> .dmg.
# The app is signed ad hoc, not notarized: on first open macOS asks you to confirm
# it in System Settings -> Privacy & Security (see docs/USER_GUIDE.md).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

[[ "$(uname -s)" == "Darwin" && "$(uname -m)" == "arm64" ]] || { echo "build_dmg.sh needs an Apple Silicon Mac" >&2; exit 1; }

VERSION="$(python3 -c 'import json; print(json.load(open("app/src-tauri/tauri.conf.json"))["version"])')"
PY="${PYTHON:-python3.12}"
ENTITLEMENTS="$ROOT/app/src-tauri/Entitlements.plist"
DIST="$ROOT/dist"

echo "==> JARVIS $VERSION"

echo "==> Python environment"
if [[ ! -x backend/.venv/bin/python ]]; then
  "$PY" -m venv backend/.venv
fi
backend/.venv/bin/pip install -q --upgrade pip
backend/.venv/bin/pip install -q -e backend pyinstaller

echo "==> Model checksums"
backend/.venv/bin/python scripts/pin_models.py
backend/.venv/bin/python scripts/pin_models.py --check

echo "==> Backend sidecar (PyInstaller)"
rm -rf backend/build backend/dist
(cd backend/packaging && ../.venv/bin/pyinstaller --noconfirm --clean \
  --distpath ../dist --workpath ../build jarvis-backend.spec)
SIDECAR="backend/dist/jarvis-backend"
[[ -x "$SIDECAR/jarvis-backend" ]] || { echo "PyInstaller produced no backend" >&2; exit 1; }

echo "==> Desktop app (Tauri)"
(cd app && npm ci && npx tauri build --bundles app)
APP="app/src-tauri/target/release/bundle/macos/JARVIS.app"
[[ -d "$APP" ]] || { echo "Tauri produced no app bundle" >&2; exit 1; }

echo "==> Bundling the backend"
rm -rf "$APP/Contents/Resources/backend"
ditto "$SIDECAR" "$APP/Contents/Resources/backend"  # ditto keeps the framework symlinks intact

echo "==> Signing (ad hoc, hardened runtime)"
# inside out: every native library and executable in the backend, then the app itself
find "$APP/Contents/Resources/backend" -type f \( -name "*.so" -o -name "*.dylib" \) -print0 |
  xargs -0 -n 50 codesign --force --sign - --timestamp=none --options runtime
find "$APP/Contents/Resources/backend" -type d -name "*.framework" -prune -print0 |
  xargs -0 -I{} codesign --force --sign - --timestamp=none --options runtime {} 2>/dev/null || true
codesign --force --sign - --timestamp=none --options runtime --entitlements "$ENTITLEMENTS" \
  "$APP/Contents/Resources/backend/jarvis-backend"
codesign --force --sign - --timestamp=none --options runtime --entitlements "$ENTITLEMENTS" "$APP"
codesign --verify --deep --strict --verbose=2 "$APP"

echo "==> Disk image"
mkdir -p "$DIST"
DMG="$DIST/JARVIS-$VERSION-arm64.dmg"
STAGE="$(mktemp -d)"
ditto "$APP" "$STAGE/JARVIS.app"
ln -s /Applications "$STAGE/Applications"
rm -f "$DMG"
hdiutil create -volname "JARVIS $VERSION" -srcfolder "$STAGE" -fs HFS+ -format UDZO -ov "$DMG" >/dev/null
rm -rf "$STAGE"
codesign --force --sign - --timestamp=none "$DMG"

du -h "$DMG"
echo "==> Done: $DMG"
