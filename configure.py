"""
configure.py
Interactive first-run wizard — asks for the personal info the app can't
guess (name, Gmail login, LinkedIn connections export) and writes it into
a profile's .env and config.json. Safe to re-run any time to update a
value; existing values are shown as the default so pressing Enter keeps
them. Multiple people can share one install — each gets their own profile
under profiles/<name>/, picked at the start of this script.

Run:  python configure.py
"""

import re
import shutil
import getpass
from pathlib import Path

import profiles

ROOT = Path(__file__).parent

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def ask(prompt: str, default: str = "", secret: bool = False) -> str:
    hint = f" [{default}]" if default and not secret else " [leave blank to skip]" if not default else ""
    reader = getpass.getpass if secret else input
    value = reader(f"{prompt}{hint}: ").strip()
    return value or default


def pick_profile() -> str:
    existing = profiles.list_profiles()
    print("── Which profile is this for? ──────────────────────")
    if existing:
        for i, name in enumerate(existing, 1):
            print(f"  {i}. {name.replace('-', ' ').title()}")
        print(f"  {len(existing) + 1}. + New profile")
        choice = input(f"Choose [1-{len(existing) + 1}]: ").strip()
        if choice.isdigit() and 1 <= int(choice) <= len(existing):
            return existing[int(choice) - 1]
    new_name = input("Full name for the new profile: ").strip() or "profile"
    slug = profiles.create_profile(new_name)
    print(f"  ✓ Created profile '{new_name}'")
    return slug


def read_env(env_file: Path) -> dict:
    values = {}
    for line in env_file.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.strip().startswith("#"):
            k, _, v = line.partition("=")
            values[k.strip()] = v.strip()
    return values


def write_env(env_file: Path, updates: dict):
    lines = env_file.read_text(encoding="utf-8").splitlines()
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
    env_file.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main():
    print()
    print("  ╔══════════════════════════════════════╗")
    print("  ║      Job Hunter — Profile Setup      ║")
    print("  ╚══════════════════════════════════════╝")
    print()

    slug = pick_profile()
    env_file = profiles.profile_env_path(slug)
    config_file = profiles.config_path(slug)
    csv_dest = profiles.linkedin_csv_path(slug)

    print()
    print("  Press Enter to keep the current/default value shown in [brackets].")
    print()

    env = read_env(env_file)
    import json
    cfg = json.loads(config_file.read_text(encoding="utf-8"))
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
    if csv_dest.exists():
        csv_default = "(already imported — press Enter to keep it, or paste a new path to replace it)"
    csv_path = ask("Path to your downloaded Connections.csv", csv_default)
    if csv_path and csv_path != csv_default:
        src = Path(csv_path).expanduser()
        if src.exists():
            csv_dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(src, csv_dest)
            print(f"  ✓ Copied to {csv_dest.relative_to(ROOT)}")
        else:
            print(f"  ✗ Couldn't find {src} — skipping. Re-run this script once you have it.")

    write_env(env_file, env_updates)
    config_file.write_text(json.dumps(cfg, indent=2), encoding="utf-8")

    shared_env = ROOT / ".env"
    has_shared_llm_key = False
    if shared_env.exists():
        shared_values = read_env(shared_env)
        has_shared_llm_key = bool(shared_values.get("GROQ_API_KEY") or shared_values.get("OLLAMA_MODEL"))

    print()
    print("  ══════════════════════════════════════")
    print(f"   Saved profile: {name}")
    print("  ══════════════════════════════════════")
    print()
    print("  Still worth checking before your first run:")
    print(f"    - profiles/{slug}/config.json -> job_search.locations (where to search)")
    print(f"    - profiles/{slug}/base_resume.html -> upload your resume via the web UI")
    if not has_shared_llm_key:
        print("    - .env (shared) -> add a GROQ_API_KEY (console.groq.com) or run setup to install Ollama locally")
    print()


if __name__ == "__main__":
    main()
