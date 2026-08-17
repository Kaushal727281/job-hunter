#!/usr/bin/env bash
# ─────────────────────────────────────────────
#  Job Hunter — macOS / Linux Start
#  Run: bash run.sh
# ─────────────────────────────────────────────
set -e
cd "$(dirname "$0")"

PORT=5000

ACTIVATE=""
if [ -f "venv/bin/activate" ]; then
  ACTIVATE="venv/bin/activate"
elif [ -f ".venv/bin/activate" ]; then
  ACTIVATE=".venv/bin/activate"
else
  echo "[X] Virtual environment not found (looked for venv and .venv)."
  echo "    Please run setup.sh first."
  exit 1
fi

# ── If the port is already running THIS app, kill it and restart ──
# Only kills it if the process is actually app.py from this folder — never
# touches an unrelated program that happens to be using the port (e.g.
# macOS AirPlay Receiver also defaults to 5000).
EXISTING_PID=$(lsof -ti tcp:$PORT || true)
if [ -n "$EXISTING_PID" ]; then
  CMD=$(ps -p "$EXISTING_PID" -o command= 2>/dev/null || true)
  if echo "$CMD" | grep -q "app.py"; then
    echo "[!] Port $PORT is already running this app (PID $EXISTING_PID) — restarting it..."
    kill "$EXISTING_PID"
    sleep 2
  else
    echo "[X] Port $PORT is in use by a different program (PID $EXISTING_PID), not job-hunter:"
    echo "      $CMD"
    echo "    Not killing it automatically — stop it yourself or change PORT in app.py."
    echo "    (On macOS this is often AirPlay Receiver — System Settings > General > AirDrop & Handoff)"
    exit 1
  fi
fi

source "$ACTIVATE"
echo "[OK] Starting Job Hunter..."
echo "[OK] Open browser at: http://localhost:$PORT"
echo "     Press Ctrl+C to stop."
echo ""
python app.py
