#!/usr/bin/env bash
set -Eeuo pipefail

# Install this fork into a NEW virtual environment. Never mutates an existing one.
if [[ $# -ne 1 ]]; then
    echo "Uso: $0 /percorso/nuovo-venv" >&2
    exit 2
fi
target_venv=$1
if [[ -e "$target_venv" ]]; then
    echo "Il percorso esiste già: $target_venv" >&2
    exit 2
fi
command -v uv >/dev/null || { echo "uv non disponibile" >&2; exit 2; }
repo_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
wheel="$repo_dir/dist/marker_pdf-2.0.0+antimix.1-py3-none-any.whl"

uv build --wheel "$repo_dir"
test -f "$wheel" || { echo "Wheel mancante: $wheel" >&2; exit 1; }
python3 -m venv "$target_venv"
uv pip install --python "$target_venv/bin/python" --no-deps \
    --index-url https://download.pytorch.org/whl/cpu \
    'torch==2.13.0+cpu' 'torchvision==0.28.0+cpu'
uv pip install --python "$target_venv/bin/python" --no-deps \
    -r "$repo_dir/antimix_gui/requirements-linux-cpu.lock"
uv pip install --python "$target_venv/bin/python" --no-deps "$wheel"
uv pip check --python "$target_venv/bin/python"
"$target_venv/bin/marker_single" --help >/dev/null
echo "Installato in $target_venv. Il launcher esistente non è stato modificato."
