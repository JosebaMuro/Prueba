#!/usr/bin/env bash
# Genera dist/StudioIA-Windows-x64.zip desde Linux o macOS (no necesita Windows).
# Requisitos: go >= 1.22, uv (pip install uv), curl, zip.
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT=$PWD; DIST=${DIST:-$ROOT/dist}; OUT=$DIST/StudioIA; PYVER=3.12.10
CACHE=${CACHE:-$ROOT/build/.cache}
rm -rf "$OUT" && mkdir -p "$OUT"/{runtime,app,voices}

echo "== 1/4 Python $PYVER para Windows (paquete oficial de NuGet)"
mkdir -p "$CACHE"
[ -f "$CACHE/python.nupkg" ] || curl -fsSL -o "$CACHE/python.nupkg" \
  "https://api.nuget.org/v3-flatcontainer/python/$PYVER/python.$PYVER.nupkg"
TMP=$(mktemp -d); unzip -q "$CACHE/python.nupkg" 'tools/*' -d "$TMP"
cp -r "$TMP/tools/." "$OUT/runtime/"; rm -rf "$TMP"
rm -rf "$OUT/runtime/Lib/site-packages/pip"* "$OUT/runtime/Lib/test" "$OUT/runtime/Lib/idlelib" "$OUT/runtime/Lib/tkinter" "$OUT/runtime/tcl" 2>/dev/null || true

echo "== 2/4 Dependencias (ruedas win_amd64)"
uv pip install --quiet --target "$OUT/runtime/Lib/site-packages" \
  --python-platform x86_64-pc-windows-msvc --python-version 3.12 --only-binary :all: \
  -r backend/requirements.txt
find "$OUT/runtime" -name "__pycache__" -type d -prune -exec rm -rf {} +

echo "== 3/4 Aplicación y lanzador"
mkdir -p "$OUT/app/backend"
cp -r backend/app "$OUT/app/backend/"; cp -r frontend "$OUT/app/"
find "$OUT/app" -name "__pycache__" -type d -prune -exec rm -rf {} +
cp studio.env.example "$OUT/studio.env.example"; cp studio.env.example "$OUT/studio.env"
cp docs/voices-README.txt "$OUT/voices/LEEME.txt"
[ -f docs/pdf/Manual-Windows.pdf ] && cp docs/pdf/Manual-Windows.pdf docs/pdf/Manual-Android.pdf "$OUT/" || true
(cd launcher && (command -v go-winres >/dev/null && go-winres make --in winres/winres.json --arch amd64 || true) \
  && GOOS=windows GOARCH=amd64 CGO_ENABLED=0 go build -trimpath -ldflags "-s -w" -o "$OUT/StudioIA.exe" .)

echo "== 4/4 Empaquetado"
(cd "$DIST" && rm -f StudioIA-Windows-x64.zip && zip -qr9 StudioIA-Windows-x64.zip StudioIA)
ls -lh "$DIST/StudioIA-Windows-x64.zip"
