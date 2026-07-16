"""
job_fetcher.py
Scrapes public job listings from LinkedIn and Shine.com (no login required).

Sources:
  - LinkedIn: guest jobs API (no login, 10 per query)
  - Shine.com: HTML job cards (India-focused, no login)
"""

import json
import logging
import time
import re
import truststore
import requests
from bs4 import BeautifulSoup
from datetime import date
from pathlib import Path
from dotenv import load_dotenv

truststore.inject_into_ssl()
load_dotenv()
logger = logging.getLogger(__name__)

SEEN_JOBS_FILE = Path(__file__).parent / "output" / "seen_jobs.json"

# Common words to strip when building a dedup key from title+company
_STOP = {
    "senior", "lead", "principal", "staff", "junior", "associate", "software",
    "engineer", "developer", "manager", "architect", "consultant", "specialist",
    "private", "limited", "pvt", "ltd", "inc", "corp", "technologies",
    "solutions", "services", "india", "the", "and", "for", "of",
}


def _norm_key(title: str, company: str) -> str:
    """Normalised dedup key combining title + company, source-agnostic."""
    text = re.sub(r"[^a-z0-9 ]", "", (title + " " + company).lower())
    words = [w for w in text.split() if w not in _STOP and len(w) > 2]
    return " ".join(sorted(words))          # sorted so order doesn't matter


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


# ── Seen-jobs deduplication ──────────────────────────────────────────────────

def _load_seen() -> set:
    SEEN_JOBS_FILE.parent.mkdir(exist_ok=True)
    if SEEN_JOBS_FILE.exists():
        return set(json.loads(SEEN_JOBS_FILE.read_text()))
    return set()

def _save_seen(seen: set):
    SEEN_JOBS_FILE.write_text(json.dumps(sorted(seen), indent=2))


# ── LinkedIn guest API ───────────────────────────────────────────────────────

def _fetch_linkedin(query: str, location: str, days: int = 3) -> list[dict]:
    """LinkedIn public guest jobs API — no login required."""
    # days → seconds for f_TPR param
    f_tpr = f"r{days * 86400}"
    url = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
    params = {"keywords": query, "location": location, "f_TPR": f_tpr, "start": 0}
    try:
        resp = requests.get(url, params=params, headers=_BASE_HEADERS, timeout=15)
        resp.raise_for_status()
    except Exception as e:
        logger.warning(f"[LinkedIn] Failed for '{query}' / '{location}': {e}")
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    jobs = []
    for card in soup.find_all("div", class_="base-card"):
        urn = card.get("data-entity-urn", "")
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

        jobs.append({
            "id": f"linkedin_{job_id}",
            "title": title,
            "company": company_el.get_text(strip=True) if company_el else "",
            "location": loc_el.get_text(strip=True) if loc_el else location,
            "experience": "",
            "is_remote": "remote" in title.lower(),
            "salary": "Not disclosed",
            "apply_link": link_el.get("href", "") if link_el else "",
            "description": "",
            "tags": [],
            "posted_at": time_el.get("datetime", "") if time_el else "",
            "source": "LinkedIn",
            "fetched_date": str(date.today()),
            "tailor_result": None,
            "pdf_path": None,
        })
    logger.info(f"  [LinkedIn] {len(jobs)} jobs")
    return jobs


def _fetch_linkedin_jd(job_id: str) -> str:
    """Fetch full description from LinkedIn public job page."""
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
        logger.debug(f"LinkedIn JD fetch failed: {e}")
    return ""


# ── Shine.com ────────────────────────────────────────────────────────────────

def _shine_url(query: str, location: str) -> str:
    slug_q = re.sub(r"\s+", "-", query.strip().lower())
    slug_l = re.sub(r"\s+", "-", location.strip().lower())
    return f"https://www.shine.com/job-search/{slug_q}-jobs-in-{slug_l}/"


def _fetch_shine(query: str, location: str) -> list[dict]:
    """Scrape Shine.com job cards — India-focused, static HTML."""
    url = _shine_url(query, location)
    try:
        resp = requests.get(url, headers=_BASE_HEADERS, timeout=15)
        resp.raise_for_status()
    except Exception as e:
        logger.warning(f"[Shine] Failed for '{query}' / '{location}': {e}")
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    # The big job card class — inspect shows: jobCardNova_bigCard__W2xn3
    cards = soup.find_all("div", class_=re.compile(r"bigCard", re.I))
    if not cards:
        # Fallback: any div whose class contains 'jobCard'
        cards = soup.find_all("div", class_=re.compile(r"jobCard", re.I))

    jobs = []
    for card in cards:
        text = card.get_text(" ", strip=True)
        # Skip cards that look like ads or short snippets
        if len(text) < 30:
            continue

        # Title — first <a> or <h2>/<h3> in card
        title_el = card.find(["h2", "h3", "h4"]) or card.find("a")
        title = title_el.get_text(strip=True) if title_el else ""
        if not title:
            continue

        # Strip experience range pattern from title: "Title - X  Y years"
        title = re.sub(r"\s*-\s*\d+\s+\d+\s+years?.*", "", title, flags=re.I).strip()

        # Company — look for cite or span after title
        company_el = card.find("cite") or card.find("span", class_=re.compile(r"company|employer", re.I))
        company = company_el.get_text(strip=True) if company_el else ""

        # Experience
        exp_match = re.search(r"(\d+)\s+to\s+(\d+)\s+Yrs?", text, re.I)
        experience = f"{exp_match.group(1)}–{exp_match.group(2)} Yrs" if exp_match else ""

        # Location
        loc_match = re.search(r"(Bangalore|Bengaluru|Mumbai|Delhi|Hyderabad|Pune|Chennai|Remote)", text, re.I)
        loc = loc_match.group(1) if loc_match else location

        # Salary
        sal_match = re.search(r"([\d.]+\s*[-–]\s*[\d.]+\s*Lakh|Not Mentioned|Competitive)", text, re.I)
        salary = sal_match.group(0) if sal_match else "Not disclosed"

        # Apply link
        link_el = card.find("a", href=True)
        link = link_el["href"] if link_el else ""
        if link and not link.startswith("http"):
            link = "https://www.shine.com" + link

        # Unique ID from link
        job_id = "shine_" + re.sub(r"[^a-z0-9]", "_", link.split("/")[-2] if link else title.lower())[:50]

        # Description snippet
        desc = re.sub(r"\s+", " ", text).strip()[:2000]

        jobs.append({
            "id": job_id,
            "title": title,
            "company": company,
            "location": loc,
            "experience": experience,
            "is_remote": "remote" in title.lower() or "remote" in desc[:200].lower(),
            "salary": salary,
            "apply_link": link,
            "description": desc,
            "tags": [],
            "posted_at": "",
            "source": "Shine",
            "fetched_date": str(date.today()),
            "tailor_result": None,
            "pdf_path": None,
        })

    logger.info(f"  [Shine] {len(jobs)} jobs")
    return jobs


def _fetch_shine_jd(apply_link: str) -> str:
    """Fetch full description from Shine job detail page."""
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
        logger.debug(f"Shine JD fetch failed: {e}")
    return ""


# ── Public API ───────────────────────────────────────────────────────────────

def fetch_full_jd(job: dict) -> str:
    """Fetch the complete job description for a job before tailoring."""
    existing = job.get("description", "")
    if existing and len(existing) > 400:
        return existing
    src = job.get("source", "")
    if src == "LinkedIn":
        return _fetch_linkedin_jd(job["id"]) or existing
    if src == "Shine":
        return _fetch_shine_jd(job.get("apply_link", "")) or existing
    return existing


def fetch_jobs(config: dict, limit: int | None = None) -> list[dict]:
    """
    Fetch new (unseen) jobs from LinkedIn + Shine.com.
    Respects config filters: exclude_keywords, min_experience, max_experience, locations, job_type.
    """
    search_cfg = config["job_search"]
    filters    = config.get("filters", {})
    max_jobs   = limit or search_cfg.get("max_jobs_per_run", 20)
    days       = int(search_cfg.get("days_old", 3))
    exclude    = [k.lower() for k in filters.get("exclude_keywords", [])]
    min_exp    = filters.get("min_experience_years", 0)
    max_exp    = filters.get("max_experience_years", 99)
    job_types  = [t.lower() for t in filters.get("job_types", [])]

    seen      = _load_seen()       # set of job IDs already processed
    seen_keys: set[str] = set()    # normalised title+company dedup (cross-source)
    all_jobs: list[dict] = []

    fetchers = [
        ("LinkedIn", _fetch_linkedin),
        ("Shine",    _fetch_shine),
    ]

    for query in search_cfg["queries"]:
        for location in search_cfg["locations"]:
            if len(all_jobs) >= max_jobs:
                break
            loc    = location.replace(", India", "").strip()
            li_loc = "India" if "Remote" in location else loc

            for src_name, fetcher in fetchers:
                if len(all_jobs) >= max_jobs:
                    break
                logger.info(f"[{src_name}] '{query}' in '{loc}'")
                raw = fetcher(query, li_loc if src_name == "LinkedIn" else loc,
                              **({} if src_name == "Shine" else {"days": days}))
                time.sleep(0.4)

                for job in raw:
                    if not job["title"] or job["id"] in seen:
                        continue

                    # Cross-source dedup: same role at same company from LinkedIn + Shine
                    nk = _norm_key(job["title"], job["company"])
                    if nk and nk in seen_keys:
                        logger.debug(f"Dedup (title+company): {job['title']} @ {job['company']}")
                        seen.add(job["id"])   # mark ID so we skip it in future runs too
                        continue

                    # Keyword exclude filter
                    text = (job["title"] + " " + job.get("description", "")[:300]).lower()
                    if any(kw in text for kw in exclude):
                        continue

                    # Experience filter
                    if job.get("experience"):
                        nums = re.findall(r"\d+", job["experience"])
                        if nums and (int(nums[0]) < min_exp or int(nums[0]) > max_exp):
                            continue

                    # Job type filter
                    if job_types == ["remote"] and not job.get("is_remote", False):
                        continue

                    all_jobs.append(job)
                    seen.add(job["id"])
                    if nk:
                        seen_keys.add(nk)

                    if len(all_jobs) >= max_jobs:
                        break

    _save_seen(seen)
    logger.info(f"Total fetched: {len(all_jobs)} new jobs (cross-source dedup active)")
    return all_jobs
