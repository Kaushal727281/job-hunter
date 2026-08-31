"""
company_research.py
===================
30-day cached per-company research powered by LLM.

Cache location: company_research/<normalized-name>.json
Schema:
  {
    "company":      str,
    "fetched_at":   ISO datetime,
    "mission":      str,    # one-sentence mission/product
    "product":      str,    # what they build / sell
    "tech_stack":   list,   # known technologies
    "culture":      str,    # engineering culture / values highlight
    "recent_news":  str,    # notable recent development (funding, launch, etc.)
    "why_join":     str,    # 1-2 sentences for cover letter para 2
  }

Usage:
    from company_research import get_company_research
    info = get_company_research(job)
    # info["why_join"] → ready to inject into cover letter
"""

from __future__ import annotations
import json
import logging
import re
from datetime import datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

_CACHE_DIR = Path(__file__).parent / "company_research"
_TTL_DAYS  = 30


def _cache_path(company: str) -> Path:
    normalized = re.sub(r"[^a-z0-9]+", "-", company.lower().strip()).strip("-")
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return _CACHE_DIR / f"{normalized}.json"


def _is_fresh(cache_file: Path) -> bool:
    if not cache_file.exists():
        return False
    try:
        data = json.loads(cache_file.read_text())
        fetched = datetime.fromisoformat(data.get("fetched_at", "2000-01-01"))
        return datetime.now() - fetched < timedelta(days=_TTL_DAYS)
    except Exception:
        return False


def _load_cache(cache_file: Path) -> dict | None:
    try:
        return json.loads(cache_file.read_text())
    except Exception:
        return None


def _fetch_from_llm(company: str, job_description: str = "") -> dict:
    """Ask the LLM to research the company. Returns a dict matching the schema above."""
    from llm_client import chat_complete

    jd_snippet = job_description[:600] if job_description else ""
    prompt = f"""You are a company research assistant. Research the company "{company}" and return JSON.

Use whatever you know about this company. If there is a job description provided, use it as a clue.

Job description snippet (for context):
{jd_snippet}

Return ONLY valid JSON with exactly this structure — no markdown, no extra keys:
{{
  "mission": "<one sentence: what the company does / its core mission>",
  "product": "<what they build or sell, in plain English>",
  "tech_stack": ["tech1", "tech2", "tech3"],
  "culture": "<engineering culture or values — 1 sentence>",
  "recent_news": "<notable recent development (funding round, product launch, acquisition) — 1 sentence, or empty string if unknown>",
  "why_join": "<1-2 compelling sentences a candidate would say in a cover letter about WHY they want to join this specific company — concrete, not generic>"
}}

Rules:
- Keep every field concise (1-2 sentences max, except tech_stack list)
- tech_stack: list up to 8 known technologies (Java, Python, Kafka, Kubernetes, etc.)
- why_join MUST be specific to this company — never write generic statements like "I am excited about your innovative work"
- If you don't know something, give your best estimate based on the company name and JD
- Return ONLY the JSON object, nothing else"""

    try:
        raw, _ = chat_complete(prompt, max_tokens=600, temperature=0.3)
        # Strip markdown if any
        if raw.startswith("```"):
            raw = raw.split("```", 2)[1]
            if raw.startswith("json"):
                raw = raw[4:]
            raw = raw.rsplit("```", 1)[0].strip()
        data = json.loads(raw)
        # Validate required keys
        for k in ("mission", "product", "tech_stack", "culture", "recent_news", "why_join"):
            if k not in data:
                data[k] = "" if k != "tech_stack" else []
        return data
    except Exception as e:
        logger.warning(f"  Company research LLM call failed for '{company}': {e}")
        return {
            "mission": "", "product": "", "tech_stack": [],
            "culture": "", "recent_news": "", "why_join": "",
        }


def get_company_research(job: dict, force_refresh: bool = False) -> dict:
    """
    Return cached (or freshly fetched) company research for the given job.
    Cache TTL: 30 days per company.

    Returns the research dict (never raises — falls back to empty dict).
    """
    company = (job.get("company") or "").strip()
    if not company:
        return {}

    cache_file = _cache_path(company)

    if not force_refresh and _is_fresh(cache_file):
        data = _load_cache(cache_file)
        if data:
            logger.info(f"  Company research: using cached data for '{company}'")
            return data

    logger.info(f"  Company research: fetching for '{company}'…")
    try:
        info = _fetch_from_llm(company, job.get("description", ""))
        record = {
            "company":    company,
            "fetched_at": datetime.now().isoformat(timespec="seconds"),
            **info,
        }
        cache_file.write_text(json.dumps(record, indent=2, ensure_ascii=False))
        logger.info(f"  Company research cached → {cache_file.name}")
        return record
    except Exception as e:
        logger.warning(f"  Company research failed for '{company}': {e}")
        return {}
