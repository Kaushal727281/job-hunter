"""
profiles.py
Multiple people can share one install of this app, each with their own
resume, config, and tracked jobs, switchable from a simple name picker (no
password — this is a shared family computer, not internet-facing).

Layout:
    profiles/<slug>/
        config.json
        base_resume.html (+ .html.bak)
        .env                    — profile-scoped: Gmail credentials only
        output/
            jobs.json, seen_jobs.json, cookies/, linkedin_connections.csv, ...
    .env                        — shared: LLM API keys (Groq/Gemini/etc.)

The active profile is thread-local, not a shared global: each Flask request
thread sets its own at the top of the request (from the session cookie), and
each spawned background thread (fetch/tailor/etc.) sets its own explicitly at
the top of its target function, since it doesn't inherit Flask's session.
This is what lets two people use the app from two tabs at once without
cross-contaminating each other's active profile.
"""

import json
import re
import shutil
import threading
from pathlib import Path

from dotenv import dotenv_values

ROOT = Path(__file__).parent
PROFILES_DIR = ROOT / "profiles"
CONFIG_EXAMPLE = ROOT / "config.example.json"
ENV_EXAMPLE = ROOT / ".env.example"

# Keys that belong in a profile's own .env (Gmail is tied to the person
# applying, not to the machine) — everything else in .env.example (LLM keys,
# RAPIDAPI_KEY) stays shared at the repo root.
_PROFILE_ENV_KEYS = ("GMAIL_ADDRESS", "GMAIL_APP_PASSWORD", "DIGEST_RECIPIENT")

_local = threading.local()


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "profile"


def list_profiles() -> list[str]:
    if not PROFILES_DIR.exists():
        return []
    return sorted(p.name for p in PROFILES_DIR.iterdir() if p.is_dir())


def profile_exists(name: str) -> bool:
    return (PROFILES_DIR / name).is_dir()


def get_active_profile() -> str:
    name = getattr(_local, "profile", None)
    if not name:
        raise RuntimeError("No active profile set for this thread — call set_active_profile() first")
    return name


def set_active_profile(name: str):
    _local.profile = name


def profile_dir(name: str = None) -> Path:
    return PROFILES_DIR / (name or get_active_profile())


def config_path(name: str = None) -> Path:
    return profile_dir(name) / "config.json"


def base_resume_path(name: str = None) -> Path:
    return profile_dir(name) / "base_resume.html"


def output_dir(name: str = None) -> Path:
    return profile_dir(name) / "output"


def seen_jobs_path(name: str = None) -> Path:
    return output_dir(name) / "seen_jobs.json"


def cookies_dir(name: str = None) -> Path:
    return output_dir(name) / "cookies"


def linkedin_csv_path(name: str = None) -> Path:
    return output_dir(name) / "linkedin_connections.csv"


def jobs_store_path(name: str = None) -> Path:
    return output_dir(name) / "jobs.json"


def profile_env_path(name: str = None) -> Path:
    return profile_dir(name) / ".env"


def get_profile_env(name: str = None) -> dict:
    """Read the active profile's .env as a plain dict — does NOT touch
    os.environ, so Gmail credentials never leak across profiles even if two
    people are using the app from different tabs at once."""
    path = profile_env_path(name)
    if not path.exists():
        return {}
    return dotenv_values(path)


def create_profile(name: str) -> str:
    """Create a new empty profile (config.json + .env seeded from the
    .example templates). base_resume.html is deliberately left absent —
    the existing upload-resume flow is how it gets filled in."""
    slug = slugify(name)
    pdir = profile_dir(slug)
    if pdir.exists():
        return slug
    pdir.mkdir(parents=True)
    (pdir / "output").mkdir()
    (pdir / "output" / "cookies").mkdir()

    cfg = json.loads(CONFIG_EXAMPLE.read_text(encoding="utf-8"))
    cfg["candidate"]["name"] = name
    (pdir / "config.json").write_text(json.dumps(cfg, indent=2), encoding="utf-8")

    env_lines = [
        ln for ln in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines()
        if any(ln.startswith(f"{k}=") for k in _PROFILE_ENV_KEYS)
    ]
    (pdir / ".env").write_text("\n".join(env_lines) + "\n", encoding="utf-8")
    return slug


def migrate_legacy_layout() -> str | None:
    """One-time migration: if profiles/ doesn't exist yet but a top-level
    config.json does (the pre-multi-profile layout), move it into
    profiles/<slug-of-candidate-name>/ so nothing is lost. Returns the new
    slug, or None if there was nothing to migrate."""
    legacy_config = ROOT / "config.json"
    if PROFILES_DIR.exists() or not legacy_config.exists():
        return None

    cfg = json.loads(legacy_config.read_text(encoding="utf-8"))
    name = cfg.get("candidate", {}).get("name") or "profile"
    slug = slugify(name)
    pdir = PROFILES_DIR / slug
    pdir.mkdir(parents=True)

    shutil.move(str(legacy_config), str(pdir / "config.json"))

    legacy_resume = ROOT / "base_resume.html"
    if legacy_resume.exists():
        shutil.move(str(legacy_resume), str(pdir / "base_resume.html"))
    legacy_resume_bak = ROOT / "base_resume.html.bak"
    if legacy_resume_bak.exists():
        shutil.move(str(legacy_resume_bak), str(pdir / "base_resume.html.bak"))

    legacy_output = ROOT / "output"
    if legacy_output.exists():
        # job_hunter.log stays shared — it's the LaunchAgent's stdout/stderr
        # target and holds mixed operational history, not personal data.
        moved_output = pdir / "output"
        moved_output.mkdir(exist_ok=True)
        for item in legacy_output.iterdir():
            if item.name == "job_hunter.log":
                continue
            shutil.move(str(item), str(moved_output / item.name))

    # Split .env: Gmail keys move into the profile, everything else
    # (LLM keys, RAPIDAPI_KEY) stays shared at the repo root.
    legacy_env = ROOT / ".env"
    if legacy_env.exists():
        lines = legacy_env.read_text(encoding="utf-8").splitlines()
        profile_lines, shared_lines = [], []
        for ln in lines:
            if any(ln.startswith(f"{k}=") for k in _PROFILE_ENV_KEYS):
                profile_lines.append(ln)
            else:
                shared_lines.append(ln)
        (pdir / ".env").write_text("\n".join(profile_lines) + "\n", encoding="utf-8")
        legacy_env.write_text("\n".join(shared_lines) + "\n", encoding="utf-8")

    return slug
