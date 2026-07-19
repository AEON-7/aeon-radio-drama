#!/usr/bin/env bash
# fetch-models.sh — download the required ComfyUI model files + custom nodes
# for aeon-radio-drama. Idempotent: skips files/nodes already present.
# Stop ComfyUI first if it's running, so files aren't locked mid-download.

set -euo pipefail
[[ -f .env ]] && { set -a; source .env; set +a; }

COMFYUI_ROOT="${COMFYUI_ROOT:-}"

c_red(){ printf '\033[31m%s\033[0m\n' "$*"; }
c_grn(){ printf '\033[32m%s\033[0m\n' "$*"; }
c_yel(){ printf '\033[33m%s\033[0m\n' "$*"; }
c_blu(){ printf '\033[36m%s\033[0m\n' "$*"; }

if [[ -z "$COMFYUI_ROOT" ]]; then
    c_red "COMFYUI_ROOT not set in .env"
    exit 1
fi
py="$COMFYUI_ROOT/venv/bin/python"
if [[ ! -x "$py" ]]; then
    c_red "No venv at $COMFYUI_ROOT/venv"
    exit 1
fi

c_blu "==> [1/2] Custom node: ComfyUI-MMAudio"
node_dir="$COMFYUI_ROOT/custom_nodes/ComfyUI-MMAudio"
if [[ -d "$node_dir" ]]; then
    c_grn "      already present"
else
    git clone --quiet https://github.com/kijai/ComfyUI-MMAudio.git "$node_dir"
    if [[ -f "$node_dir/requirements.txt" ]]; then
        "$py" -m pip install --quiet -r "$node_dir/requirements.txt"
    fi
    c_grn "      installed"
fi

c_blu "==> [2/2] Model files (COMFYUI_ROOT=$COMFYUI_ROOT)"
COMFYUI_ROOT="$COMFYUI_ROOT" HF_TOKEN="${HF_TOKEN:-}" "$py" "$(dirname "$0")/scripts/fetch_models.py"