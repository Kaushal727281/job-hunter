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
import time
import logging
import truststore
from pathlib import Path
from bs4 import BeautifulSoup, NavigableString
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
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8")).get("candidate", {}).get("name", "The candidate")
    except Exception:
        return "The candidate"


def _get_client() -> Groq:
    api_key = os.getenv("GROQ_API_KEY")
    if not api_key:
        raise EnvironmentError("GROQ_API_KEY not set in .env")
    return Groq(api_key=api_key)


def _extract_sections(soup: BeautifulSoup) -> dict:
    """Pull out the text sections we want Groq to rewrite."""
    def _txt(el): return el.get_text(" ", strip=True) if el else ""

    # Summary
    summary = _txt(soup.find(class_="summary-text"))

    # Skills — new structure: grouped .skill-group divs with .tag chips
    skill_groups = []
    all_skill_tags = []
    for sg in soup.find_all(class_="skill-group"):
        label = _txt(sg.find(class_="skill-group-label"))
        tags  = [_txt(t) for t in sg.find_all(class_="tag") if _txt(t)]
        if tags:
            skill_groups.append({"label": label, "tags": tags})
            all_skill_tags.extend(tags)

    # Fallback: old .skills-text single string
    skills_flat = ""
    if not all_skill_tags:
        skills_el = soup.find(class_="skills-text")
        if skills_el:
            skills_flat = _txt(skills_el)

    # Experience bullets — collect all <li> text from each .job block
    jobs_data = []
    for job_div in soup.find_all(class_="job"):
        title_el   = job_div.find(class_="job-title")
        company_el = job_div.find(class_="job-company")
        bullets    = [li.get_text(" ", strip=True) for li in job_div.find_all("li")]
        if bullets:
            jobs_data.append({
                "title":   _txt(title_el),
                "company": _txt(company_el),
                "bullets": bullets,
            })

    return {
        "summary":      summary,
        "skill_groups": skill_groups,
        "skills_flat":  skills_flat,
        "jobs":         jobs_data,
    }


def _apply_sections(soup: BeautifulSoup, modified: dict) -> BeautifulSoup:
    """Inject Groq's modified text back into the HTML."""
    # Summary
    new_summary = modified.get("summary", "")
    if new_summary:
        summary_el = soup.find(class_="summary-text")
        if summary_el:
            summary_el.clear()
            summary_el.append(new_summary)

    # New ATS keywords → silently append into the last existing .skill-group's .skill-tags
    new_kw = modified.get("new_ats_keywords", [])
    if new_kw:
        sidebar = soup.find(class_="sidebar")
        if sidebar:
            # Don't add duplicates — filter out keywords already in any .tag
            existing = {t.get_text(strip=True).lower()
                        for t in sidebar.find_all(class_="tag")}
            fresh = [k for k in new_kw if k.lower() not in existing]
            if fresh:
                # Find the last .skill-group and append to its .skill-tags
                skill_groups = sidebar.find_all(class_="skill-group")
                if skill_groups:
                    last_sg = skill_groups[-1]
                    tags_div = last_sg.find(class_="skill-tags")
                    if not tags_div:
                        tags_div = soup.new_tag("div", **{"class": "skill-tags"})
                        last_sg.append(tags_div)
                    for kw in fresh:
                        span = soup.new_tag("span", **{"class": "tag"})
                        span.string = kw
                        tags_div.append(span)

    # Old .skills-text fallback
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


def _bold_keywords(soup: BeautifulSoup, keywords: list) -> BeautifulSoup:
    """
    Wrap every occurrence of each keyword (case-insensitive) in <strong> tags
    inside text content areas only (skips style/script/title/existing strong).
    """
    if not keywords:
        return soup

    # Sort longest first so multi-word phrases match before their sub-words
    kw_sorted = sorted(set(keywords), key=len, reverse=True)
    pattern   = re.compile(
        "(" + "|".join(re.escape(k) for k in kw_sorted) + ")",
        re.IGNORECASE,
    )

    _SKIP = {"style", "script", "title", "strong", "b", "head", "a"}

    for node in soup.find_all(string=True):
        parent = node.parent
        # Skip if inside a tag we don't want to touch
        if any(p.name in _SKIP for p in [parent] + list(parent.parents)):
            continue
        text = str(node)
        if not pattern.search(text):
            continue

        parts = pattern.split(text)
        if len(parts) == 1:
            continue

        # Replace the NavigableString with mixed text + <strong> nodes
        for part in parts:
            if not part:
                continue
            if pattern.fullmatch(part):
                strong = soup.new_tag("strong")
                strong.string = part
                node.insert_before(strong)
            else:
                node.insert_before(NavigableString(part))
        node.extract()

    return soup


def tailor_resume(job: dict) -> dict:
    """
    Tailors the base resume for the given job.
    Returns:
      {
        "resume_html":      str,   — full modified HTML with bolded keywords
        "cover_note":       str,   — 3-sentence cover note
        "match_score":      int,   — 1-10 relevance score
        "key_matches":      list   — top matching skills/keywords
        "bold_keywords":    list   — keywords bolded in the resume HTML
        "new_ats_keywords": list   — new keywords added from JD
      }
    """
    base_html = BASE_RESUME_PATH.read_text(encoding="utf-8")
    soup = BeautifulSoup(base_html, "html.parser")
    sections = _extract_sections(soup)

    # Build compact text representation for Groq
    resume_text = f"SUMMARY:\n{sections['summary']}\n\n"

    if sections["skill_groups"]:
        resume_text += "SKILLS (by category):\n"
        for sg in sections["skill_groups"]:
            resume_text += f"  {sg['label']}: {', '.join(sg['tags'])}\n"
        resume_text += "\n"
    elif sections["skills_flat"]:
        resume_text += f"SKILLS:\n{sections['skills_flat']}\n\n"

    for j in sections["jobs"]:
        resume_text += f"ROLE: {j['title']} @ {j['company']}\n"
        for b in j["bullets"]:
            resume_text += f"  • {b}\n"
        resume_text += "\n"

    client = _get_client()
    candidate_name = _candidate_name()

    prompt = f"""You are an expert ATS resume optimizer helping {candidate_name} tailor their resume for a specific job. Your TWO goals:
1. Maximize ATS keyword match score by weaving JD keywords naturally into the resume.
2. Keep every claim 100% truthful — never fabricate roles, companies, or technologies not present.

## Target Job
Title: {job['title']}
Company: {job['company']}
Location: {job['location']} {'(Remote)' if job.get('is_remote') else ''}

## Job Description
{job.get('description', '')[:2500]}

## Candidate's Current Resume
{resume_text}

## Instructions
1. **SUMMARY**: Rewrite as a candidate APPLYING for this role. STRICT RULES:
   - NEVER use the phrase "as a [job title] at [company]" — this implies already employed there
   - NEVER say "at [company]" or "for [company]" at the end of the sentence
   - DO start with: "[X]+ years of experience in [core skills]..."
   - DO end with what value the candidate brings, NOT where they are going
   - BAD example: "...seeking a role as Full Stack Engineer at Deutsche Bank"
   - GOOD example: "...bringing 7 years of enterprise Java expertise and a proven record of delivering scalable solutions."

2. **NEW_ATS_KEYWORDS**: List up to 15 keywords/phrases from the JD that are NOT already in the candidate's skills section. Be INCLUSIVE for ATS coverage — apply these rules:
   - If the JD names a tool/framework in the same family as one the candidate uses, include it (e.g. JD says "IBM WebSphere MQ" → candidate has Kafka → include "IBM WebSphere MQ")
   - If the JD names a methodology/concept the candidate would routinely apply (SDLC, Agile, CI/CD, unit testing, SQL), include it even if not spelled out in the resume
   - If the JD names a testing framework (JUnit, SpecFlow, Karate, Mockito) and candidate does Java/backend development, include it
   - Include domain terms (RESTful Web Services, NoSQL, Generative AI, microservices) that apply to the candidate's stack
   - NEVER invent specialised products (e.g. a specific cloud product) the candidate has no exposure to
   Return as a JSON array of short strings (tool names, frameworks, buzzwords — not full sentences).

3. **JOBS – bullets**: For each role rewrite bullets to:
   - Put the most JD-relevant bullets first
   - Weave in JD terminology naturally (e.g. replace "REST services" with "RESTful microservices" if JD uses that phrase)
   - Never add technologies or responsibilities not actually present

4. **BOLD_KEYWORDS**: Return up to 15 keywords/short phrases that appear in BOTH the JD and the tailored resume — these will be bolded in the final PDF so recruiters see instant matches. Pick the most impactful technical terms and action phrases.

5. **COVER_LETTER**: Write a full professional cover letter (~200 words, 4 paragraphs):
   - Para 1: Enthusiastic opening — name the specific role + company, hook with a key strength.
   - Para 2: Why THIS company specifically — research-based reason (product, mission, tech stack).
   - Para 3: Your top 2-3 relevant achievements from the resume that directly match the JD.
   - Para 4: Confident closing — express availability, invite for interview, professional sign-off.
   Address it to "Hiring Manager" at {job['company']}. Do NOT include a date or address block — just the letter body paragraphs separated by blank lines.

6. **COVER_NOTE**: 1-2 sentence teaser (for dashboard preview) summarising why this is a strong match.

Return ONLY valid JSON with this exact structure:
{{
  "summary": "<new summary>",
  "new_ats_keywords": ["keyword1", "keyword2"],
  "jobs": [
    {{"title": "<role>", "company": "<co>", "bullets": ["bullet1", "bullet2", ...]}}
  ],
  "cover_letter": "<full 4-paragraph cover letter body>",
  "cover_note": "<1-2 sentence teaser>",
  "match_score": <1-10>,
  "key_matches": ["skill1", "skill2", "skill3", "skill4", "skill5"],
  "bold_keywords": ["Spring Boot", "microservices", "Java 8", "REST APIs"]
}}

Return ONLY valid JSON, no markdown fences, no extra text."""

    logger.info(f"Tailoring resume for: {job['title']} at {job['company']} "
                f"(prompt ~{len(prompt)//4} tokens)")

    # Retry up to 2 times on transient 429 rate-limit (short wait only; daily limit won't recover)
    last_exc = None
    for attempt in range(3):
        try:
            response = client.chat.completions.create(
                model=MODEL,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=6000,
                temperature=0.5,
            )
            break
        except Exception as e:
            last_exc = e
            err_str = str(e)
            # If daily token limit exceeded, no point retrying
            if "tokens per day" in err_str or "TPD" in err_str:
                raise
            if "429" in err_str and attempt < 2:
                wait = 5 * (attempt + 1)
                logger.warning(f"Groq 429 on attempt {attempt+1}, retrying in {wait}s…")
                time.sleep(wait)
            else:
                raise
    else:
        raise last_exc  # all retries exhausted

    raw = response.choices[0].message.content.strip()
    logger.info(f"  Groq raw response length: {len(raw)} chars, "
                f"finish_reason: {response.choices[0].finish_reason}")

    # Strip markdown fences if Groq adds them despite instructions
    if raw.startswith("```"):
        raw = raw.split("```", 2)[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.rsplit("```", 1)[0].strip()

    # Sanitize: replace literal control chars inside JSON string values
    # (Groq sometimes embeds literal newlines in multi-line cover letters)
    def _fix_control_chars(s: str) -> str:
        out, in_str, esc = [], False, False
        for ch in s:
            if esc:
                out.append(ch); esc = False
            elif ch == '\\':
                out.append(ch); esc = True
            elif ch == '"':
                out.append(ch); in_str = not in_str
            elif in_str and ch == '\n':
                out.append('\\n')
            elif in_str and ch == '\r':
                out.append('\\r')
            elif in_str and ch == '\t':
                out.append('\\t')
            elif in_str and ord(ch) < 0x20:
                pass  # drop other control chars inside strings
            else:
                out.append(ch)
        return ''.join(out)

    raw = _fix_control_chars(raw)

    try:
        result = json.loads(raw)
    except json.JSONDecodeError as e:
        logger.error(f"  JSON parse error: {e} — raw tail: {raw[-200:]}")
        raise

    # Validate required keys
    for key in ("summary", "jobs", "match_score", "key_matches"):
        if key not in result:
            raise ValueError(f"Groq response missing key: {key}")
    result.setdefault("cover_letter", result.get("cover_note", ""))
    result.setdefault("cover_note", "")

    # Post-process summary — strip "as a [title] at [company]" phrases the model keeps adding
    company_raw = job.get("company", "").strip()
    summary = result.get("summary", "")
    if company_raw and summary:
        ce = re.escape(company_raw)
        # Pattern A: exact company name (anywhere — not just at end)
        summary = re.sub(
            r",?\s+as\s+(?:an?\s+)?.+?\s+at\s+" + ce + r"[.,]?",
            ".", summary, flags=re.IGNORECASE,
        ).strip()
        # Pattern B: bare "at Company" at end
        summary = re.sub(
            r",?\s+at\s+" + ce + r"[.,]?\s*$",
            ".", summary, flags=re.IGNORECASE,
        ).strip()
        # Pattern C: first significant word of company
        # Catches mismatches like stored "Deutsche Bank AG" vs text "Deutsche Bank"
        sig_words = [w for w in re.sub(r"[^a-zA-Z0-9 ]", "", company_raw).split()
                     if len(w) > 3 and w.lower() not in {"the", "and", "pvt", "ltd", "inc", "corp"}]
        if sig_words:
            cw = re.escape(sig_words[0])
            summary = re.sub(
                r",?\s+as\s+(?:an?\s+)?.+?\s+at\s+" + cw + r"\b[^.!?]*[.,]?",
                ".", summary, flags=re.IGNORECASE,
            ).strip()
        # Pattern D: company-agnostic safety net
        # Matches "as a Title at Company." where the phrase ends with a period
        # (followed by a new capital sentence or end of string — not a comma)
        summary = re.sub(
            r",?\s+as\s+(?:an?\s+)?[A-Z][\w\s,/]*?\s+at\s+[A-Z]\w+(?:\s+[A-Z]\w+)*[.,]?"
            r"(?=\s+[A-Z]|\s*$)",
            ".", summary,
        ).strip()
    result["summary"] = summary

    result.setdefault("new_ats_keywords", [])
    result.setdefault("bold_keywords", result.get("key_matches", []))

    # 1. Inject modified text back into the full HTML
    soup = _apply_sections(soup, result)

    # 2. Bold every keyword that appears in both JD and the tailored resume
    soup = _bold_keywords(soup, result["bold_keywords"])

    result["resume_html"] = str(soup)

    # Clean up intermediate keys callers don't need
    del result["summary"]
    del result["jobs"]

    logger.info(
        f"  Match score: {result['match_score']}/10 | "
        f"Key matches: {', '.join(result['key_matches'])} | "
        f"New ATS keywords: {result.get('new_ats_keywords', [])} | "
        f"Bolded: {result.get('bold_keywords', [])}"
    )
    return result
