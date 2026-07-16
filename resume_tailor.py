"""
resume_tailor.py
Uses Groq API (Llama 3.3 70B) to tailor the base resume for a specific job.

Approach: extract only the text sections that need rewriting (summary + bullet points),
send those to Groq (~2KB), then inject the changes back into the full HTML.
This keeps tokens well under Groq's free-tier limit (12K TPM).
"""

import os
import re
import json
import logging
import truststore
from pathlib import Path
from bs4 import BeautifulSoup
from groq import Groq
from dotenv import load_dotenv

truststore.inject_into_ssl()
load_dotenv()
logger = logging.getLogger(__name__)

BASE_RESUME_PATH  = Path(__file__).parent / "base_resume.html"
CONFIG_FILE       = Path(__file__).parent / "config.json"
MODEL = "llama-3.3-70b-versatile"


def _candidate_name() -> str:
    try:
        return json.loads(CONFIG_FILE.read_text()).get("candidate", {}).get("name", "The candidate")
    except Exception:
        return "The candidate"


def _get_client() -> Groq:
    api_key = os.getenv("GROQ_API_KEY")
    if not api_key:
        raise EnvironmentError("GROQ_API_KEY not set in .env")
    return Groq(api_key=api_key)


def _extract_sections(soup: BeautifulSoup) -> dict:
    """Pull out the text sections we want Groq to rewrite."""
    # Summary
    summary_el = soup.find(class_="summary-text")
    summary = summary_el.get_text(" ", strip=True) if summary_el else ""

    # Skills
    skills_el = soup.find(class_="skills-text")
    skills = skills_el.get_text(" ", strip=True) if skills_el else ""

    # Experience bullets — collect all <li> text from each job block
    jobs_data = []
    for job_div in soup.find_all(class_="job"):
        title_el   = job_div.find(class_="job-title")
        company_el = job_div.find(class_="job-company")
        bullets = [li.get_text(" ", strip=True) for li in job_div.find_all("li")]
        if bullets:
            jobs_data.append({
                "title":   title_el.get_text(strip=True) if title_el else "",
                "company": company_el.get_text(strip=True) if company_el else "",
                "bullets": bullets,
            })

    return {"summary": summary, "skills": skills, "jobs": jobs_data}


def _apply_sections(soup: BeautifulSoup, modified: dict) -> BeautifulSoup:
    """Inject Groq's modified text back into the HTML."""
    # Summary
    new_summary = modified.get("summary", "")
    if new_summary:
        summary_el = soup.find(class_="summary-text")
        if summary_el:
            summary_el.clear()
            summary_el.append(new_summary)

    # Skills — reordered/supplemented for ATS
    new_skills = modified.get("skills", "")
    if new_skills:
        skills_el = soup.find(class_="skills-text")
        if skills_el:
            skills_el.clear()
            skills_el.append(new_skills)

    # Experience bullets
    mod_jobs = modified.get("jobs", [])
    job_divs = soup.find_all(class_="job")
    for i, job_div in enumerate(job_divs):
        if i >= len(mod_jobs):
            break
        new_bullets = mod_jobs[i].get("bullets", [])
        ul = job_div.find("ul")
        if ul and new_bullets:
            ul.clear()
            for b in new_bullets:
                li = soup.new_tag("li")
                li.string = b
                ul.append(li)

    return soup


def tailor_resume(job: dict) -> dict:
    """
    Tailors the base resume for the given job.
    Returns:
      {
        "resume_html": str,   — full modified HTML (with photo intact)
        "cover_note": str,    — 3-sentence cover note
        "match_score": int,   — 1-10 relevance score
        "key_matches": list   — top 5 matching skills/keywords
      }
    """
    base_html = BASE_RESUME_PATH.read_text(encoding="utf-8")
    soup = BeautifulSoup(base_html, "html.parser")
    sections = _extract_sections(soup)

    # Build a compact text-only representation for Groq (~2KB)
    resume_text = f"SUMMARY:\n{sections['summary']}\n\n"
    resume_text += f"SKILLS:\n{sections['skills']}\n\n"
    for j in sections["jobs"]:
        resume_text += f"ROLE: {j['title']} @ {j['company']}\n"
        for b in j["bullets"]:
            resume_text += f"  • {b}\n"
        resume_text += "\n"

    client = _get_client()

    candidate_name = _candidate_name()
    prompt = f"""You are a professional resume writer helping {candidate_name} tailor their resume for a specific job. Your goal is to maximise ATS (Applicant Tracking System) score while keeping the resume 100% truthful.

## Target Job
Title: {job['title']}
Company: {job['company']}
Location: {job['location']} {'(Remote)' if job.get('is_remote') else ''}
Salary: {job.get('salary', 'Not disclosed')}

## Job Description
{job.get('description', '')[:2000]}

## Current Resume Text
{resume_text}

## Instructions
1. Rewrite the SUMMARY to directly address this role (2-3 sentences, mention the company name, mirror key phrases from the JD).
2. Rewrite the SKILLS line: keep all existing skills, put the ones most relevant to this JD first, and add any missing JD keywords that {candidate_name} genuinely has. Keep the "·" separator format.
3. For each role, reorder the bullets to put the most relevant ones first. You may lightly rephrase to match JD language (never fabricate new facts or add experience not present).
4. Return a JSON object with this exact structure — mirror the same roles and bullet counts:

{{
  "summary": "<new summary text>",
  "skills": "<reordered · separated skills string>",
  "jobs": [
    {{"title": "<role>", "company": "<co>", "bullets": ["bullet1", "bullet2", ...]}},
    ...
  ],
  "cover_note": "<3 sentences why {candidate_name} is a great fit for this specific role at this company>",
  "match_score": <integer 1-10>,
  "key_matches": ["skill1", "skill2", "skill3", "skill4", "skill5"]
}}

Return ONLY valid JSON, no markdown fences, no extra text."""

    logger.info(f"Tailoring resume for: {job['title']} at {job['company']} "
                f"(prompt ~{len(prompt)//4} tokens)")

    response = client.chat.completions.create(
        model=MODEL,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=4096,
        temperature=0.3,
    )

    raw = response.choices[0].message.content.strip()

    # Strip markdown fences if added
    if raw.startswith("```"):
        raw = raw.split("```", 2)[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.rsplit("```", 1)[0].strip()

    result = json.loads(raw)

    for key in ("summary", "skills", "jobs", "cover_note", "match_score", "key_matches"):
        if key not in result:
            if key == "skills":
                result["skills"] = sections["skills"]   # fallback: keep original
            else:
                raise ValueError(f"Groq response missing key: {key}")

    # Inject modified text back into the full HTML
    soup = _apply_sections(soup, result)
    result["resume_html"] = str(soup)

    # Remove intermediate keys not needed by callers
    del result["summary"]
    del result["skills"]
    del result["jobs"]

    logger.info(
        f"  Match score: {result['match_score']}/10 | "
        f"Key matches: {', '.join(result['key_matches'])}"
    )
    return result
