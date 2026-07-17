"""
job_scorer.py
Quick AI-powered fit scoring for fetched jobs using the local Ollama model.

Scores each job 1-10 based on candidate skills vs JD — runs after fetch,
before tailoring. Uses a tiny prompt (max_tokens=150) so it's fast.
"""

import json
import logging
import re
import time
from pathlib import Path
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

CONFIG_FILE      = Path(__file__).parent / "config.json"
BASE_RESUME_PATH = Path(__file__).parent / "base_resume.html"


def _candidate_profile() -> dict:
    """Extract name, years exp, and flat skill list from config + base resume."""
    try:
        cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        candidate = cfg.get("candidate", {})
        name  = candidate.get("name", "The candidate")
        years = candidate.get("total_experience_years", 0)
    except Exception:
        name, years = "The candidate", 0

    skills = []
    try:
        soup = BeautifulSoup(BASE_RESUME_PATH.read_text(encoding="utf-8"), "html.parser")
        for sg in soup.find_all(class_="skill-group"):
            skills += [t.get_text(strip=True) for t in sg.find_all(class_="tag") if t.get_text(strip=True)]
        if not skills:
            el = soup.find(class_="skills-text")
            if el:
                raw = el.get_text(" ", strip=True)
                skills = [s.strip() for s in re.split(r"\s*[·,]\s*", raw) if s.strip()]
    except Exception:
        pass

    return {"name": name, "years": years, "skills": skills}


def score_job(job: dict, profile: dict) -> tuple[int, str]:
    """
    Score a single job 1-10. Returns (score, reason).
    Uses a short prompt — fast, cheap on resources.
    """
    from llm_client import chat_complete

    skills_str = ", ".join(profile["skills"][:20]) or "Java, Spring Boot"
    jd_snippet = (job.get("description") or job.get("title", ""))[:400].replace("\n", " ")
    if not jd_snippet:
        jd_snippet = f"{job.get('title', '')} role"

    prompt = (
        f"Rate how well this candidate fits the job. Return ONLY JSON: "
        f'{"{"}"score": <1-10>, "reason": "<one sentence>"{"}"}\n\n'
        f"Candidate: {profile['years']} years exp, skills: {skills_str}\n"
        f"Job: {job.get('title', '')} at {job.get('company', '')} — {jd_snippet}"
    )

    try:
        raw, _ = chat_complete(prompt, max_tokens=80, temperature=0.2)
        # Strip markdown fences if present
        if raw.startswith("```"):
            raw = raw.split("```", 2)[1]
            if raw.startswith("json"):
                raw = raw[4:]
            raw = raw.rsplit("```", 1)[0].strip()
        data = json.loads(raw)
        score  = max(1, min(10, int(data.get("score", 5))))
        reason = str(data.get("reason", "")).strip()[:200]
        return score, reason
    except Exception as e:
        logger.warning(f"  Score parse failed for {job.get('id')}: {e} — raw: {raw[:100] if 'raw' in dir() else '?'}")
        return 5, ""


def score_jobs(job_ids: list[str], status_cb=None):
    """
    Score a list of jobs by ID. Saves fit_score + fit_reason to each job.
    status_cb(msg): optional callback to update a status string.
    """
    import job_store

    if not job_ids:
        return

    # Only score if Ollama is configured
    import os
    from dotenv import load_dotenv
    load_dotenv()
    if not os.getenv("OLLAMA_MODEL", "").strip():
        logger.info("OLLAMA_MODEL not set — skipping fit scoring")
        return

    profile = _candidate_profile()
    total   = len(job_ids)
    logger.info(f"Fit-scoring {total} new job(s) with Ollama…")

    for i, job_id in enumerate(job_ids, 1):
        job = job_store.get_job(job_id)
        if not job:
            continue
        if status_cb:
            status_cb(f"Scoring job fit… {i}/{total}")
        try:
            score, reason = score_job(job, profile)
            job_store.update_job(job_id, fit_score=score, fit_reason=reason)
            logger.info(f"  [{i}/{total}] {job.get('title')} @ {job.get('company')} → {score}/10")
        except Exception as e:
            logger.warning(f"  Failed to score {job_id}: {e}")
        # Small pause between calls to avoid hammering Ollama
        if i < total:
            time.sleep(0.5)

    logger.info(f"Fit-scoring complete for {total} job(s)")
