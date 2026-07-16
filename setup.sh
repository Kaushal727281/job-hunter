#!/usr/bin/env bash
# ─────────────────────────────────────────────
#  Job Hunter — macOS / Linux Setup
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
PYTHON=""
for cmd in python3 python3.12 python3.11 python3.10 python; do
  if command -v "$cmd" &>/dev/null; then
    VER=$($cmd -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>/dev/null || echo "0.0")
    MAJOR=$(echo $VER | cut -d. -f1)
    MINOR=$(echo $VER | cut -d. -f2)
    if [ "$MAJOR" -ge 3 ] && [ "$MINOR" -ge 10 ]; then
      PYTHON=$cmd
      break
    fi
  fi
done

if [ -z "$PYTHON" ]; then
  err "Python 3.10+ not found."
  if [[ "$OSTYPE" == "darwin"* ]]; then
    err "Install via Homebrew:  brew install python"
    err "Or download from:      https://python.org"
  else
    err "Install via:  sudo apt install python3  (Ubuntu/Debian)"
    err "Or download:  https://python.org"
  fi
  exit 1
fi
ok "Python $($PYTHON --version) at $(which $PYTHON)"

# ── 2. Virtual environment ────────────────────
step "Setting up virtual environment"

# Determine correct activate path (Unix = bin/activate)
ACTIVATE=".venv/bin/activate"

if [ ! -d ".venv" ]; then
  # Try creating venv
  if ! $PYTHON -m venv .venv 2>/dev/null; then
    # Ubuntu/Debian: python3-venv may be missing
    if command -v apt-get &>/dev/null; then
      warn "Installing python3-venv..."
      sudo apt-get install -y python3-venv python3-pip -q
      $PYTHON -m venv .venv
    elif command -v yum &>/dev/null; then
      warn "Installing python3-venv..."
      sudo yum install -y python3 python3-pip -q
      $PYTHON -m venv .venv
    elif command -v brew &>/dev/null; then
      warn "Trying Homebrew python..."
      brew install python -q
      $PYTHON -m venv .venv
    else
      err "Could not create virtual environment."
      err "Try:  pip3 install -r requirements.txt  (without venv)"
      exit 1
    fi
  fi
  ok "Created .venv"
else
  ok ".venv already exists — skipping"
fi

# Verify activate script exists
if [ ! -f "$ACTIVATE" ]; then
  err ".venv was created but $ACTIVATE not found."
  err "Delete .venv and re-run:  rm -rf .venv && bash setup.sh"
  exit 1
fi

source "$ACTIVATE"
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

if grep -q "your_groq_api_key_here" .env 2>/dev/null; then
  warn "GROQ_API_KEY is not set — get a free key at: https://console.groq.com"
else
  ok "GROQ_API_KEY looks set"
fi

# ── 5. Config file ────────────────────────────
step "Setting up config.json"
if [ ! -f "config.json" ]; then
  cp config.example.json config.json
  ok "Created config.json from config.example.json"
  warn "Open config.json and update your name + job queries"
else
  ok "config.json already exists — skipping"
fi

# ── 6. Chrome / Chromium check ────────────────
step "Checking for Chrome / Chromium (needed for PDF generation)"
CHROME_FOUND=false

# macOS paths (Intel + Apple Silicon M1/M2/M4)
for path in \
  "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
  "/Applications/Chromium.app/Contents/MacOS/Chromium" \
  "$HOME/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
  "/usr/bin/google-chrome" \
  "/usr/bin/chromium-browser" \
  "/usr/bin/chromium" \
  "/snap/bin/chromium"; do
  if [ -f "$path" ]; then
    ok "Found: $path"
    CHROME_FOUND=true
    break
  fi
done

if [ "$CHROME_FOUND" = false ]; then
  for name in google-chrome google-chrome-stable chromium chromium-browser; do
    if command -v $name &>/dev/null; then
      ok "Found in PATH: $(command -v $name)"
      CHROME_FOUND=true
      break
    fi
  done
fi

if [ "$CHROME_FOUND" = false ]; then
  warn "Chrome / Chromium not found — PDF downloads won't work"
  if [[ "$OSTYPE" == "darwin"* ]]; then
    warn "Install: https://www.google.com/chrome/"
  else
    warn "Install: sudo apt install chromium-browser  or  https://www.google.com/chrome/"
  fi
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
