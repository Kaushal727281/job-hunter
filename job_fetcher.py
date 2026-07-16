"""
job_fetcher.py
Scrapes public job listings from multiple portals (no login required).

Working sources:
  - LinkedIn       : guest jobs API
  - Shine          : HTML job cards (India-focused)
  - Foundit        : (formerly Monster India) HTML job cards
  - RemoteOK       : public JSON API
  - WeWorkRemotely : RSS feed (remote roles)
  - HNJobs         : Hacker News jobs page (startup/tech)
  - Indeed         : best-effort HTML scrape
"""

import json
import logging
import os
import time
import re
import xml.etree.ElementTree as ET
import truststore
import requests
from bs4 import BeautifulSoup
from datetime import date
from pathlib import Path
from dotenv import load_dotenv

truststore.inject_into_ssl()
load_dotenv()
logger = logging.getLogger(__name__)

SEEN_JOBS_FILE  = Path(__file__).parent / "output" / "seen_jobs.json"
COOKIES_DIR     = Path(__file__).parent / "output" / "cookies"


# ── Cookie helpers ─────────────────────────────────────────────────────────

def _load_cookies(site: str) -> dict:
    """
    Load browser cookies exported by the 'Cookie-Editor' Chrome extension.
    File: output/cookies/{site}_cookies.json
    Cookie-Editor exports a JSON array like:
      [{"name": "CTK", "value": "abc123", "domain": ".indeed.com", ...}, ...]
    Returns a plain {name: value} dict ready to pass to requests.
    """
    path = COOKIES_DIR / f"{site}_cookies.json"
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text())
        cookies = {c["name"]: c["value"] for c in raw if "name" in c and "value" in c}
        logger.info(f"  [{site}] Loaded {len(cookies)} cookies from {path.name}")
        return cookies
    except Exception as e:
        logger.warning(f"  [{site}] Failed to load cookies: {e}")
        return {}


_SSL_VERIFY: bool = False  # corporate Zscaler proxy intercepts SSL; skip cert verification
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


def _make_session(site: str, extra_headers: dict | None = None) -> requests.Session:
    """Return a requests.Session pre-loaded with saved cookies + browser headers."""
    sess = requests.Session()
    sess.headers.update(_BASE_HEADERS)
    sess.verify = _SSL_VERIFY
    if extra_headers:
        sess.headers.update(extra_headers)
    cookies = _load_cookies(site)
    sess.cookies.update(cookies)
    return sess

_STOP = {
    "senior", "lead", "principal", "staff", "junior", "associate", "software",
    "engineer", "developer", "manager", "architect", "consultant", "specialist",
    "private", "limited", "pvt", "ltd", "inc", "corp", "technologies",
    "solutions", "services", "india", "the", "and", "for", "of",
}

# ── Company type & rating ──────────────────────────────────────────────────

_PRODUCT_COS = {
    "flipkart", "swiggy", "zomato", "ola", "paytm", "phonepe", "razorpay",
    "groww", "zerodha", "cred", "meesho", "nykaa", "byju", "unacademy",
    "freshworks", "zoho", "browserstack", "postman", "hasura", "chargebee",
    "cleartax", "lenskart", "policybazaar", "cars24", "oyo", "myntra",
    "bigbasket", "dunzo", "rapido", "urban company", "dream11", "mpl",
    "juspay", "cashfree", "slice", "smallcase", "darwinbox", "leadsquared",
    "whatfix", "sprinklr", "innovaccer", "druva", "icertis", "mindtickle",
    "niyo", "jupiter", "fi money", "delhivery", "porter", "blackbuck",
    "sharechat", "dailyhunt", "udaan", "moglix", "zetwerk",
    "google", "microsoft", "amazon", "apple", "meta", "netflix", "adobe",
    "salesforce", "oracle", "sap", "servicenow", "workday", "atlassian",
    "github", "gitlab", "hashicorp", "elastic", "mongodb", "databricks",
    "snowflake", "confluent", "stripe", "twilio", "cloudflare",
    "datadog", "splunk", "pagerduty", "newrelic", "dynatrace", "grafana",
    "intuit", "zendesk", "hubspot", "intercom", "slack", "zoom",
    "dropbox", "docusign", "vmware", "nutanix", "palo alto networks",
    "crowdstrike", "qualcomm", "intel", "nvidia", "arm", "broadcom",
    "paypal", "visa", "mastercard", "booking.com", "airbnb", "expedia",
    "linkedin", "walmart labs", "jpmorgan", "goldman sachs", "morgan stanley",
    "deutsche bank", "wells fargo", "citibank", "american express",
    "samsung", "sony", "siemens", "bosch", "philips", "honeywell",
    "uber", "lyft", "twitter", "pinterest", "snap", "tiktok", "bytedance",
}

_SERVICE_COS = {
    "tcs", "tata consultancy", "infosys", "wipro", "hcl", "hcltech",
    "tech mahindra", "mphasis", "ltimindtree", "lti", "mindtree",
    "hexaware", "mastech", "kpit", "persistent", "cyient", "zensar",
    "birlasoft", "coforge", "sonata", "sasken", "accenture", "ibm",
    "capgemini", "cognizant", "dxc", "cgi", "unisys", "ntt data",
    "fujitsu", "atos", "deloitte", "pwc", "ey", "kpmg", "nagarro",
    "globant", "epam", "infobeans", "kellton",
}

# Curated company ratings (0–5) with culture tags
_COMPANY_RATINGS = {
    "google":          (4.5, "Top FAANG · Excellent WLB · Strong Growth"),
    "microsoft":       (4.4, "FAANG · Good WLB · Stable · Strong Benefits"),
    "amazon":          (3.9, "FAANG · Fast Growth · High Pressure · Good Pay"),
    "meta":            (4.2, "FAANG · Great Pay · Strong Engineering Culture"),
    "apple":           (4.3, "FAANG · Premium Products · Good WLB"),
    "netflix":         (4.5, "Top Pay · Freedom & Responsibility · Senior Only"),
    "atlassian":       (4.4, "Remote-First · Great Culture · Strong Growth"),
    "stripe":          (4.5, "Top Startup · Excellent Engineering · High Bar"),
    "databricks":      (4.4, "Strong Growth · Data-First · Excellent Pay"),
    "github":          (4.4, "Developer-First · Good Culture · Remote Friendly"),
    "gitlab":          (4.5, "Fully Remote · Transparent Culture · Open Source"),
    "cloudflare":      (4.3, "Fast Growth · Strong Engineering · Good Pay"),
    "salesforce":      (4.1, "Good WLB · Strong Benefits · Enterprise Scale"),
    "adobe":           (4.1, "Good WLB · Creative Culture · Stable"),
    "oracle":          (3.6, "Stable · Legacy Systems · Slower Innovation"),
    "sap":             (3.8, "Stable · Enterprise · Good Benefits"),
    "servicenow":      (4.2, "Fast Growth · Good Pay · Good Culture"),
    "zoom":            (4.0, "Remote Culture · Good WLB · Stable Post-COVID"),
    "hubspot":         (4.4, "Great Culture · Good WLB · Strong Values"),
    "freshworks":      (4.1, "Indian Product Co · Good Growth · Chennai/Bengaluru"),
    "zoho":            (3.8, "Bootstrap Mindset · Stable · Unique Culture"),
    "razorpay":        (4.2, "Fast Growth · Fintech · Good Pay · Bengaluru"),
    "phonepe":         (4.1, "Unicorn · Fintech · Fast Paced · Bengaluru"),
    "flipkart":        (4.0, "Unicorn · Walmart-backed · Good Pay · Bengaluru"),
    "swiggy":          (3.9, "Unicorn · Fast Paced · Good Pay"),
    "zomato":          (3.8, "Unicorn · Fast Paced · Demanding Culture"),
    "cred":            (4.2, "Unicorn · Premium Culture · Good Pay · Design-First"),
    "meesho":          (4.0, "Unicorn · Social Commerce · Fast Growth"),
    "groww":           (4.1, "Unicorn · Fintech · Good Engineering Culture"),
    "zerodha":         (4.3, "Profitable · Bootstrapped · Great WLB · Bengaluru"),
    "browserstack":    (4.4, "Remote-Friendly · Great Culture · Profitable"),
    "darwinbox":       (4.1, "Unicorn · HR-Tech · Fast Growth · Hyderabad"),
    "chargebee":       (4.2, "SaaS · Good Culture · Global"),
    "delhivery":       (3.9, "Logistics Tech · Fast Growth · IPO"),
    "infosys":         (3.5, "Service Co · Stable · Good for Freshers · Scale"),
    "tcs":             (3.4, "Largest IT · Stable · Process-Heavy · Good Benefits"),
    "wipro":           (3.3, "Service Co · Stable · Slower Growth"),
    "hcl":             (3.4, "Service Co · Good Scale · Niche Products"),
    "tech mahindra":   (3.3, "Service Co · Telecoms Focus · Mid-Growth"),
    "accenture":       (3.6, "Consulting · Global Exposure · Good Learning"),
    "ibm":             (3.5, "Legacy + Cloud Push · Stable · Consulting"),
    "capgemini":       (3.5, "French MNC · Consulting · Good Scale"),
    "cognizant":       (3.4, "Service Co · US-Heavy · Stable"),
    "jpmorgan":        (4.0, "Top Bank · Good Pay · Strong Engineering"),
    "goldman sachs":   (4.1, "Top Bank · Excellent Pay · High Pressure"),
    "morgan stanley":  (3.9, "Top Bank · Good Pay · Stable"),
    "deutsche bank":   (3.7, "Bank · Good Pay · Risk Management"),
    "visa":            (4.1, "Fintech · Good WLB · Stable · Good Benefits"),
    "mastercard":      (4.1, "Fintech · Good Culture · Global"),
    "paypal":          (4.0, "Fintech · Good WLB · Good Pay"),
    "samsung":         (3.8, "Hardware+Software · Korean Culture · Good Pay"),
    "qualcomm":        (4.0, "Semiconductor · Strong R&D · Good Pay · Hyderabad"),
    "intel":           (3.7, "Semiconductor · R&D · Slower Innovation Cycle"),
    "nvidia":          (4.4, "AI/GPU Leader · Excellent Pay · Strong Growth"),
    "amazon web services": (4.1, "Cloud Leader · Strong Engineering · Fast Paced"),
    "microsoft azure": (4.2, "Cloud · Good Culture · Good Pay"),
    "uber":            (4.0, "Global Tech · Good Pay · High Bar"),
    "airbnb":          (4.2, "Global Tech · Strong Design Culture · Good WLB"),
}


def _classify_company(company: str) -> str:
    lc = company.lower().strip()
    for name in _PRODUCT_COS:
        if name in lc:
            return "Product"
    for name in _SERVICE_COS:
        if name in lc:
            return "Service"
    if re.search(r'\b(technologies|tech solutions|it solutions|outsourcing'
                 r'|staffing|consulting|infotech|infosystems|softtech)\b', lc):
        return "Service"
    return "Unknown"


_COMPANY_CACHE_FILE = Path(__file__).parent / "company_type_cache.json"


def _load_company_cache() -> dict:
    try:
        return json.loads(_COMPANY_CACHE_FILE.read_text())
    except Exception:
        return {}


def _save_company_cache(cache: dict) -> None:
    _COMPANY_CACHE_FILE.write_text(json.dumps(cache, indent=2))


def _groq_classify_companies(companies: list) -> dict:
    """Batch-classify companies as Product/Service/Unknown using Groq LLM."""
    if not companies:
        return {}
    try:
        from groq import Groq
        client = Groq(api_key=os.getenv("GROQ_API_KEY", ""))
        results: dict = {}
        for i in range(0, len(companies), 30):
            batch = companies[i:i + 30]
            lines = "\n".join(f"{idx + 1}. {c}" for idx, c in enumerate(batch))
            prompt = (
                "Classify each company below as exactly one of: Product, Service, or Unknown.\n"
                "  Product = builds its own software products/platforms (e.g. Google, Zoho, Freshworks, Razorpay)\n"
                "  Service = IT services / consulting / outsourcing / staffing (e.g. TCS, Infosys, Accenture, Wipro)\n"
                "  Unknown = cannot determine with confidence\n\n"
                f"Companies:\n{lines}\n\n"
                "Reply with ONLY a JSON object mapping the exact company name to its type. "
                "Example: {\"Google\": \"Product\", \"TCS\": \"Service\"}"
            )
            resp = client.chat.completions.create(
                model="llama-3.3-70b-versatile",
                messages=[{"role": "user", "content": prompt}],
                max_tokens=600,
                temperature=0.1,
            )
            raw = resp.choices[0].message.content.strip()
            if raw.startswith("```"):
                raw = raw.split("```", 2)[1]
                if raw.startswith("json"):
                    raw = raw[4:]
                raw = raw.rsplit("```", 1)[0].strip()
            results.update(json.loads(raw))
        return results
    except Exception as e:
        logger.debug(f"Groq company classification failed: {e}")
        return {}


def _rate_company(company: str) -> dict:
    """Return {rating, tags} for a company."""
    lc = company.lower().strip()
    for name, (rating, tags) in _COMPANY_RATINGS.items():
        if name in lc:
            return {"rating": rating, "tags": tags}
    # Default by type
    ctype = _classify_company(company)
    if ctype == "Product":
        return {"rating": 3.8, "tags": "Product Company · Tech-First"}
    if ctype == "Service":
        return {"rating": 3.3, "tags": "Service Company · Good Scale"}
    return {"rating": 3.5, "tags": ""}


# ── Salary estimation ──────────────────────────────────────────────────────

def _estimate_salary(title: str, location: str, company_type: str) -> str:
    """Estimate Indian market salary range in LPA."""
    t = title.lower()

    # Seniority base range
    if any(w in t for w in ["principal", "distinguished", "fellow", "vp ", "vice president"]):
        lo, hi = 60, 110
    elif any(w in t for w in ["lead", "staff", "head of", "director"]):
        lo, hi = 35, 70
    elif any(w in t for w in ["architect"]):
        lo, hi = 30, 65
    elif any(w in t for w in ["manager", "engineering manager"]):
        lo, hi = 28, 60
    elif any(w in t for w in ["senior", "sr."]):
        lo, hi = 18, 40
    else:
        lo, hi = 12, 25

    # Role multiplier
    if any(w in t for w in ["machine learning", "ml engineer", "deep learning", "ai engineer"]):
        m = 1.30
    elif "data scientist" in t:
        m = 1.20
    elif "data engineer" in t:
        m = 1.10
    elif any(w in t for w in ["devops", "sre", "platform engineer", "cloud architect"]):
        m = 1.05
    elif "data analyst" in t:
        m = 0.82
    elif any(w in t for w in ["full stack", "fullstack"]):
        m = 0.95
    else:
        m = 1.0   # java/backend/general

    # Company type premium
    if company_type == "Product":
        m *= 1.30
    elif company_type == "Service":
        m *= 0.82

    # Location adjustment (Bengaluru = 1.0 baseline)
    l = location.lower()
    if "remote" in l:
        lm = 1.05
    elif "bengaluru" in l or "bangalore" in l:
        lm = 1.00
    elif "mumbai" in l:
        lm = 0.97
    elif "delhi" in l or "ncr" in l or "noida" in l or "gurgaon" in l:
        lm = 0.95
    elif "hyderabad" in l:
        lm = 0.90
    elif "pune" in l:
        lm = 0.88
    else:
        lm = 0.90

    return f"₹{int(lo*m*lm)}–{int(hi*m*lm)} LPA (est.)"


# ── Helpers ────────────────────────────────────────────────────────────────

def _norm_key(title: str, company: str) -> str:
    text = re.sub(r"[^a-z0-9 ]", "", (title + " " + company).lower())
    words = [w for w in text.split() if w not in _STOP and len(w) > 2]
    return " ".join(sorted(words))


def _job_base(overrides: dict) -> dict:
    base = {
        "id": "", "title": "", "company": "", "location": "",
        "experience": "", "is_remote": False, "salary": "Not disclosed",
        "apply_link": "", "description": "", "tags": [], "posted_at": "",
        "source": "", "fetched_date": str(date.today()),
        "tailor_result": None, "pdf_path": None,
        "company_type": "Unknown", "company_rating": 3.5,
        "company_tags": "", "salary_estimate": "",
    }
    base.update(overrides)
    company = base["company"]
    base["company_type"] = _classify_company(company)
    cr = _rate_company(company)
    base["company_rating"] = cr["rating"]
    base["company_tags"]   = cr["tags"]
    if not base["salary_estimate"]:
        base["salary_estimate"] = _estimate_salary(
            base["title"], base["location"], base["company_type"]
        )
    return base


_BASE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,*/*;q=0.9",
    "Accept-Language": "en-IN,en-US;q=0.9,en;q=0.8",
    "Connection": "keep-alive",
}


# ── Seen-jobs dedup ────────────────────────────────────────────────────────

def _load_seen() -> set:
    SEEN_JOBS_FILE.parent.mkdir(exist_ok=True)
    if SEEN_JOBS_FILE.exists():
        return set(json.loads(SEEN_JOBS_FILE.read_text()))
    return set()

def _save_seen(seen: set):
    SEEN_JOBS_FILE.write_text(json.dumps(sorted(seen), indent=2))


# ── LinkedIn ───────────────────────────────────────────────────────────────

def _fetch_linkedin(query: str, location: str, days: int = 3) -> list[dict]:
    f_tpr = f"r{days * 86400}"
    url = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
    params = {"keywords": query, "location": location, "f_TPR": f_tpr, "start": 0}
    try:
        resp = requests.get(url, params=params, headers=_BASE_HEADERS, timeout=15)
        resp.raise_for_status()
    except Exception as e:
        logger.warning(f"[LinkedIn] {e}")
        return []
    soup = BeautifulSoup(resp.text, "html.parser")
    jobs = []
    for card in soup.find_all("div", class_="base-card"):
        urn    = card.get("data-entity-urn", "")
        job_id = urn.split(":")[-1] if urn else ""
        if not job_id:
            continue
        title_el   = card.find("h3", class_="base-search-card__title")
        company_el = card.find("h4", class_="base-search-card__subtitle")
        loc_el     = card.find("span", class_="job-search-card__location")
        link_el    = card.find("a", class_="base-card__full-link")
        time_el    = card.find("time")
        title      = title_el.get_text(strip=True) if title_el else ""
        if not title:
            continue
        jobs.append(_job_base({
            "id": f"linkedin_{job_id}", "title": title,
            "company": company_el.get_text(strip=True) if company_el else "",
            "location": loc_el.get_text(strip=True) if loc_el else location,
            "is_remote": "remote" in title.lower(),
            "apply_link": link_el.get("href", "") if link_el else "",
            "posted_at": time_el.get("datetime", "") if time_el else "",
            "source": "LinkedIn",
        }))
    logger.info(f"  [LinkedIn] {len(jobs)} jobs — '{query}'")
    return jobs


def _fetch_linkedin_jd(job_id: str) -> str:
    li_id = job_id.replace("linkedin_", "")
    url = f"https://www.linkedin.com/jobs/view/{li_id}/"
    try:
        resp = requests.get(url, headers=_BASE_HEADERS, timeout=15)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        for cls in ["description__text", "show-more-less-html__markup"]:
            div = soup.find("div", class_=cls)
            if div:
                return div.get_text(separator="\n", strip=True)[:4000]
    except Exception as e:
        logger.debug(f"LinkedIn JD: {e}")
    return ""


# ── Shine ──────────────────────────────────────────────────────────────────

def _shine_url(query: str, location: str) -> str:
    slug_q = re.sub(r"\s+", "-", query.strip().lower())
    slug_l = re.sub(r"\s+", "-", location.strip().lower())
    return f"https://www.shine.com/job-search/{slug_q}-jobs-in-{slug_l}/"


def _normalise_loc(raw: str, fallback: str) -> str:
    if re.match(r"noida|gurgaon|gurugram|delhi", raw, re.I):
        return "Delhi NCR"
    if raw.lower() == "bangalore":
        return "Bengaluru"
    return raw or fallback


def _fetch_shine(query: str, location: str) -> list[dict]:
    url = _shine_url(query, location)
    try:
        resp = requests.get(url, headers=_BASE_HEADERS, timeout=15)
        resp.raise_for_status()
    except Exception as e:
        logger.warning(f"[Shine] {e}")
        return []
    soup  = BeautifulSoup(resp.text, "html.parser")
    cards = soup.find_all("div", class_=re.compile(r"bigCard", re.I)) or \
            soup.find_all("div", class_=re.compile(r"jobCard", re.I))
    jobs = []
    for card in cards:
        text = card.get_text(" ", strip=True)
        if len(text) < 30:
            continue
        title_el = card.find(["h2", "h3", "h4"]) or card.find("a")
        title = title_el.get_text(strip=True) if title_el else ""
        if not title:
            continue
        title = re.sub(r"\s*-\s*\d+\s+\d+\s+years?.*", "", title, flags=re.I).strip()
        company_el = card.find("cite") or card.find("span", class_=re.compile(r"company|employer", re.I))
        exp_m = re.search(r"(\d+)\s+to\s+(\d+)\s+Yrs?", text, re.I)
        loc_m = re.search(
            r"(Bengaluru|Bangalore|Hyderabad|Mumbai|Pune|Chennai|Kolkata"
            r"|Noida|Gurgaon|Gurugram|Delhi\s*NCR|Delhi|Remote)", text, re.I)
        sal_m = re.search(r"([\d.]+\s*[-–]\s*[\d.]+\s*Lakh|Not Mentioned|Competitive)", text, re.I)
        link_el = card.find("a", href=True)
        link = link_el["href"] if link_el else ""
        if link and not link.startswith("http"):
            link = "https://www.shine.com" + link
        job_id = "shine_" + re.sub(r"[^a-z0-9]", "_", link.split("/")[-2] if link else title.lower())[:50]
        jobs.append(_job_base({
            "id": job_id, "title": title,
            "company": company_el.get_text(strip=True) if company_el else "",
            "location": _normalise_loc(loc_m.group(1) if loc_m else "", location),
            "experience": f"{exp_m.group(1)}–{exp_m.group(2)} Yrs" if exp_m else "",
            "salary": sal_m.group(0) if sal_m else "Not disclosed",
            "is_remote": "remote" in title.lower() or "remote" in text[:200].lower(),
            "apply_link": link,
            "description": re.sub(r"\s+", " ", text).strip()[:2000],
            "source": "Shine",
        }))
    logger.info(f"  [Shine] {len(jobs)} jobs — '{query}'")
    return jobs


def _fetch_shine_jd(apply_link: str) -> str:
    if not apply_link:
        return ""
    try:
        resp = requests.get(apply_link, headers=_BASE_HEADERS, timeout=15)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        jd = soup.find("div", class_=re.compile(r"jobDesc|job-desc|jd-content|description", re.I))
        if jd:
            return jd.get_text(separator="\n", strip=True)[:4000]
    except Exception as e:
        logger.debug(f"Shine JD: {e}")
    return ""


# ── Glassdoor India ─────────────────────────────────────────────────────────

def _fetch_glassdoor(query: str, location: str) -> list[dict]:
    """Scrape Glassdoor India job cards (server-rendered HTML, 30 results per page)."""
    loc_slug = re.sub(r"\s+", "-", location.strip().lower())
    q_slug   = re.sub(r"\s+", "-", query.strip().lower())
    loc_len  = len(loc_slug)
    kw_start = loc_len + 1
    kw_end   = kw_start + len(q_slug)
    url = (
        f"https://www.glassdoor.co.in/Job/{loc_slug}-{q_slug}-jobs-"
        f"SRCH_IL.0,{loc_len}_IN115_KO{kw_start},{kw_end}.htm"
    )
    sess = _make_session("glassdoor", {"Referer": "https://www.glassdoor.co.in/"})
    try:
        resp = sess.get(url, timeout=20)
        resp.raise_for_status()
    except Exception as e:
        logger.warning(f"[Glassdoor] {e}")
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    cards = soup.find_all(attrs={"data-test": "jobListing"})
    jobs = []

    for card in cards:
        job_id_raw = card.get("data-jobid", "")
        title_el   = card.find(attrs={"data-test": "job-title"})
        company_el = card.find(class_=re.compile(r"EmployerProfile_compactEmployerName", re.I))
        loc_el     = card.find(attrs={"data-test": "emp-location"})
        sal_el     = card.find(attrs={"data-test": "detailSalary"})
        link_el    = card.find("a", href=re.compile(r"/job-listing/", re.I))

        title = title_el.get_text(strip=True) if title_el else ""
        if not title:
            continue

        link = link_el["href"] if link_el else ""
        if link and not link.startswith("http"):
            link = "https://www.glassdoor.co.in" + link

        loc_text = loc_el.get_text(strip=True) if loc_el else location
        loc_m = re.search(
            r"(Bengaluru|Bangalore|Hyderabad|Mumbai|Pune|Chennai|Kolkata"
            r"|Noida|Gurgaon|Gurugram|Delhi\s*NCR|Delhi|Remote|India)", loc_text, re.I)

        company = company_el.get_text(strip=True) if company_el else ""
        sal_text = sal_el.get_text(strip=True) if sal_el else ""

        jobs.append(_job_base({
            "id": f"glassdoor_{job_id_raw or re.sub(r'[^a-z0-9]', '_', title.lower())[:40]}",
            "title": title,
            "company": company,
            "location": _normalise_loc(loc_m.group(1) if loc_m else "", location),
            "salary": sal_text or "Not disclosed",
            "apply_link": link,
            "is_remote": "remote" in loc_text.lower(),
            "source": "Glassdoor",
        }))

    logger.info(f"  [Glassdoor] {len(jobs)} jobs — '{query}'")
    return jobs


# ── RemoteOK ───────────────────────────────────────────────────────────────

def _fetch_remoteok(query: str) -> list[dict]:
    tag = re.sub(r"[^a-z0-9]", "-", query.lower().split()[0])
    url = f"https://remoteok.com/api?tag={tag}"
    try:
        resp = requests.get(url, headers={**_BASE_HEADERS, "Accept": "application/json"}, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, list) and data:
            data = data[1:]
    except Exception as e:
        logger.warning(f"[RemoteOK] {e}")
        return []
    jobs = []
    for item in data[:15]:
        if not isinstance(item, dict):
            continue
        title   = item.get("position", "")
        company = item.get("company", "")
        job_id  = str(item.get("id", ""))
        if not title or not job_id:
            continue
        sal = ""
        if item.get("salary_min") and item.get("salary_max"):
            sal = f"${item['salary_min']:,}–${item['salary_max']:,}"
        desc = BeautifulSoup(item.get("description", ""), "html.parser").get_text()[:2000]
        jobs.append(_job_base({
            "id": f"remoteok_{job_id}", "title": title, "company": company,
            "location": "Remote", "is_remote": True,
            "salary": sal or "Not disclosed",
            "salary_estimate": sal or _estimate_salary(title, "Remote", _classify_company(company)),
            "apply_link": item.get("url", ""),
            "description": desc,
            "tags": item.get("tags", [])[:8],
            "posted_at": item.get("date", ""),
            "source": "RemoteOK",
        }))
    logger.info(f"  [RemoteOK] {len(jobs)} jobs — '{query}'")
    return jobs


# ── We Work Remotely (RSS) ─────────────────────────────────────────────────

_WWR_FEEDS = {
    "java":       "https://weworkremotely.com/categories/remote-back-end-programming-jobs.rss",
    "python":     "https://weworkremotely.com/categories/remote-back-end-programming-jobs.rss",
    "data":       "https://weworkremotely.com/categories/remote-data-science-ai-jobs.rss",
    "devops":     "https://weworkremotely.com/categories/remote-devops-sysadmin-jobs.rss",
    "fullstack":  "https://weworkremotely.com/categories/remote-full-stack-programming-jobs.rss",
    "frontend":   "https://weworkremotely.com/categories/remote-front-end-programming-jobs.rss",
    "management": "https://weworkremotely.com/categories/remote-management-executive-jobs.rss",
}

def _wwr_feed_for(query: str) -> str:
    q = query.lower()
    for kw, feed in _WWR_FEEDS.items():
        if kw in q:
            return feed
    return _WWR_FEEDS["java"]  # default backend


def _fetch_weworkremotely(query: str) -> list[dict]:
    """We Work Remotely — RSS feed, always works."""
    feed_url = _wwr_feed_for(query)
    headers = {**_BASE_HEADERS, "Accept": "application/rss+xml, application/xml, text/xml, */*"}
    try:
        resp = requests.get(feed_url, headers=headers, timeout=15)
        resp.raise_for_status()
        root = ET.fromstring(resp.content)
    except Exception as e:
        logger.warning(f"[WWR] {e}")
        return []

    ns = {"media": "http://search.yahoo.com/mrss/"}
    keywords = [w.lower() for w in re.split(r"\s+", query) if len(w) > 3]
    jobs = []

    for item in root.iter("item"):
        def txt(tag):
            el = item.find(tag)
            return el.text.strip() if el is not None and el.text else ""

        raw_title = txt("title")        # "Company | Job Title"
        link      = txt("link") or txt("guid")
        region    = txt("region")
        pub_date  = txt("pubDate")
        desc_html = txt("description")

        if "|" in raw_title:
            parts   = raw_title.split("|", 1)
            company = parts[0].strip()
            title   = parts[1].strip()
        else:
            company, title = "", raw_title

        # Filter by query keywords (loose match)
        if keywords and not any(kw in title.lower() or kw in desc_html.lower() for kw in keywords):
            continue

        desc = BeautifulSoup(desc_html, "html.parser").get_text(separator=" ", strip=True)[:2000]
        job_id = re.sub(r"[^a-z0-9]", "_", (title + company).lower())[:50]

        jobs.append(_job_base({
            "id": f"wwr_{job_id}", "title": title, "company": company,
            "location": region or "Remote", "is_remote": True,
            "apply_link": link,
            "description": desc,
            "posted_at": pub_date,
            "source": "WeWorkRemotely",
        }))

    logger.info(f"  [WWR] {len(jobs)} jobs — '{query}'")
    return jobs


# ── Hacker News Jobs ───────────────────────────────────────────────────────

def _fetch_hnjobs(query: str) -> list[dict]:
    """Hacker News jobs page — static HTML, YC-backed companies."""
    try:
        resp = requests.get("https://news.ycombinator.com/jobs", headers=_BASE_HEADERS, timeout=15)
        resp.raise_for_status()
    except Exception as e:
        logger.warning(f"[HNJobs] {e}")
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    keywords = [w.lower() for w in query.split() if len(w) > 3]
    jobs = []

    for row in soup.find_all("tr", class_="athing"):
        # HN renders title in <span class="titleline"><a>...</a></span>
        # or older: <td class="title"><a class="storylink">
        title_span = row.find("span", class_="titleline")
        link_el    = title_span.find("a") if title_span else row.find("a", class_="storylink")
        if not link_el:
            continue

        full_text = link_el.get_text(" ", strip=True)

        # Filter by keywords (show all if no match to avoid empty results)
        if keywords and not any(kw in full_text.lower() for kw in keywords):
            continue

        href = link_el.get("href", "")
        if not href.startswith("http"):
            href = "https://news.ycombinator.com/" + href.lstrip("/")

        # Parse "Company – Role (Location)" or just use full text as title
        sep_m = re.split(r"\s+[–—-]\s+", full_text, maxsplit=1)
        if len(sep_m) == 2:
            company, role = sep_m[0].strip(), sep_m[1].strip()
        else:
            company, role = "", full_text

        loc_m = re.search(r"\(([^)]{2,40})\)", role)
        loc   = loc_m.group(1) if loc_m else "Remote / Global"
        role  = re.sub(r"\s*\([^)]*\)\s*$", "", role).strip() or full_text

        job_id = "hn_" + re.sub(r"[^a-z0-9]", "_", (role + company).lower())[:50]
        jobs.append(_job_base({
            "id": job_id, "title": role, "company": company,
            "location": loc, "is_remote": "remote" in loc.lower(),
            "apply_link": href,
            "source": "HNJobs",
        }))

    logger.info(f"  [HNJobs] {len(jobs)} jobs — '{query}'")
    return jobs


# ── Indeed India (best-effort) ─────────────────────────────────────────────

def _fetch_indeed(query: str, location: str) -> list[dict]:
    """Indeed India — uses browser cookies when available, JSON-LD + HTML fallback."""
    loc_map = {"Delhi NCR": "Delhi", "Remote India": "India"}
    loc  = loc_map.get(location, location)
    sess = _make_session("indeed", {
        "Referer": "https://in.indeed.com/",
        "sec-fetch-site": "same-origin",
        "sec-fetch-mode": "navigate",
    })
    has_cookies = bool(_load_cookies("indeed"))
    if not has_cookies:
        logger.info("  [Indeed] No cookies found — requests may be blocked (403). "
                    "Export cookies from in.indeed.com and save to output/cookies/indeed_cookies.json")
    params = {"q": query, "l": loc, "fromage": "3", "sort": "date"}
    try:
        resp = sess.get("https://in.indeed.com/jobs", params=params, timeout=15)
        resp.raise_for_status()
    except Exception as e:
        logger.warning(f"[Indeed] {e}")
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    jobs = []

    # Each job card is <table class="mainContentTable ...">
    # Title link: <a data-jk="...">  (inside the table)
    # Company:    <span data-testid="company-name">
    # Location:   <div  data-testid="text-location">
    # Salary:     <div  data-testid="attribute_snippet_testid">
    for card in soup.find_all("table", class_=re.compile(r"mainContentTable", re.I)):
        jk_el  = card.find("a", attrs={"data-jk": True})
        if not jk_el:
            continue
        job_id = jk_el.get("data-jk", "")
        title  = jk_el.get_text(strip=True)
        if not title or not job_id:
            continue

        co_el  = card.find("span", attrs={"data-testid": "company-name"}) or \
                 card.find(class_=re.compile(r"companyName", re.I))
        loc_el = card.find("div", attrs={"data-testid": "text-location"}) or \
                 card.find(class_=re.compile(r"companyLocation", re.I))
        sal_el = card.find(attrs={"data-testid": "attribute_snippet_testid"})

        loc_text = loc_el.get_text(strip=True) if loc_el else loc
        loc_m = re.search(
            r"(Bengaluru|Bangalore|Hyderabad|Mumbai|Pune|Chennai|Kolkata"
            r"|Noida|Gurgaon|Gurugram|Delhi\s*NCR|Delhi|Remote)", loc_text, re.I)

        jobs.append(_job_base({
            "id": f"indeed_{job_id}", "title": title,
            "company": co_el.get_text(strip=True) if co_el else "",
            "location": _normalise_loc(loc_m.group(1) if loc_m else "", loc_text or loc),
            "salary": sal_el.get_text(strip=True) if sal_el else "Not disclosed",
            "is_remote": "remote" in loc_text.lower(),
            "apply_link": f"https://in.indeed.com/viewjob?jk={job_id}",
            "source": "Indeed",
        }))

    logger.info(f"  [Indeed] {len(jobs)} jobs — '{query}'")
    return jobs


# ── Full JD fetchers for each source ──────────────────────────────────────

def _fetch_indeed_jd(job_id: str) -> str:
    jk = job_id.replace("indeed_", "")
    url = f"https://in.indeed.com/viewjob?jk={jk}"
    try:
        sess = _make_session("indeed")
        resp = sess.get(url, timeout=15)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        el = (soup.find(id="jobDescriptionText") or
              soup.find(class_="jobDescriptionText") or
              soup.find(attrs={"data-testid": "jobDescriptionText"}))
        if el:
            return el.get_text(separator="\n", strip=True)[:4000]
    except Exception as e:
        logger.debug(f"Indeed JD: {e}")
    return ""


def _fetch_glassdoor_jd(apply_link: str) -> str:
    if not apply_link:
        return ""
    try:
        sess = _make_session("glassdoor")
        resp = sess.get(apply_link, timeout=20)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        el = soup.find(class_=re.compile(r"jobDescription", re.I))
        if el:
            return el.get_text(separator="\n", strip=True)[:4000]
    except Exception as e:
        logger.debug(f"Glassdoor JD: {e}")
    return ""


def _fetch_generic_jd(apply_link: str, source: str = "") -> str:
    """Generic JD fetcher — tries common description containers, then falls back to body text.
    Works for HNJobs (YC pages, Ashby, Lever, Greenhouse, Workday, etc.) and any other source."""
    if not apply_link:
        return ""
    try:
        sess = _make_session(source.lower() or "generic")
        resp = sess.get(apply_link, timeout=15)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        # 1. Named containers used by common ATS / job boards
        for attrs in [
            {"id": "jobDescriptionText"},                                      # Indeed
            {"data-testid": "jobDescriptionText"},                             # Indeed
            {"class": re.compile(r"jobDescription|job.?desc", re.I)},         # Glassdoor, generic
            {"class": re.compile(r"show_job|job.?detail|job.?posting", re.I)},# YC, generic
            {"class": re.compile(r"posting.?body|job.?body", re.I)},          # Lever
            {"id": re.compile(r"job.?desc|content|posting", re.I)},           # generic
            {"class": re.compile(r"description__text|show-more-less-html", re.I)},  # LinkedIn
            {"class": re.compile(r"prose|markdown|rich.?text", re.I)},        # modern boards
            {"class": re.compile(r"content|details|about.?role|responsibilities", re.I)},
        ]:
            el = soup.find(attrs=attrs)
            if el:
                text = el.get_text(separator="\n", strip=True)
                if len(text) > 200:
                    return text[:4000]

        # 2. Fallback: collect all substantial <p> and <li> text from the page body
        for tag in soup(["script", "style", "nav", "header", "footer"]):
            tag.decompose()
        paragraphs = [t.get_text(strip=True) for t in soup.find_all(["p", "li"])
                      if len(t.get_text(strip=True)) > 40]
        if paragraphs:
            return "\n".join(paragraphs[:80])[:4000]
    except Exception as e:
        logger.debug(f"{source} JD ({apply_link[:60]}): {e}")
    return ""


def _fetch_hnjobs_jd(apply_link: str) -> str:
    return _fetch_generic_jd(apply_link, "HNJobs")


# ── Full JD dispatcher ─────────────────────────────────────────────────────

def fetch_full_jd(job: dict) -> str:
    existing = job.get("description", "")
    if existing and len(existing) > 400:
        return existing
    src = job.get("source", "")
    link = job.get("apply_link", "")
    if src == "LinkedIn":
        return _fetch_linkedin_jd(job["id"]) or existing
    if src == "Shine":
        return _fetch_shine_jd(link) or existing
    if src == "Indeed":
        return _fetch_indeed_jd(job["id"]) or existing
    if src == "Glassdoor":
        return _fetch_glassdoor_jd(link) or existing
    # HNJobs, RemoteOK, WeWorkRemotely, and any future source — fetch from apply link
    if link:
        return _fetch_generic_jd(link, src) or existing
    return existing


# ── Main entry ─────────────────────────────────────────────────────────────

def fetch_jobs(config: dict, limit: int | None = None) -> list[dict]:
    search_cfg = config["job_search"]
    filters    = config.get("filters", {})
    max_jobs   = limit or search_cfg.get("max_jobs_per_run", 60)
    days       = int(search_cfg.get("days_old", 3))
    exclude    = [k.lower() for k in filters.get("exclude_keywords", [])]
    min_exp    = filters.get("min_experience_years", 0)
    max_exp    = filters.get("max_experience_years", 99)
    enabled    = {s.lower() for s in search_cfg.get(
        "sources", ["LinkedIn", "Shine", "Foundit", "RemoteOK",
                    "WeWorkRemotely", "HNJobs", "Indeed"])}

    seen: set       = _load_seen()
    seen_keys: set  = set()
    all_jobs: list  = []

    _li_map = {
        "Delhi NCR":    "Delhi, India",
        "Remote India": "India",
        "Bengaluru":    "Bengaluru, Karnataka, India",
        "Hyderabad":    "Hyderabad, Telangana, India",
        "Mumbai":       "Mumbai, Maharashtra, India",
        "Pune":         "Pune, Maharashtra, India",
    }

    def _add(job: dict) -> bool:
        if len(all_jobs) >= max_jobs or not job["title"] or job["id"] in seen:
            return False
        nk = _norm_key(job["title"], job["company"])
        if nk and nk in seen_keys:
            seen.add(job["id"])
            return False
        text = (job["title"] + " " + job.get("description", "")[:300]).lower()
        if any(kw in text for kw in exclude):
            return False
        if job.get("experience"):
            nums = re.findall(r"\d+", job["experience"])
            if nums and (int(nums[0]) < min_exp or int(nums[0]) > max_exp):
                return False
        all_jobs.append(job)
        seen.add(job["id"])
        if nk:
            seen_keys.add(nk)
        return True

    for query in search_cfg["queries"]:
        if len(all_jobs) >= max_jobs:
            break

        for location in search_cfg["locations"]:
            if len(all_jobs) >= max_jobs:
                break
            li_loc     = _li_map.get(location, location)
            shine_loc  = location.replace(" India", "").strip()

            if "linkedin" in enabled:
                for j in _fetch_linkedin(query, li_loc, days=days): _add(j)
                time.sleep(0.4)
            if "shine" in enabled:
                for j in _fetch_shine(query, shine_loc): _add(j)
                time.sleep(0.3)
            if "glassdoor" in enabled:
                for j in _fetch_glassdoor(query, shine_loc): _add(j)
                time.sleep(0.5)
            if "indeed" in enabled:
                for j in _fetch_indeed(query, location): _add(j)
                time.sleep(0.5)

        # Remote / global sources — once per query
        if "remoteok" in enabled:
            for j in _fetch_remoteok(query): _add(j)
            time.sleep(0.5)
        if "weworkremotely" in enabled:
            for j in _fetch_weworkremotely(query): _add(j)
            time.sleep(0.4)
        if "hnjobs" in enabled:
            for j in _fetch_hnjobs(query): _add(j)
            time.sleep(0.3)

    _save_seen(seen)
    logger.info(f"Total: {len(all_jobs)} new jobs (dedup active)")

    # ── Groq-classify companies still marked Unknown ──────────────────────
    cache = _load_company_cache()
    unknown_cos = list({j["company"] for j in all_jobs
                        if j.get("company_type") == "Unknown" and j.get("company")})
    to_classify = [c for c in unknown_cos if c not in cache]
    if to_classify:
        logger.info(f"  Classifying {len(to_classify)} unknown companies via Groq…")
        new_types = _groq_classify_companies(to_classify)
        cache.update(new_types)
        _save_company_cache(cache)
        logger.info(f"  Classification done — {len(new_types)} companies typed")
    # Apply cache to all Unknown jobs in this batch
    for job in all_jobs:
        if job.get("company_type") == "Unknown":
            classified = cache.get(job.get("company", ""), "Unknown")
            if classified in ("Product", "Service"):
                job["company_type"] = classified

    return all_jobs
