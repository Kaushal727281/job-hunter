"""
job_scorer.py
=============
AI-powered 5-dimension fit scoring for fetched jobs.

Scoring dimensions (0-100 total):
  Technical Skills    30 pts  — stack overlap between JD and candidate skills
  Experience Match    25 pts  — years / seniority / domain alignment
  Behavioral Fit      15 pts  — team size, leadership, ownership style
  Career Alignment    30 pts  — growth trajectory & role relevance
  Location            gate    — REMOTE or India/Bangalore → pass; otherwise –20 pts

Thresholds:
  75+ → Strong Fit   (auto-apply eligible)
  60–74 → Good Fit   (eligible, lower priority)
  < 60 → Weak Fit    (skip auto-apply)

Previous scale was 1-10; new scale is 1-100 internally but we also store a
normalised 1-10 score (score_10 = round(score_100 / 10)) for backwards compat.
"""

import json
import logging
import re
import time
from bs4 import BeautifulSoup

import profiles

logger = logging.getLogger(__name__)


# ── Dimension weights ─────────────────────────────────────────────────────────
_WEIGHTS = {
    "technical":    30,
    "experience":   25,
    "behavioral":   15,
    "alignment":    30,
}
_LOCATION_PENALTY = 20


def _candidate_profile() -> dict:
    """Extract name, years exp, title, and flat skill list from config + base resume."""
    try:
        cfg = json.loads(profiles.config_path().read_text(encoding="utf-8"))
        candidate = cfg.get("candidate", {})
        name   = candidate.get("name", "The candidate")
        years  = candidate.get("total_experience_years", 0)
        title  = candidate.get("current_title", "Software Engineer")
    except Exception:
        name, years, title = "The candidate", 6, "Lead Software Engineer"

    skills = []
    try:
        soup = BeautifulSoup(profiles.base_resume_path().read_text(encoding="utf-8"), "html.parser")
        for sg in soup.find_all(class_="skill-group"):
            skills += [t.get_text(strip=True) for t in sg.find_all(class_="tag") if t.get_text(strip=True)]
        if not skills:
            el = soup.find(class_="skills-text")
            if el:
                raw = el.get_text(" ", strip=True)
                skills = [s.strip() for s in re.split(r"\s*[·,]\s*", raw) if s.strip()]
    except Exception:
        pass

    return {"name": name, "years": years, "title": title, "skills": skills}


def _is_india_or_remote(job: dict) -> bool:
    """Return True if the job is in India, Bangalore, or is remote."""
    loc = (job.get("location") or "").lower()
    remote = job.get("is_remote", False)
    if remote:
        return True
    india_kws = ("india", "bangalore", "bengaluru", "hyderabad", "pune", "chennai",
                 "mumbai", "delhi", "gurgaon", "noida", "remote", "worldwide")
    return any(k in loc for k in india_kws)


def score_job(job: dict, profile: dict) -> tuple[int, str]:
    """
    Score a single job using 5-dimension framework.
    Returns (score_100, reason_string).
    score_100 is 0-100; caller may normalise to 1-10.
    """
    from llm_client import chat_complete

    skills_str = ", ".join(profile["skills"][:25]) or "Java, Spring Boot, Microservices"
    jd_snippet = (job.get("description") or job.get("title", ""))[:800].replace("\n", " ")
    if not jd_snippet:
        jd_snippet = f"{job.get('title', '')} role"

    location_note = (
        "Location: PASS (India/Remote — no location penalty)"
        if _is_india_or_remote(job)
        else f"Location: FAIL — job is outside India/Remote (apply –{_LOCATION_PENALTY} pts from final score)"
    )

    prompt = (
        "Score this candidate's fit for the job using 4 dimensions (0-100 total).\n\n"
        "Dimensions and max points:\n"
        f"  technical   {_WEIGHTS['technical']} pts — stack overlap between JD required skills and candidate skills\n"
        f"  experience  {_WEIGHTS['experience']} pts — years/seniority/domain match\n"
        f"  behavioral  {_WEIGHTS['behavioral']} pts — team, leadership, ownership style fit\n"
        f"  alignment   {_WEIGHTS['alignment']} pts — career trajectory and role relevance\n\n"
        f"Candidate: {profile['name']}, {profile['years']} yrs exp, "
        f"current title: {profile['title']}\n"
        f"Skills: {skills_str}\n\n"
        f"Job: {job.get('title', '')} at {job.get('company', '')}\n"
        f"JD: {jd_snippet}\n\n"
        f"{location_note}\n\n"
        "Return ONLY JSON — no markdown, no extra text:\n"
        '{"technical":<0-30>,"experience":<0-25>,"behavioral":<0-15>,"alignment":<0-30>,'
        '"reason":"<one concise sentence summarising the match>"}'
    )

    raw = ""
    try:
        raw, _ = chat_complete(prompt, max_tokens=120, temperature=0.2)
        if raw.startswith("```"):
            raw = raw.split("```", 2)[1]
            if raw.startswith("json"):
                raw = raw[4:]
            raw = raw.rsplit("```", 1)[0].strip()
        data = json.loads(raw)

        tech  = max(0, min(_WEIGHTS["technical"],  int(data.get("technical",  0))))
        exp   = max(0, min(_WEIGHTS["experience"],  int(data.get("experience", 0))))
        beh   = max(0, min(_WEIGHTS["behavioral"],  int(data.get("behavioral", 0))))
        align = max(0, min(_WEIGHTS["alignment"],   int(data.get("alignment",  0))))
        total = tech + exp + beh + align

        # Location gate
        if not _is_india_or_remote(job):
            total = max(0, total - _LOCATION_PENALTY)

        reason = str(data.get("reason", "")).strip()[:300]
        breakdown = f"T:{tech} E:{exp} B:{beh} A:{align} = {total}"
        if not _is_india_or_remote(job):
            breakdown += f" (–{_LOCATION_PENALTY} location)"
        full_reason = f"{reason} [{breakdown}]" if reason else breakdown

        return total, full_reason

    except Exception as e:
        logger.warning(
            f"  Score parse failed for {job.get('id')}: {e} "
            f"— raw: {raw[:120] if raw else '?'}"
        )
        return 50, "parse_error"


def score_jobs(job_ids: list[str], status_cb=None):
    """
    Score a list of jobs by ID. Saves fit_score (1-10 compat) + fit_reason to each job.
    Also saves fit_score_100 for the new 0-100 scale.
    status_cb(msg): optional callback to update a status string.
    """
    import job_store
    import os
    from dotenv import load_dotenv
    load_dotenv()

    if not job_ids:
        return

    # Check at least one LLM provider is configured
    has_provider = any([
        os.getenv("OLLAMA_MODEL", "").strip(),
        os.getenv("GROQ_API_KEY", "").strip(),
        os.getenv("DEEPSEEK_API_KEY", "").strip(),
        os.getenv("MISTRAL_API_KEY", "").strip(),
        os.getenv("OPENROUTER_API_KEY", "").strip(),
        os.getenv("GEMINI_API_KEY", "").strip(),
    ])
    if not has_provider:
        logger.info("No LLM provider configured — skipping fit scoring")
        return

    profile = _candidate_profile()
    total   = len(job_ids)
    logger.info(f"Fit-scoring {total} new job(s) (5-dimension framework)…")

    for i, job_id in enumerate(job_ids, 1):
        job = job_store.get_job(job_id)
        if not job:
            continue
        if status_cb:
            status_cb(f"Scoring job fit… {i}/{total}")
        try:
            score_100, reason = score_job(job, profile)
            score_10 = max(1, min(10, round(score_100 / 10)))

            # Determine fit label
            if score_100 >= 75:
                fit_label = "Strong Fit"
            elif score_100 >= 60:
                fit_label = "Good Fit"
            else:
                fit_label = "Weak Fit"

            job_store.update_job(
                job_id,
                fit_score=score_10,          # backwards-compat 1-10
                fit_score_100=score_100,      # new 0-100
                fit_reason=reason,
                fit_label=fit_label,
            )
            logger.info(
                f"  [{i}/{total}] {job.get('title')} @ {job.get('company')} "
                f"→ {score_100}/100 ({fit_label})"
            )
        except Exception as e:
            logger.warning(f"  Failed to score {job_id}: {e}")
        if i < total:
            time.sleep(0.4)

    logger.info(f"Fit-scoring complete for {total} job(s)")
