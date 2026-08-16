"""
configure.py
Interactive first-run wizard — asks for the personal info the app can't
guess (name, Gmail login, LinkedIn connections export) and writes it into
.env and config.json. Safe to re-run any time to update a value; existing
values are shown as the default so pressing Enter keeps them.

Run:  python configure.py
"""

import json
import re
import shutil
import getpass
from pathlib import Path

ROOT = Path(__file__).parent
ENV_FILE = ROOT / ".env"
ENV_EXAMPLE = ROOT / ".env.example"
CONFIG_FILE = ROOT / "config.json"
CONFIG_EXAMPLE = ROOT / "config.example.json"
CSV_DEST = ROOT / "output" / "linkedin_connections.csv"

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def ask(prompt: str, default: str = "", secret: bool = False) -> str:
    hint = f" [{default}]" if default and not secret else " [leave blank to skip]" if not default else ""
    reader = getpass.getpass if secret else input
    value = reader(f"{prompt}{hint}: ").strip()
    return value or default


def read_env() -> dict:
    if not ENV_FILE.exists():
        shutil.copy(ENV_EXAMPLE, ENV_FILE)
    values, lines = {}, ENV_FILE.read_text(encoding="utf-8").splitlines()
    for line in lines:
        if "=" in line and not line.strip().startswith("#"):
            k, _, v = line.partition("=")
            values[k.strip()] = v.strip()
    return values


def write_env(updates: dict):
    lines = ENV_FILE.read_text(encoding="utf-8").splitlines()
    seen = set()
    for i, line in enumerate(lines):
        if "=" in line and not line.strip().startswith("#"):
            key = line.split("=", 1)[0].strip()
            if key in updates:
                lines[i] = f"{key}={updates[key]}"
                seen.add(key)
    for key, val in updates.items():
        if key not in seen:
            lines.append(f"{key}={val}")
    ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")


def read_config() -> dict:
    if not CONFIG_FILE.exists():
        shutil.copy(CONFIG_EXAMPLE, CONFIG_FILE)
    return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))


def write_config(cfg: dict):
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


def main():
    print()
    print("  ╔══════════════════════════════════════╗")
    print("  ║      Job Hunter — First-Run Setup    ║")
    print("  ╚══════════════════════════════════════╝")
    print()
    print("  Press Enter to keep the current/default value shown in [brackets].")
    print()

    env = read_env()
    cfg = read_config()
    candidate = cfg.setdefault("candidate", {})

    # ── Name ───────────────────────────────────────────────────────────────
    print("── Your name ─────────────────────────────────────")
    name = ask("Full name (used on your resume + outreach messages)",
               candidate.get("name", ""))
    candidate["name"] = name

    exp_default = str(candidate.get("total_experience_years", "")) or "0"
    while True:
        exp_raw = ask("Total years of experience", exp_default)
        try:
            candidate["total_experience_years"] = float(exp_raw) if "." in exp_raw else int(exp_raw)
            break
        except ValueError:
            print("  Please enter a number, e.g. 5")

    # ── Gmail ──────────────────────────────────────────────────────────────
    print()
    print("── Gmail (used to fetch replies and send your daily digest) ──────")
    print("  Needs a Gmail *App Password*, not your normal password.")
    print("  Create one at: https://myaccount.google.com/apppasswords")
    print("  (requires 2-Step Verification to be turned on for your Google account)")
    gmail_default = env.get("GMAIL_ADDRESS") or candidate.get("email", "")
    while True:
        gmail_addr = ask("Gmail address", gmail_default)
        if not gmail_addr or EMAIL_RE.match(gmail_addr):
            break
        print("  That doesn't look like a valid email address.")
    candidate["email"] = gmail_addr

    existing_pwd = env.get("GMAIL_APP_PASSWORD", "")
    pwd_hint = " (already set — press Enter to keep it)" if existing_pwd else ""
    gmail_pwd = getpass.getpass(f"Gmail App Password{pwd_hint}: ").strip() or existing_pwd

    env_updates = {}
    if gmail_addr:
        env_updates["GMAIL_ADDRESS"] = gmail_addr
    if gmail_pwd:
        env_updates["GMAIL_APP_PASSWORD"] = gmail_pwd
    # .env.example ships DIGEST_RECIPIENT as a literal placeholder — since it's
    # a non-empty string, code that does getenv("DIGEST_RECIPIENT", gmail_addr)
    # would use that placeholder as the actual send target instead of falling
    # back to gmail_addr. Point it at the real address unless already customized.
    digest = env.get("DIGEST_RECIPIENT", "")
    if gmail_addr and (not digest or digest == "your.email@gmail.com"):
        env_updates["DIGEST_RECIPIENT"] = gmail_addr

    # ── LinkedIn connections export ────────────────────────────────────────
    print()
    print("── LinkedIn connections (used to find referral contacts) ─────────")
    print("  Export it from LinkedIn: Settings & Privacy -> Data Privacy ->")
    print("  \"Get a copy of your data\" -> Connections. You'll get an email")
    print("  with a download link for a Connections.csv file.")
    csv_default = ""
    if CSV_DEST.exists():
        csv_default = "(already imported — press Enter to keep it, or paste a new path to replace it)"
    csv_path = ask("Path to your downloaded Connections.csv", csv_default)
    if csv_path and csv_path != csv_default:
        src = Path(csv_path).expanduser()
        if src.exists():
            CSV_DEST.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(src, CSV_DEST)
            print(f"  ✓ Copied to {CSV_DEST.relative_to(ROOT)}")
        else:
            print(f"  ✗ Couldn't find {src} — skipping. Re-run this script once you have it.")

    write_env(env_updates)
    write_config(cfg)

    print()
    print("  ══════════════════════════════════════")
    print("   Saved!")
    print("  ══════════════════════════════════════")
    print()
    print("  Still worth checking before your first run:")
    print("    - config.json -> job_search.queries / locations (what to search for)")
    print("    - base_resume.html -> replace with your own resume")
    if not env.get("GROQ_API_KEY") and not env.get("OLLAMA_MODEL"):
        print("    - .env -> add a GROQ_API_KEY (console.groq.com) or run setup to install Ollama locally")
    print()


if __name__ == "__main__":
    main()
