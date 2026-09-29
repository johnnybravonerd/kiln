#!/usr/bin/env bash
# Assemble the self-contained Windows folder /workspace/Kiln-windows and its zip.
# Needs: /workspace/kiln-toolchain/python-*-embed-amd64.zip and zig-x86_64-windows-*.zip
set -euo pipefail
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TC="${KILN_TOOLCHAIN_DIR:-/workspace/kiln-toolchain}"
OUT="${1:-/workspace/Kiln-windows}"
PYZIP="$(ls "$TC"/python-*-embed-amd64.zip | sort -V | tail -1)"
ZIGZIP="$(ls "$TC"/zig-x86_64-windows-*.zip | sort -V | tail -1)"
rm -rf "$OUT" && mkdir -p "$OUT/toolchain/python" "$OUT/toolchain/zig"
cp "$SRC/kiln.py" "$OUT/"
cp -r "$SRC/kiln_lang" "$SRC/runtime" "$SRC/examples" "$SRC/lib" "$OUT/"
find "$OUT" -name __pycache__ -prune -exec rm -rf {} +
cp "$SRC/windows/Kiln.bat" "$SRC/windows/kiln-shell.bat" "$SRC/windows/make-desktop-shortcut.bat" "$SRC/windows/HOW-TO-EDIT-LAUNCHER.txt" "$SRC/windows/kiln.ico" "$OUT/"
cat "$SRC/windows/README-WINDOWS.md" "$SRC/README.md" > "$OUT/README.md"
python3 - "$PYZIP" "$ZIGZIP" "$OUT" << 'PY'
import sys, zipfile, os, shutil, glob
py, zg, out = sys.argv[1:]
zipfile.ZipFile(py).extractall(os.path.join(out, "toolchain", "python"))
# embeddable Python ignores the script folder when a ._pth exists: add the Kiln root (..\..)
pth = glob.glob(os.path.join(out, "toolchain", "python", "python*._pth"))[0]
lines = open(pth).read().splitlines()
if "..\\.." not in lines:
    lines.insert(2, "..\\..")
open(pth, "w", newline="\r\n").write("\n".join(lines) + "\n")
tmp = os.path.join(out, "toolchain", "_zigtmp")
zipfile.ZipFile(zg).extractall(tmp)
inner = os.path.join(tmp, os.listdir(tmp)[0])
for name in os.listdir(inner):
    shutil.move(os.path.join(inner, name), os.path.join(out, "toolchain", "zig", name))
shutil.rmtree(tmp)
print("python:", os.path.basename(py), " zig:", os.path.basename(zg), " pth:", os.path.basename(pth))
PY
# Windows line endings for batch/text files
for f in "$OUT"/*.bat "$OUT"/HOW-TO-EDIT-LAUNCHER.txt; do sed -i 's/\r$//; s/$/\r/' "$f"; done
( cd "$(dirname "$OUT")" && rm -f "$(basename "$OUT").zip" && python3 -c "
import os, sys, zipfile
root = sys.argv[1]
with zipfile.ZipFile(root + '.zip', 'w', zipfile.ZIP_DEFLATED, compresslevel=9) as z:
    for d, _, files in os.walk(root):
        for f in files:
            p = os.path.join(d, f)
            z.write(p, os.path.relpath(p, os.path.dirname(root) or '.'))
" "$(basename "$OUT")" )
du -sh "$OUT" "$OUT.zip"
