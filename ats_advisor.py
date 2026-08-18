"""
ats_advisor.py
==============
Two responsibilities:
  1. get_company_ats_profile(job)  →  which ATS system the company likely uses,
     which resume template to submit, and why.
  2. run_ats_postmortem(job, resume_html) →  simulate an ATS parse and return a
     detailed keyword/section/format analysis with a Pass / Borderline / Fail verdict.

No LLM calls — fully deterministic so it runs instantly on every page load.
"""

from __future__ import annotations
import re
from bs4 import BeautifulSoup

# ── 1. COMPANY ATS PROFILES ────────────────────────────────────────────────

# Each profile:
#   ats_system    : name of the ATS platform
#   template      : one of classic | compact | modern | tech | executive
#   template_why  : one-line human reason
#   rules         : list of bullet-point DOs / DON'Ts for this ATS
#   parse_notes   : what the ATS is known to misread / drop

_PROFILES: list[dict] = [
    {
        "id": "faang",
        "match": lambda c, t, d: any(k in c for k in (
            "google","amazon","meta","apple","microsoft","netflix","uber","airbnb",
            "stripe","shopify","atlassian","salesforce","twilio","datadog","snowflake",
            "mongodb","confluent","elastic","hashicorp","gitlab","github","dropbox",
            "figma","notion","canva","bytedance","grab","sea limited","gojek",
            "razorpay","cred","zepto","meesho",
        )),
        "label":        "FAANG / Big Tech",
        "ats_system":   "Greenhouse / Workday (custom pipeline)",
        "template":     "tech",
        "template_why": "Engineering teams parse with keyword scanners — minimal Tech layout scores highest",
        "rules": [
            "✅ Lead each bullet with a strong action verb (Led, Architected, Reduced, Shipped)",
            "✅ Include measurable impact (%, latency, scale, team size)",
            "✅ Mirror exact tech acronyms from the JD (AWS vs Amazon Web Services)",
            "✅ One-page or clean two-page — no graphics, no sidebar",
            "⛔ No tables, columns or text boxes — Greenhouse text-extracts, columns become garbled",
            "⛔ No skill star ratings or progress bars",
        ],
        "parse_notes": [
            "Greenhouse extracts raw text — CSS columns merge left+right into one stream",
            "Workday can drop content inside <table> cells",
        ],
    },
    {
        "id": "indian_product",
        "match": lambda c, t, d: any(k in c for k in (
            "flipkart","zomato","swiggy","byjus","byju","ola","paytm","phonepe",
            "freshworks","zoho","chargebee","clevertap","browserstack","postman",
            "groww","upstox","smallcase","unacademy","vedantu","dunzo","urban company",
            "lenskart","nykaa","boat","dream11","mpl","games24x7","juspay",
        )),
        "label":        "Indian Product Company",
        "ats_system":   "Greenhouse / Lever / internal ATS",
        "template":     "modern",
        "template_why": "Modern sidebar balances visual polish with ATS-safe single-column text flow",
        "rules": [
            "✅ Highlight scale numbers — MAU, TPS, data volume",
            "✅ Mention CI/CD, observability tools — Indian product cos value DevOps ownership",
            "✅ Projects section matters a lot here — include side projects",
            "✅ Keep skills as plain comma-separated text, not a styled grid",
            "⛔ No fancy two-column layouts — Greenhouse single-streams text",
        ],
        "parse_notes": [
            "Greenhouse / Lever both handle clean HTML well",
            "Sidebar content in CSS columns may be read out-of-order",
        ],
    },
    {
        "id": "it_services",
        "match": lambda c, t, d: any(k in c for k in (
            "tcs","infosys","wipro","hcl","tech mahindra","cognizant","capgemini",
            "accenture","ibm","mphasis","hexaware","l&t infotech","ltimindtree",
            "mindtree","niit technologies","mastech","zensar","syntel","kpit",
            "persistent systems","cyient","sonata software","sasken",
        )) or (t in ("service","consulting")),
        "label":        "IT Services / Consulting",
        "ats_system":   "Taleo / SAP SuccessFactors (strict plain-text parsers)",
        "template":     "compact",
        "template_why": "Taleo/SAP are old-school parsers — Compact single-column fits the most text without any formatting risk",
        "rules": [
            "✅ Use exact skill keywords that appear in the JD — Taleo is keyword-density driven",
            "✅ Spell out acronyms at least once (e.g. 'Spring Boot (microservices framework)')",
            "✅ Skills section must be a flat comma-separated list — no categories",
            "✅ Standard section headings: 'Work Experience', 'Education', 'Skills'",
            "⛔ No columns, no sidebar, no text boxes — Taleo linearises HTML top-to-bottom",
            "⛔ No images, icons or SVGs",
            "⛔ No CSS-generated content (::before / ::after) — invisible to parser",
            "⛔ Avoid special characters (·, –, →) — some Taleo installs garble them",
        ],
        "parse_notes": [
            "Taleo (Oracle) is the strictest: strips all HTML, reads raw text only",
            "SAP SuccessFactors also strips formatting — only text content is indexed",
            "Both rank candidates purely on keyword frequency in the extracted text",
        ],
    },
    {
        "id": "finance_banking",
        "match": lambda c, t, d: any(k in c for k in (
            "jpmorgan","goldman sachs","barclays","hsbc","morgan stanley","wells fargo",
            "citibank","deutsche bank","bank of america","credit suisse","ubs","nomura",
            "macquarie","rbc","td bank","visa","mastercard","american express",
            "fidelity","blackrock","deloitte","kpmg","ey ","ernst","pwc","bain",
            "mckinsey","boston consulting",
        )),
        "label":        "Finance / Banking / Big 4",
        "ats_system":   "Workday / iCIMS (formal parsers)",
        "template":     "classic",
        "template_why": "Classic B&W is the safest for Workday/iCIMS — formal, single-column, no styling risk",
        "rules": [
            "✅ Emphasise compliance, risk, audit, regulatory keywords if in JD",
            "✅ Include domain: banking, insurance, payments, trading, risk management",
            "✅ Formal tone — avoid informal language",
            "✅ Clean reverse-chronological work history is mandatory",
            "⛔ No sidebar, no colour headers — Workday PDF parser misreads them",
            "⛔ No skill bars or ratings",
        ],
        "parse_notes": [
            "Workday parses PDF directly — avoid text-in-images",
            "iCIMS scores keyword density against job requisition fields",
        ],
    },
    {
        "id": "startup",
        "match": lambda c, t, d: any(k in d[:300] for k in (
            "series a","series b","seed","y combinator","ycombinator","techstars",
        )) or any(k in c for k in ("startup","ventures","labs",)),
        "label":        "Startup",
        "ats_system":   "Lever / Ashby / no ATS (human first-pass)",
        "template":     "modern",
        "template_why": "Startups often do human first-pass — Modern Sidebar stands out visually",
        "rules": [
            "✅ Lead with impact: shipped, scaled, reduced, built from scratch",
            "✅ Side projects and open-source work count heavily",
            "✅ Show ownership breadth — full-stack, infra, oncall",
            "✅ Concise — 1 page preferred",
            "⛔ No fluff like 'results-driven professional'",
        ],
        "parse_notes": [
            "Lever / Ashby parse HTML cleanly",
            "Many startups skip ATS entirely — recruiter reads PDF directly",
        ],
    },
    {
        "id": "mnc_india",
        "match": lambda c, t, d: any(k in c for k in (
            "oracle","sap","cisco","intel","qualcomm","dell","hp ","hewlett",
            "siemens","bosch","honeywell","ge ","general electric","philips",
            "nokia","ericsson","ntt","fujitsu","hitachi","samsung","lg ",
        )),
        "label":        "MNC (India office)",
        "ats_system":   "Workday / Taleo (varies by region)",
        "template":     "classic",
        "template_why": "MNCs use enterprise ATS — Classic single-column is the safest choice",
        "rules": [
            "✅ Mirror exact JD keywords — enterprise ATS ranks by keyword frequency",
            "✅ Include global standards: ISO, CMMI, Agile, ITIL if relevant",
            "✅ Quantify everything — MNC ATS rank quantified bullets higher",
            "⛔ No fancy formatting — Workday and Taleo both flatten HTML",
        ],
        "parse_notes": [
            "Workday parses PDF natively — test with plain-text extraction first",
        ],
    },
]

_DEFAULT_PROFILE = {
    "id":           "default",
    "label":        "General / Unknown",
    "ats_system":   "Unknown — assume Workday-class parser",
    "template":     "classic",
    "template_why": "Safest default — single-column Classic passes all major ATS parsers",
    "rules": [
        "✅ Plain comma-separated skills list",
        "✅ Standard section headings",
        "⛔ No columns, tables, or CSS-generated content",
    ],
    "parse_notes": [
        "Unknown ATS — optimise for lowest-common-denominator plain-text extraction",
    ],
}


def get_company_ats_profile(job: dict) -> dict:
    """Return the ATS profile best matching this job's company / type."""
    company = (job.get("company") or "").lower()
    title   = (job.get("title")   or "").lower()
    desc    = (job.get("description") or "").lower()
    cotype  = (job.get("company_type") or "").lower()

    for p in _PROFILES:
        try:
            if p["match"](company, cotype, desc):
                return p
        except Exception:
            pass
    return _DEFAULT_PROFILE


# ── 2. ATS POSTMORTEM ENGINE ───────────────────────────────────────────────

def _extract_jd_keywords(jd: str) -> list[str]:
    """Pull tech/skill keywords from the JD — simple heuristic, no LLM."""
    # Known tech keywords to look for
    TECH_VOCAB = [
        "java","python","javascript","typescript","kotlin","scala","go","rust","c++","c#",
        "spring boot","spring","hibernate","jpa","jersey","jax-rs","rest","restful","grpc",
        "microservices","monolith","kafka","rabbitmq","activemq","redis","memcached",
        "postgresql","mysql","oracle","mongodb","cassandra","elasticsearch","dynamodb",
        "aws","azure","gcp","kubernetes","docker","terraform","helm","ansible",
        "jenkins","github actions","gitlab ci","ci/cd","devops",
        "angular","react","vue","next.js","node.js","graphql",
        "oauth2","jwt","openid","saml","sso","ldap",
        "agile","scrum","jira","kanban",
        "prometheus","grafana","elk","splunk","datadog","newrelic",
        "testng","junit","selenium","playwright","cypress","jest",
        "hadoop","spark","flink","airflow","dbt","snowflake","databricks",
        "llm","rag","langchain","vector db","pinecone","openai","ml","ai",
        "linux","bash","shell","git",
        "api gateway","load balancer","cdn","nginx","apache",
        "soa","event-driven","cqrs","saga","circuit breaker",
        "multithreading","concurrency","jvm","gc tuning",
        "design patterns","solid","ddd","tdd","bdd",
        "team lead","leadership","mentor","architect","senior","staff",
        "performance","scalability","reliability","availability","sla","slo",
        "insurance","banking","fintech","payments","e-commerce","healthcare",
        "underwriting","risk","compliance","audit","regulatory",
    ]
    jd_lower = jd.lower()
    found = []
    for kw in TECH_VOCAB:
        # whole-word match
        if re.search(r'\b' + re.escape(kw) + r'\b', jd_lower):
            found.append(kw)
    return found


def _extract_resume_text(resume_html: str) -> str:
    soup = BeautifulSoup(resume_html, "html.parser")
    # Remove script/style
    for tag in soup(["script", "style"]):
        tag.decompose()
    return soup.get_text(" ").lower()


def _check_sections(resume_html: str) -> dict[str, bool]:
    """Check whether key ATS-expected sections exist."""
    text = _extract_resume_text(resume_html).lower()
    return {
        "contact":    bool(re.search(r'@|phone|\+91|\+1', text)),
        "summary":    bool(re.search(r'summary|profile|objective', text)),
        "experience": bool(re.search(r'experience|employment|work history', text)),
        "skills":     bool(re.search(r'skills|technologies|tech stack', text)),
        "education":  bool(re.search(r'education|university|college|b\.tech|bachelor|master', text)),
        "projects":   bool(re.search(r'project|github|open.?source', text)),
    }


def _detect_duplicate_bullets(resume_html: str) -> list[str]:
    """Find bullet text that appears more than once — a known tailoring bug."""
    soup = BeautifulSoup(resume_html, "html.parser")
    bullets = [li.get_text(strip=True) for li in soup.find_all("li")]
    seen, dupes = set(), []
    for b in bullets:
        norm = re.sub(r'\s+', ' ', b).lower().strip()
        if len(norm) > 20:
            if norm in seen:
                dupes.append(b[:80])
            seen.add(norm)
    return dupes


def _detect_skills_format_issues(resume_html: str) -> list[str]:
    """Detect skills section formatting problems."""
    issues = []
    soup = BeautifulSoup(resume_html, "html.parser")

    # CSS ::after separators — not visible in HTML, but warn if .tag exists
    if soup.find(class_="tag"):
        issues.append("Skills use CSS class '.tag' with ::after separators — ATS cannot read CSS-generated commas")

    # Skill category labels mixed into skill text
    skill_text = ""
    for el in soup.find_all(class_=re.compile(r'skill')):
        skill_text += el.get_text(" ")

    if re.search(r'languages.*?:.*?frameworks', skill_text, re.I):
        issues.append("Category label 'Languages & Frameworks:' is part of skill text — ATS reads it as a skill name")

    # Skills broken by comma split (e.g. "AWS (EC2" as separate tag)
    raw_html = resume_html
    if "AWS (EC2" in raw_html and "S3" in raw_html and "RDS)" in raw_html:
        issues.append("AWS services split into separate tags: 'AWS (EC2', 'S3', 'RDS)' — ATS reads them as 3 unrelated skills")

    return issues


def run_ats_postmortem(job: dict, resume_html: str) -> dict:
    """
    Simulate an ATS parse of the tailored resume against the job description.
    Returns a dict with:
      - keyword_hits / keyword_misses / keyword_score (0-100)
      - sections (what was found)
      - duplicate_bullets
      - format_issues
      - verdict: "Pass" | "Borderline" | "Fail"
      - verdict_color: green | orange | red
      - summary: one-line verdict explanation
      - recommendations: list of actionable fixes
    """
    jd        = job.get("description") or ""
    jd_kws    = _extract_jd_keywords(jd)
    resume_t  = _extract_resume_text(resume_html)
    profile   = get_company_ats_profile(job)

    # Keyword matching
    hits   = [kw for kw in jd_kws if re.search(r'\b' + re.escape(kw) + r'\b', resume_t)]
    misses = [kw for kw in jd_kws if kw not in hits]
    kw_score = round(len(hits) / max(len(jd_kws), 1) * 100)

    # Section detection
    sections = _check_sections(resume_html)
    missing_sections = [s for s, present in sections.items() if not present]

    # Duplicate bullets
    dupes = _detect_duplicate_bullets(resume_html)

    # Format issues
    fmt_issues = _detect_skills_format_issues(resume_html)

    # Contact completeness
    contact_issues = []
    if "linkedin" not in resume_t:
        contact_issues.append("LinkedIn URL missing")
    if "github" not in resume_t:
        contact_issues.append("GitHub URL missing")

    # Verdict
    deductions = 0
    if kw_score < 40:   deductions += 3
    elif kw_score < 60: deductions += 2
    elif kw_score < 75: deductions += 1
    if dupes:           deductions += 2
    if fmt_issues:      deductions += len(fmt_issues)
    if missing_sections:deductions += len(missing_sections)

    if deductions == 0 and kw_score >= 75:
        verdict, color = "Pass", "green"
        summary = f"Strong match — {kw_score}% of JD keywords found, no format issues."
    elif deductions <= 2 and kw_score >= 50:
        verdict, color = "Borderline", "orange"
        summary = f"Likely screened in but may lose to a better-optimised resume ({kw_score}% keyword match)."
    else:
        verdict, color = "Fail", "red"
        summary = f"High risk of ATS rejection — {kw_score}% keyword match with {len(fmt_issues)+len(dupes)} structural issue(s)."

    # Actionable recommendations
    recs = []
    if dupes:
        recs.append(f"🔴 Remove {len(dupes)} duplicate bullet(s): \"{dupes[0]}…\"")
    for fi in fmt_issues:
        recs.append(f"🔴 Format: {fi}")
    for ci in contact_issues:
        recs.append(f"🟡 Contact: {ci}")
    if misses[:5]:
        recs.append(f"🟡 Missing JD keywords: {', '.join(misses[:5])}" + (" + more" if len(misses) > 5 else ""))
    for s in missing_sections:
        recs.append(f"🟡 Section not detected by ATS: '{s}'")
    if not recs:
        recs.append("✅ No critical issues found — resume is well-optimised for this ATS.")

    return {
        "profile":          profile,
        "keyword_hits":     hits,
        "keyword_misses":   misses,
        "keyword_score":    kw_score,
        "sections":         sections,
        "missing_sections": missing_sections,
        "duplicate_bullets": dupes,
        "format_issues":    fmt_issues,
        "contact_issues":   contact_issues,
        "verdict":          verdict,
        "verdict_color":    color,
        "summary":          summary,
        "recommendations":  recs,
    }
