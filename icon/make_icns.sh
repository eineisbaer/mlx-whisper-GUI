#!/usr/bin/env bash
#
# Build Transcriber.icns from a 1024x1024 source PNG using only macOS built-ins.
#
#   ./make_icns.sh [source.png]
#
# With no argument it renders icon.svg (next to this script) to a PNG first.
# The result, Transcriber.icns, is what setup.py points py2app at.
#
set -euo pipefail
cd "$(dirname "$0")"

SRC="${1:-}"

if [ -z "$SRC" ]; then
    # No PNG given: render the bundled SVG to 1024x1024 via Quick Look (WebKit).
    command -v qlmanage >/dev/null 2>&1 || {
        echo "error: no source PNG given and qlmanage not available." >&2
        exit 1
    }
    echo "Rendering icon.svg -> icon.png (1024x1024)…"
    qlmanage -t -s 1024 -o . icon.svg >/dev/null 2>&1
    # qlmanage writes "icon.svg.png"; normalise the name.
    mv -f icon.svg.png icon.png
    SRC="icon.png"
fi

[ -f "$SRC" ] || { echo "error: source '$SRC' not found." >&2; exit 1; }

ICONSET="Transcriber.iconset"
rm -rf "$ICONSET"
mkdir -p "$ICONSET"

# macOS wants these exact sizes/names (1x and 2x for each logical size).
for size in 16 32 128 256 512; do
    sips -z "$size" "$size"       "$SRC" --out "$ICONSET/icon_${size}x${size}.png"      >/dev/null
    sips -z $((size*2)) $((size*2)) "$SRC" --out "$ICONSET/icon_${size}x${size}@2x.png"  >/dev/null
done

iconutil -c icns "$ICONSET" -o Transcriber.icns
rm -rf "$ICONSET"

echo "Wrote $(pwd)/Transcriber.icns"
