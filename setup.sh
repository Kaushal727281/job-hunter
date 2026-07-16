#!/usr/bin/env bash
# ─────────────────────────────────────────────
#  Job Hunter — one-time setup script
#  Run once after cloning:  bash setup.sh
# ─────────────────────────────────────────────
set -e

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
ok()   { echo -e "${GREEN}  ✓ $1${NC}"; }
warn() { echo -e "${YELLOW}  ⚠ $1${NC}"; }
err()  { echo -e "${RED}  ✗ $1${NC}"; }
step() { echo -e "\n${YELLOW}▶ $1${NC}"; }

echo ""
echo "  ╔══════════════════════════════════════╗"
echo "  ║        Job Hunter — Setup            ║"
echo "  ╚══════════════════════════════════════╝"
echo ""

# ── 1. Python version ─────────────────────────
step "Checking Python version"
PYTHON=$(command -v python3 || command -v python || true)
if [ -z "$PYTHON" ]; then
  err "Python not found. Install Python 3.10+ from https://python.org"
  exit 1
fi
PY_VER=$($PYTHON -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
PY_MAJOR=$($PYTHON -c "import sys; print(sys.version_info.major)")
PY_MINOR=$($PYTHON -c "import sys; print(sys.version_info.minor)")
if [ "$PY_MAJOR" -lt 3 ] || { [ "$PY_MAJOR" -eq 3 ] && [ "$PY_MINOR" -lt 10 ]; }; then
  err "Python $PY_VER found but 3.10+ is required."
  exit 1
fi
ok "Python $PY_VER"

# ── 2. Virtual environment ────────────────────
step "Setting up virtual environment"
if [ ! -d ".venv" ]; then
  $PYTHON -m venv .venv
  ok "Created .venv"
else
  ok ".venv already exists — skipping"
fi

# Activate
source .venv/bin/activate
ok "Activated .venv"

# ── 3. Install dependencies ───────────────────
step "Installing Python dependencies"
pip install --upgrade pip -q
pip install -r requirements.txt -q
ok "All packages installed"

# ── 4. Environment file ───────────────────────
step "Setting up .env"
if [ ! -f ".env" ]; then
  cp .env.example .env
  ok "Created .env from .env.example"
  warn "Open .env and add your GROQ_API_KEY"
else
  ok ".env already exists — skipping"
fi

# Check if GROQ_API_KEY is still a placeholder
if grep -q "your_groq_api_key_here" .env 2>/dev/null; then
  warn "GROQ_API_KEY is not set in .env — tailoring won't work until you add it"
  warn "Get a free key at: https://console.groq.com"
else
  ok "GROQ_API_KEY looks set"
fi

# ── 5. Config file ────────────────────────────
step "Setting up config.json"
if [ ! -f "config.json" ]; then
  cp config.example.json config.json
  ok "Created config.json from config.example.json"
  warn "Open config.json and update your name, email, and job queries"
else
  ok "config.json already exists — skipping"
fi

# ── 6. Chrome / Chromium check ────────────────
step "Checking for Chrome / Chromium (needed for PDF generation)"
CHROME_FOUND=false
for path in \
  "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
  "/Applications/Chromium.app/Contents/MacOS/Chromium" \
  "/usr/bin/google-chrome" \
  "/usr/bin/chromium-browser" \
  "/usr/bin/chromium"; do
  if [ -f "$path" ]; then
    ok "Found: $path"
    CHROME_FOUND=true
    break
  fi
done
if [ "$CHROME_FOUND" = false ]; then
  for name in google-chrome chromium chromium-browser; do
    if command -v $name &>/dev/null; then
      ok "Found in PATH: $(command -v $name)"
      CHROME_FOUND=true
      break
    fi
  done
fi
if [ "$CHROME_FOUND" = false ]; then
  warn "Chrome / Chromium not found — PDF downloads won't work"
  warn "Install Google Chrome from: https://www.google.com/chrome/"
fi

# ── 7. Output dir ─────────────────────────────
step "Creating output directory"
mkdir -p output
ok "output/ ready"

# ── Done ──────────────────────────────────────
echo ""
echo -e "${GREEN}  ══════════════════════════════════════${NC}"
echo -e "${GREEN}   Setup complete!${NC}"
echo -e "${GREEN}  ══════════════════════════════════════${NC}"
echo ""
echo "  Next steps:"
echo ""

if grep -q "your_groq_api_key_here" .env 2>/dev/null; then
  echo "  1. Add your Groq API key to .env"
  echo "       Get free key → https://console.groq.com"
  echo ""
fi

echo "  2. Add your resume:"
echo "       Replace base_resume.html with your own HTML resume"
echo ""
echo "  3. Update config.json with your name + job queries"
echo ""
echo "  4. Start the app:"
echo "       source .venv/bin/activate"
echo "       python app.py"
echo ""
echo "  5. Open in browser:"
echo "       http://localhost:5000"
echo ""
