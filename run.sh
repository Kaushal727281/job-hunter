#!/usr/bin/env bash
# run.sh — Full job-hunt pipeline: fetch → score → apply
# Run this daily (or via cron) — no Claude needed.
#
# Usage:
#   ./run.sh              # full pipeline (fetch + score + apply)
#   ./run.sh --web        # launch web dashboard on port 5000
#   ./run.sh --status     # just show dashboard
#   ./run.sh --dry-run    # see what would be applied, no submissions
#   ./run.sh --fetch-only # only fetch + score, skip apply
#   ./run.sh --apply-only # skip fetch, jump straight to apply
#   ./run.sh --min-score 8 --limit 5

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Use the virtualenv python if it exists, else system python3
if [ -f ".venv/bin/python" ]; then
    PYTHON=".venv/bin/python"
elif [ -f "venv/bin/python" ]; then
    PYTHON="venv/bin/python"
else
    PYTHON="python3"
fi

echo ""
echo "======================================================"
echo "  JOB HUNTER PIPELINE  — $(date '+%Y-%m-%d %H:%M:%S')"
echo "======================================================"
echo "  Python: $($PYTHON --version)"
echo ""

if [ "${1:-}" = "--web" ]; then
    echo "  Starting web dashboard → http://localhost:5000"
    echo ""
    exec "$PYTHON" app.py
fi

exec "$PYTHON" run_pipeline.py "$@"
