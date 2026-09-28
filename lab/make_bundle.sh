#!/bin/bash
# The delivery folder and zip: the wheel, the guide (HTML + PDF), the example catalog, the
# install sheet, checksums.   lab/make_bundle.sh [PYTHON]
# PYTHON runs the guide builder (playwright, markdown); WHEEL_PY builds the wheel (pip), default PYTHON.
set -euo pipefail
cd "$(dirname "$0")/.."
PY=${1:-python3}
WHEEL_PY=${WHEEL_PY:-$PY}
VER=$(grep -m1 '^version' pyproject.toml | cut -d'"' -f2)
OUT=dist/supagent-$VER
rm -rf build "$OUT" "$OUT.zip" dist/*.whl
"$WHEEL_PY" -m pip wheel -q --no-deps -w dist .
"$PY" lab/make_guide.py --out "$OUT"
cp dist/supagent-$VER-py3-none-any.whl "$OUT/"
cp examples/catalog.example.yaml INSTALL.txt "$OUT/"
(cd "$OUT" && sha256sum * > SHA256SUMS)
(cd dist && "$PY" -c "import shutil; shutil.make_archive('supagent-$VER', 'zip', '.', 'supagent-$VER')")
ls -la "$OUT" "$OUT.zip"
