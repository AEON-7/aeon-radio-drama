#!/usr/bin/env bash
# comfyui-start.sh — launch the local ComfyUI server in the background.
# Reads COMFYUI_ROOT / COMFYUI_URL from .env. Idempotent: no-ops if already running.
#
# The interpreter defaults to $COMFYUI_ROOT/venv/bin/python; override with
# COMFYUI_PYTHON (in .env or the environment) for embedded-Python / conda /
# non-standard layouts.

set -euo pipefail
[[ -f .env ]] && { set -a; source .env; set +a; }

COMFYUI_URL="${COMFYUI_URL:-http://127.0.0.1:8188}"
COMFYUI_ROOT="${COMFYUI_ROOT:-}"
COMFYUI_PYTHON="${COMFYUI_PYTHON:-}"

c_red(){ printf '\033[31m%s\033[0m\n' "$*"; }
c_grn(){ printf '\033[32m%s\033[0m\n' "$*"; }
c_yel(){ printf '\033[33m%s\033[0m\n' "$*"; }
c_blu(){ printf '\033[36m%s\033[0m\n' "$*"; }

if [[ -z "$COMFYUI_ROOT" ]]; then
    c_red "COMFYUI_ROOT not set in .env — can't find the ComfyUI install."
    exit 1
fi
COMFYUI_PYTHON="${COMFYUI_PYTHON:-$COMFYUI_ROOT/venv/bin/python}"
if [[ ! -x "$COMFYUI_PYTHON" ]]; then
    c_red "No usable Python at $COMFYUI_PYTHON."
    c_yel "     Install ComfyUI's venv there, or set COMFYUI_PYTHON in .env."
    exit 1
fi

# Parse host/port out of COMFYUI_URL (e.g. http://127.0.0.1:8188)
host_port="${COMFYUI_URL#http://}"
host_port="${host_port#https://}"
host_port="${host_port%%/*}"
host="${host_port%%:*}"
port="${host_port##*:}"

pidfile="$COMFYUI_ROOT/comfyui.pid"
logfile="$COMFYUI_ROOT/comfyui.log"

if curl -sf "$COMFYUI_URL/system_stats" >/dev/null 2>&1; then
    c_grn "==> ComfyUI already reachable at $COMFYUI_URL"
    exit 0
fi

if [[ -f "$pidfile" ]] && kill -0 "$(cat "$pidfile")" 2>/dev/null; then
    c_yel "==> A ComfyUI process is already running (PID $(cat "$pidfile")) but not yet responding — give it a moment."
    exit 0
fi

c_blu "==> Starting ComfyUI ($host:$port)"
cd "$COMFYUI_ROOT"
nohup "$COMFYUI_PYTHON" main.py --listen "$host" --port "$port" > "$logfile" 2>&1 &
echo $! > "$pidfile"
disown

c_blu "    Waiting for it to come up..."
for _ in $(seq 1 30); do
    if curl -sf "$COMFYUI_URL/system_stats" >/dev/null 2>&1; then
        c_grn "==> ComfyUI up at $COMFYUI_URL (PID $(cat "$pidfile"))"
        c_blu "    Log: $logfile"
        exit 0
    fi
    sleep 1
done

c_red "==> ComfyUI didn't come up within 30s — check $logfile"
exit 1