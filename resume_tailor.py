"""
resume_tailor.py
Uses LLM (Groq → Gemini fallback) to tailor the base resume for a specific job.

Key rotation: set GROQ_API_KEY, GROQ_API_KEY_2, GROQ_API_KEY_3 … in .env.
When a key hits its daily limit the next one is used automatically.
Set GEMINI_API_KEY for a final fallback (1M tokens/day free).
"""

import re
import json
import logging
import truststore
from pathlib import Path
from bs4 import BeautifulSoup, NavigableString
from dotenv import load_dotenv

truststore.inject_into_ssl()
load_dotenv()
logger = logging.getLogger(__name__)

BASE_RESUME_PATH = Path(__file__).parent / "base_resume.html"
CONFIG_FILE      = Path(__file__).parent / "config.json"


def _candidate_name() -> str:
    try:
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8")).get("candidate", {}).get("name", "The candidate")
    except Exception:
        return "The candidate"


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

    # New ATS keywords — do NOT inject as a visible sidebar section.
    # Showing aspirational keywords (AWS, Kubernetes, etc.) that the candidate
    # doesn't actually have experience with looks dishonest to human reviewers.
    # Instead they are only used for ATS bolding in the text body (bold_keywords).

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
    Wrap the FIRST occurrence of each keyword (case-insensitive) in a
    <strong class="ats-kw"> tag. Each keyword is bolded exactly once.
    Skips style/script/title/existing strong/mark/head/a tags.
    """
    if not keywords:
        return soup

    clean = [k for k in keywords if k and k.strip()]
    if not clean:
        return soup

    # Track which keywords have already been bolded (normalised to lowercase)
    bolded: set[str] = set()

    # Sort longest first so multi-word phrases match before their sub-words
    kw_sorted = sorted(set(clean), key=len, reverse=True)
    _SKIP = {"style", "script", "title", "strong", "b", "mark", "head", "a"}

    for node in soup.find_all(string=True):
        parent = node.parent
        if any(p.name in _SKIP for p in [parent] + list(parent.parents)):
            continue

        # Build pattern from keywords not yet bolded
        remaining = [k for k in kw_sorted if k.lower() not in bolded]
        if not remaining:
            break

        pattern = re.compile(
            "(" + "|".join(re.escape(k) for k in remaining) + ")",
            re.IGNORECASE,
        )

        text = str(node)
        if not pattern.search(text):
            continue

        parts = pattern.split(text)
        if len(parts) == 1:
            continue

        for part in parts:
            if not part:
                continue
            if pattern.fullmatch(part) and part.lower() not in bolded:
                tag = soup.new_tag("strong", **{"class": "ats-kw"})
                tag.string = part
                node.insert_before(tag)
                bolded.add(part.lower())   # mark as done — no more bolding for this keyword
            else:
                node.insert_before(NavigableString(part))
        node.extract()

    return soup


def _detect_domain(job: dict) -> tuple[str, str]:
    """
    Detect the target company's industry domain.
    Returns (domain_label, emphasis_hint) to inject into the prompt.
    """
    company = (job.get("company") or "").lower()
    desc    = (job.get("description") or "").lower()[:1500]
    combined = company + " " + desc

    _INSURANCE = ("insurance", "insurer", "underwriting", "claims", "actuarial",
                  "allianz", "axa", "zurich", "prudential", "aviva", "aig", "cigna",
                  "metlife", "manulife", "liberty mutual", "chubb", "berkshire",
                  "policy", "premium", "reinsurance", "lloyds")
    _BANKING   = ("bank", "banking", "jpmorgan", "goldman", "barclays", "bnp",
                  "hsbc", "citi", "wells fargo", "morgan stanley", "deutsche",
                  "credit suisse", "nomura", "ubs", "fidelity", "blackrock",
                  "payments", "ach", "wire transfer", "swift", "clearing", "settlement",
                  "visa", "mastercard", "fintech", "neobank", "remittance")
    _HEALTHCARE= ("health", "healthcare", "hospital", "pharma", "pharmaceutical",
                  "clinical", "medical", "ehr", "fhir", "hl7", "optum", "epic",
                  "patient", "doctor", "diagnosis", "lab", "imaging")
    _ECOMMERCE = ("ecommerce", "e-commerce", "retail", "marketplace", "shopify",
                  "amazon", "flipkart", "meesho", "logistics", "supply chain",
                  "inventory", "warehouse", "fulfilment", "catalog", "cart")
    _TELECOM   = ("telecom", "telco", "telecomm", "network", "5g", "vodafone",
                  "airtel", "jio", "att", "verizon", "t-mobile", "ericsson", "nokia")
    _FAANG     = ("google", "meta", "apple", "microsoft", "netflix", "uber",
                  "airbnb", "stripe", "atlassian", "salesforce", "twilio", "datadog",
                  "snowflake", "confluent", "hashicorp", "gitlab")

    if any(k in combined for k in _INSURANCE):
        return (
            "Insurance",
            "IMPORTANT: This is an INSURANCE company. The candidate's FICO platform "
            "is used by global insurers for underwriting automation, claims processing, "
            "and risk scoring. Emphasize these insurance use-cases in the summary and bullets. "
            "Use terms like 'underwriting', 'claims', 'risk decisioning', 'insurance automation'."
        )
    if any(k in combined for k in _BANKING):
        return (
            "Banking / Fintech",
            "IMPORTANT: This is a BANKING or FINTECH company. Emphasize the candidate's "
            "FICO platform serving banks for ACH origination, credit scoring, fraud detection, "
            "and loan decisioning. Use terms like 'payments', 'financial decisioning', "
            "'transaction processing', 'regulatory compliance'."
        )
    if any(k in combined for k in _HEALTHCARE):
        return (
            "Healthcare",
            "IMPORTANT: This is a HEALTHCARE company. Emphasize the candidate's experience "
            "with high-volume, high-reliability enterprise systems, data security (Spring Security), "
            "and rules-driven automated workflows — paralleling clinical decision support."
        )
    if any(k in combined for k in _ECOMMERCE):
        return (
            "E-commerce / Retail",
            "IMPORTANT: This is an E-COMMERCE or RETAIL company. Emphasize high-throughput "
            "API design, scalability, event-driven architecture (Kafka), and the TiffinLane "
            "marketplace project as direct e-commerce experience."
        )
    if any(k in combined for k in _TELECOM):
        return (
            "Telecom",
            "IMPORTANT: This is a TELECOM company. Emphasize high-availability distributed "
            "systems, event-driven Kafka pipelines, REST APIs at scale, and microservices architecture."
        )
    if any(k in combined for k in _FAANG):
        return (
            "Tech Product (FAANG-style)",
            "IMPORTANT: This is a top-tier TECH PRODUCT company. Emphasize engineering depth: "
            "system design, performance tuning, scalable architecture, the Job Hunter AI project, "
            "and clean engineering practices. Avoid domain-specific jargon."
        )
    return (
        "Tech / Enterprise",
        "Emphasize enterprise-grade Java engineering, scalability, technical leadership, "
        "and cross-domain applicability of the FICO platform."
    )


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

    candidate_name = _candidate_name()
    domain_label, domain_hint = _detect_domain(job)
    logger.info(f"  Detected domain: {domain_label}")

    prompt = f"""You are an expert ATS resume optimizer helping {candidate_name} tailor their resume for a specific job. Your TWO goals:
1. Maximize ATS keyword match score by weaving JD keywords naturally into the resume.
2. Keep every claim 100% truthful — never fabricate roles, companies, or technologies not present.

## Target Job
Title: {job['title']}
Company: {job['company']} [{domain_label}]
Location: {job['location']} {'(Remote)' if job.get('is_remote') else ''}

## Company Domain Context
{domain_hint}

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

2. **BOLD_KEYWORDS** — skill/keyword matching (CRITICAL):
   Step A — Compare the JD skills/technologies to the candidate's resume. List every skill word that appears in BOTH the JD and the resume (exact or close match, e.g. "REST APIs" matches "RESTful APIs").
   Step B — Count the matches from Step A. If the count is LESS THAN 5, identify additional skill/technology words from the JD that are contextually aligned with what the candidate already does (e.g. JD mentions "Kafka" and candidate works on microservices → add "Kafka"; JD mentions "Agile" → add "Agile"). Add enough to reach at least 5 total.
   Step C — Return all these words (Step A matches + Step B additions) as the "bold_keywords" array. These will be bolded in the PDF.
   RULES: Return plain strings — NO asterisks, NO markdown, NO ** wrapping. Example: "Spring Boot" not "**Spring Boot**".

3. **NEW_ATS_KEYWORDS**: From the Step B additions above that are NOT already in the candidate's skills section, return them here too. These will be injected into the resume skills section.
   - Only include terms contextually aligned with the candidate's background
   - NEVER invent specific products the candidate has no exposure to
   Return as a JSON array of short plain strings.

4. **JOBS – bullets**: For each role rewrite bullets to:
   - Put the most JD-relevant bullets first
   - Weave in JD terminology naturally (e.g. replace "REST services" with "RESTful microservices" if JD uses that phrase)
   - Never add technologies or responsibilities not actually present

5. **COVER_LETTER**: Write a full professional cover letter (~200 words, 4 paragraphs):
   - Para 1: Enthusiastic opening — name the specific role + company, hook with a key strength.
   - Para 2: Why THIS company specifically — research-based reason (product, mission, tech stack).
   - Para 3: Your top 2-3 relevant achievements from the resume that directly match the JD.
   - Para 4: Confident closing — express availability, invite for interview, professional sign-off.
   Address it to "Hiring Manager" at {job['company']}. Do NOT include a date or address block — just the letter body paragraphs separated by blank lines.

6. **COVER_NOTE**: 1-2 sentence teaser (for dashboard preview) summarising why this is a strong match.

7. **IMPROVEMENT_TIPS**: Identify 3-5 specific, actionable gaps between the JD requirements and this candidate's resume. Each tip should be a short, direct suggestion (1 sentence max) that would increase the candidate's chances — e.g. "Add hands-on RabbitMQ experience to a bullet", "Mention AWS deployment explicitly", "Include HLD/LLD design experience". Be honest and specific; do not repeat things already present.

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
  "bold_keywords": ["Spring Boot", "microservices", "Java 8", "REST APIs", "Kafka"],
  "improvement_tips": ["tip1", "tip2", "tip3"]
}}

Return ONLY valid JSON, no markdown fences, no extra text."""

    logger.info(f"Tailoring resume for: {job['title']} at {job['company']} "
                f"(prompt ~{len(prompt)//4} tokens)")

    from llm_client import chat_complete
    raw, finish_reason = chat_complete(prompt, max_tokens=6000, temperature=0.5)
    logger.info(f"  LLM response length: {len(raw)} chars, finish_reason: {finish_reason}")

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
    # Normalise any fields that llama may return as lists instead of strings
    for _f in ("summary", "cover_letter", "cover_note"):
        if isinstance(result.get(_f), list):
            result[_f] = "\n".join(str(p) for p in result[_f])
    result.setdefault("cover_letter", result.get("cover_note", ""))
    result.setdefault("cover_note", "")

    # Post-process cover letter — remove duplicate salutation / sign-off lines
    cl = result.get("cover_letter", "")
    # llama models sometimes return cover_letter as a list of paragraphs — normalise to str
    if isinstance(cl, list):
        cl = "\n".join(str(p) for p in cl)
        result["cover_letter"] = cl
    if cl:
        lines = cl.splitlines()
        seen_salutation = False
        seen_signoff    = False
        clean_lines     = []
        for ln in lines:
            stripped = ln.strip()
            is_salutation = bool(re.match(r"dear\s+hiring\s+manager", stripped, re.I))
            is_signoff    = bool(re.match(r"(sincerely|regards|best\s+regards)[,.]?\s*$", stripped, re.I))
            if is_salutation:
                if seen_salutation:
                    continue   # drop duplicate salutation
                seen_salutation = True
            if is_signoff:
                if seen_signoff:
                    continue   # drop duplicate sign-off
                seen_signoff = True
            clean_lines.append(ln)
        # Also remove duplicate trailing name lines (same name appears twice at end)
        result["cover_letter"] = "\n".join(clean_lines).strip()

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
    result.setdefault("improvement_tips", [])

    # Strip markdown ** wrapping that local models (llama) sometimes add to keywords
    def _strip_md(lst):
        return [re.sub(r"^\*+|\*+$", "", k).strip() for k in lst if k]

    result["new_ats_keywords"]  = _strip_md(result["new_ats_keywords"])
    result["key_matches"]       = _strip_md(result.get("key_matches", []))
    result["improvement_tips"]  = _strip_md(result.get("improvement_tips", []))

    # bold_keywords = explicit list from model, else fall back to key_matches
    raw_bold = _strip_md(result.get("bold_keywords") or [])
    # Also include new ATS keywords so freshly injected terms get highlighted
    all_bold = list(dict.fromkeys(raw_bold + result["new_ats_keywords"] + result["key_matches"]))
    result["bold_keywords"] = all_bold[:20]   # cap at 20 to avoid over-bolding

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
