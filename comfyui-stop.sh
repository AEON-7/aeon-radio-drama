#!/usr/bin/env bash
# comfyui-stop.sh — stop the local ComfyUI server started by comfyui-start.sh.

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

pidfile="$COMFYUI_ROOT/comfyui.pid"

if [[ -f "$pidfile" ]] && kill -0 "$(cat "$pidfile")" 2>/dev/null; then
    pid="$(cat "$pidfile")"
    kill "$pid"
    c_grn "==> Stopped ComfyUI (PID $pid)"
    rm -f "$pidfile"
    exit 0
fi

# Fall back to a process-name match in case the pidfile is stale/missing.
match_pids="$(pgrep -f "ComfyUI/venv/bin/python main.py" || true)"
if [[ -n "$match_pids" ]]; then
    kill $match_pids
    c_grn "==> Stopped ComfyUI (PID(s) $match_pids, found by process match)"
    rm -f "$pidfile"
    exit 0
fi

c_yel "==> No running ComfyUI process found."
rm -f "$pidfile"