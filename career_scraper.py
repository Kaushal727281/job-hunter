# -*- coding: utf-8 -*-
"""
career_scraper.py
-----------------
Career site scraper and validator for companies listed in company_database.py.

Supports ATS platforms:
  - Greenhouse  : public JSON API  (boards-api.greenhouse.io)
  - Lever       : public JSON API  (api.lever.co)
  - Workday     : POST-based search API  (*.myworkdayjobs.com)
  - iCIMS       : careers-{company}.icims.com HTML scrape
  - SuccessFactors : SAP-hosted or custom SF endpoint
  - SmartRecruiters: public postings API
  - Naukri / LinkedIn: aggregator, skipped for direct scraping
  - Custom      : best-effort HTML link counting

Usage:
    python career_scraper.py --validate            # check all career URLs
    python career_scraper.py --count               # estimate total available jobs
    python career_scraper.py --scrape              # fetch actual job listings
    python career_scraper.py --scrape --tier 1 --type product --output jobs.json
"""

import argparse
import hashlib
import json
import logging
import re
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Dict, List, Optional

import truststore
import requests
from bs4 import BeautifulSoup

import company_database as cdb

# ── Bootstrap ───────────────────────────────────────────────────────────────

truststore.inject_into_ssl()

import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(name)s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

# ── Constants ────────────────────────────────────────────────────────────────

_TIMEOUT = 10          # seconds per HTTP request
_RATE_SLEEP = 0.5      # seconds between requests to the same domain
_SSL_VERIFY = False    # set False if behind a corporate Zscaler / MITM proxy

_BASE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/html, */*",
    "Accept-Language": "en-US,en;q=0.9",
}

# Keywords used for counting job links on custom career pages
_JOB_LINK_KEYWORDS = {"job", "position", "opening", "career", "role", "vacancy", "requisition"}

# Patterns for "Showing X jobs" or "X open positions" style counts
_COUNT_PATTERNS = [
    re.compile(r"(\d[\d,]*)\s+(?:open\s+)?(?:job|position|opening|role|vacanc)", re.I),
    re.compile(r"showing\s+(\d[\d,]*)", re.I),
    re.compile(r"(\d[\d,]*)\s+result", re.I),
]

# Aggregator ATS types — no direct API, skip scraping
_AGGREGATOR_ATS = {"naukri", "linkedin"}

# ── Session factory ──────────────────────────────────────────────────────────

def _make_session(extra_headers: Optional[dict] = None) -> requests.Session:
    """Return a requests.Session with browser-like headers."""
    sess = requests.Session()
    sess.headers.update(_BASE_HEADERS)
    sess.verify = _SSL_VERIFY
    if extra_headers:
        sess.headers.update(extra_headers)
    return sess


# ── Helpers ──────────────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _make_job_id(source: str, raw_id: str) -> str:
    """Produce a stable hex job_id from source + raw identifier."""
    return hashlib.md5(f"{source}::{raw_id}".encode()).hexdigest()


def _domain_of(url: str) -> str:
    """Extract netloc (e.g. boards-api.greenhouse.io) from a URL."""
    try:
        return urllib.parse.urlparse(url).netloc.lower()
    except Exception:
        return url


def _match_keywords(title: str, keywords: List[str]) -> bool:
    """Return True if any keyword appears in the job title (case-insensitive)."""
    if not keywords:
        return True
    low = title.lower()
    return any(kw.lower() in low for kw in keywords)


def _match_location(location: str, loc_filter: Optional[str]) -> bool:
    """Return True if loc_filter is None or appears in the job location."""
    if not loc_filter:
        return True
    return loc_filter.lower() in location.lower()


def _extract_count_from_html(html: str) -> int:
    """
    Try to find a job count by scanning common patterns in page text.
    Returns -1 if no count found.
    """
    for pat in _COUNT_PATTERNS:
        m = pat.search(html)
        if m:
            try:
                return int(m.group(1).replace(",", ""))
            except ValueError:
                pass
    return -1


def _count_job_links(soup: BeautifulSoup) -> int:
    """
    Count anchor tags whose href or text contain job-related keywords.
    Used as a fallback count on custom career pages.
    """
    count = 0
    for a in soup.find_all("a", href=True):
        href = (a.get("href") or "").lower()
        text = a.get_text(separator=" ").lower()
        if any(kw in href or kw in text for kw in _JOB_LINK_KEYWORDS):
            count += 1
    return count


def _discover_greenhouse_token(company_name: str) -> List[str]:
    """
    Generate candidate Greenhouse board tokens from a company name.
    Greenhouse tokens are typically the company name in lowercase with hyphens.
    Returns a list of candidates to try, most-likely first.
    """
    base = company_name.lower().strip()
    # Remove common legal suffixes
    for suffix in (" inc", " corp", " ltd", " llc", " limited", " technologies",
                   " solutions", " software", " systems", " (microsoft)", " (salesforce)"):
        base = base.replace(suffix, "")
    base = base.strip()
    slug = re.sub(r"[^a-z0-9]+", "-", base).strip("-")
    no_hyphen = slug.replace("-", "")
    return [slug, no_hyphen]


# ── ATS-specific scrapers ────────────────────────────────────────────────────

def _scrape_greenhouse(company: dict, keywords: List[str], location: Optional[str],
                       sess: requests.Session) -> List[dict]:
    """
    Fetch jobs via Greenhouse public board API.
    Tries to derive the board token from career_url; falls back to _discover_greenhouse_token.
    """
    career_url = company.get("career_url", "")
    company_name = company["name"]

    # Try to extract token from URL patterns like:
    #   https://boards.greenhouse.io/atlassian
    #   https://boards.greenhouse.io/embed/job_board?for=atlassian
    token = None
    m = re.search(r"greenhouse\.io/(?:embed/job_board\?for=)?([A-Za-z0-9_-]+)", career_url)
    if m:
        token = m.group(1).lower().strip()

    candidates = [token] if token else []
    candidates += _discover_greenhouse_token(company_name)
    candidates = list(dict.fromkeys(candidates))  # dedup, preserve order

    jobs = []
    for candidate in candidates:
        url = f"https://boards-api.greenhouse.io/v1/boards/{candidate}/jobs?content=true"
        try:
            resp = sess.get(url, timeout=_TIMEOUT)
            if resp.status_code == 200:
                data = resp.json()
                raw_jobs = data.get("jobs", [])
                logger.debug(f"  [greenhouse:{candidate}] {len(raw_jobs)} total jobs found")
                for j in raw_jobs:
                    title = j.get("title", "")
                    loc = j.get("location", {}).get("name", "")
                    if not _match_keywords(title, keywords):
                        continue
                    if not _match_location(loc, location):
                        continue
                    jobs.append({
                        "job_id": _make_job_id(f"greenhouse:{candidate}", str(j.get("id", title))),
                        "title": title,
                        "company": company_name,
                        "location": loc,
                        "url": j.get("absolute_url", career_url),
                        "source": f"greenhouse:{candidate}",
                        "date_posted": j.get("updated_at", _now_iso())[:10],
                        "description": (j.get("content") or "")[:500],
                        "tier": company.get("tier", 0),
                        "company_type": company.get("company_type", ""),
                    })
                return jobs  # success, stop trying tokens
            elif resp.status_code == 404:
                logger.debug(f"  [greenhouse] token '{candidate}' not found (404)")
        except Exception as e:
            logger.debug(f"  [greenhouse:{candidate}] error: {e}")
        time.sleep(_RATE_SLEEP)

    logger.warning(f"  [greenhouse] Could not resolve board token for '{company_name}'")
    return jobs


def _count_greenhouse(career_url: str, company_name: str,
                      sess: requests.Session) -> int:
    """Count total jobs on a Greenhouse board (for validate/count modes)."""
    m = re.search(r"greenhouse\.io/(?:embed/job_board\?for=)?([A-Za-z0-9_-]+)", career_url)
    token = m.group(1).lower().strip() if m else None
    candidates = [token] if token else []
    candidates += _discover_greenhouse_token(company_name)
    candidates = list(dict.fromkeys(candidates))

    for candidate in candidates:
        url = f"https://boards-api.greenhouse.io/v1/boards/{candidate}/jobs"
        try:
            resp = sess.get(url, timeout=_TIMEOUT)
            if resp.status_code == 200:
                return len(resp.json().get("jobs", []))
        except Exception:
            pass
        time.sleep(_RATE_SLEEP)
    return -1


def _scrape_lever(company: dict, keywords: List[str], location: Optional[str],
                  sess: requests.Session) -> List[dict]:
    """
    Fetch jobs via Lever public postings API.
    Token is typically the company slug visible in the career URL.
    """
    career_url = company.get("career_url", "")
    company_name = company["name"]

    # lever.co/companyslug or jobs.lever.co/companyslug
    token = None
    m = re.search(r"lever\.co/([A-Za-z0-9_-]+)", career_url)
    if m:
        token = m.group(1).lower().strip()

    # Fallback: derive from company name
    if not token:
        slug = re.sub(r"[^a-z0-9]+", "-", company_name.lower()).strip("-")
        token = slug

    url = f"https://api.lever.co/v0/postings/{token}?mode=json"
    jobs = []
    try:
        resp = sess.get(url, timeout=_TIMEOUT)
        if resp.status_code == 200:
            raw_jobs = resp.json()
            logger.debug(f"  [lever:{token}] {len(raw_jobs)} total jobs found")
            for j in raw_jobs:
                title = j.get("text", "")
                loc = j.get("categories", {}).get("location", "")
                if not _match_keywords(title, keywords):
                    continue
                if not _match_location(loc, location):
                    continue
                jobs.append({
                    "job_id": _make_job_id(f"lever:{token}", j.get("id", title)),
                    "title": title,
                    "company": company_name,
                    "location": loc,
                    "url": j.get("hostedUrl", career_url),
                    "source": f"lever:{token}",
                    "date_posted": datetime.fromtimestamp(
                        j.get("createdAt", 0) / 1000, tz=timezone.utc
                    ).strftime("%Y-%m-%d") if j.get("createdAt") else _now_iso()[:10],
                    "description": j.get("descriptionPlain", "")[:500],
                    "tier": company.get("tier", 0),
                    "company_type": company.get("company_type", ""),
                })
    except Exception as e:
        logger.warning(f"  [lever:{token}] error: {e}")
    return jobs


def _count_lever(career_url: str, company_name: str, sess: requests.Session) -> int:
    m = re.search(r"lever\.co/([A-Za-z0-9_-]+)", career_url)
    token = m.group(1).lower().strip() if m else re.sub(r"[^a-z0-9]+", "-", company_name.lower()).strip("-")
    url = f"https://api.lever.co/v0/postings/{token}?mode=json"
    try:
        resp = sess.get(url, timeout=_TIMEOUT)
        if resp.status_code == 200:
            return len(resp.json())
    except Exception:
        pass
    return -1


def _scrape_workday(company: dict, keywords: List[str], location: Optional[str],
                    sess: requests.Session) -> List[dict]:
    """
    Fetch jobs via Workday's undocumented CXS search API.
    The career_url in company_database already contains the Workday subdomain
    when ats_type == "workday", e.g.:
      https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite
    """
    career_url = company.get("career_url", "")
    company_name = company["name"]

    # Parse Workday URL: https://{tenant}.wd{n}.myworkdayjobs.com/{site_path}
    m = re.match(
        r"https?://([^.]+)\.(wd\d+)\.myworkdayjobs\.com/(?:en-US/)?([^/?#]+)",
        career_url, re.I
    )
    if not m:
        logger.warning(f"  [workday] Cannot parse Workday URL for '{company_name}': {career_url}")
        return []

    tenant, wd_instance, site_path = m.group(1), m.group(2), m.group(3)
    api_url = (
        f"https://{tenant}.{wd_instance}.myworkdayjobs.com"
        f"/wday/cxs/{tenant}/{site_path}/jobs"
    )

    limit = 20
    offset = 0
    jobs = []
    total_remote = None

    while True:
        payload = {
            "limit": limit,
            "offset": offset,
            "searchText": keywords[0] if keywords else "",
            "locations": [],
        }
        try:
            resp = sess.post(
                api_url,
                json=payload,
                headers={"Content-Type": "application/json"},
                timeout=_TIMEOUT,
            )
            if resp.status_code != 200:
                logger.warning(f"  [workday:{tenant}] HTTP {resp.status_code}")
                break
            data = resp.json()
            if total_remote is None:
                total_remote = data.get("total", 0)
                logger.debug(f"  [workday:{tenant}] total={total_remote}")
            postings = data.get("jobPostings", [])
            if not postings:
                break
            for j in postings:
                title = j.get("title", "")
                loc = j.get("locationsText", "")
                if not _match_keywords(title, keywords):
                    continue
                if not _match_location(loc, location):
                    continue
                ext_id = j.get("externalPath", title)
                jobs.append({
                    "job_id": _make_job_id(f"workday:{tenant}", ext_id),
                    "title": title,
                    "company": company_name,
                    "location": loc,
                    "url": career_url.rstrip("/") + j.get("externalPath", ""),
                    "source": f"workday:{tenant}",
                    "date_posted": j.get("postedOn", _now_iso()[:10])[:10],
                    "description": j.get("jobDescription", {}).get("item", "")[:500],
                    "tier": company.get("tier", 0),
                    "company_type": company.get("company_type", ""),
                })
            offset += len(postings)
            if total_remote and offset >= total_remote:
                break
            # Only page up to 200 jobs (first 10 pages) to stay polite
            if offset >= 200:
                break
            time.sleep(_RATE_SLEEP)
        except Exception as e:
            logger.warning(f"  [workday:{tenant}] error: {e}")
            break

    return jobs


def _count_workday(career_url: str, company_name: str, sess: requests.Session) -> int:
    m = re.match(
        r"https?://([^.]+)\.(wd\d+)\.myworkdayjobs\.com/(?:en-US/)?([^/?#]+)",
        career_url, re.I
    )
    if not m:
        return -1
    tenant, wd_instance, site_path = m.group(1), m.group(2), m.group(3)
    api_url = (
        f"https://{tenant}.{wd_instance}.myworkdayjobs.com"
        f"/wday/cxs/{tenant}/{site_path}/jobs"
    )
    try:
        resp = sess.post(
            api_url,
            json={"limit": 1, "offset": 0, "searchText": "", "locations": []},
            headers={"Content-Type": "application/json"},
            timeout=_TIMEOUT,
        )
        if resp.status_code == 200:
            return resp.json().get("total", -1)
    except Exception:
        pass
    return -1


def _scrape_smartrecruiters(company: dict, keywords: List[str], location: Optional[str],
                             sess: requests.Session) -> List[dict]:
    """Fetch jobs via SmartRecruiters public postings API."""
    career_url = company.get("career_url", "")
    company_name = company["name"]

    # Extract company_id from URL like https://careers.smartrecruiters.com/CompanyId
    company_id = None
    m = re.search(r"smartrecruiters\.com/([A-Za-z0-9_-]+)", career_url)
    if m:
        company_id = m.group(1)
    if not company_id:
        company_id = re.sub(r"[^A-Za-z0-9]+", "", company_name)

    url = f"https://api.smartrecruiters.com/v1/companies/{company_id}/postings?limit=100"
    jobs = []
    try:
        resp = sess.get(url, timeout=_TIMEOUT)
        if resp.status_code == 200:
            data = resp.json()
            raw_jobs = data.get("content", [])
            logger.debug(f"  [smartrecruiters:{company_id}] {len(raw_jobs)} jobs")
            for j in raw_jobs:
                title = j.get("name", "")
                loc_data = j.get("location", {})
                loc = ", ".join(filter(None, [
                    loc_data.get("city", ""),
                    loc_data.get("country", ""),
                ]))
                if not _match_keywords(title, keywords):
                    continue
                if not _match_location(loc, location):
                    continue
                jobs.append({
                    "job_id": _make_job_id(f"smartrecruiters:{company_id}", j.get("id", title)),
                    "title": title,
                    "company": company_name,
                    "location": loc,
                    "url": j.get("ref", career_url),
                    "source": f"smartrecruiters:{company_id}",
                    "date_posted": j.get("releasedDate", _now_iso())[:10],
                    "description": "",
                    "tier": company.get("tier", 0),
                    "company_type": company.get("company_type", ""),
                })
    except Exception as e:
        logger.warning(f"  [smartrecruiters:{company_id}] error: {e}")
    return jobs


def _count_smartrecruiters(career_url: str, company_name: str, sess: requests.Session) -> int:
    m = re.search(r"smartrecruiters\.com/([A-Za-z0-9_-]+)", career_url)
    company_id = m.group(1) if m else re.sub(r"[^A-Za-z0-9]+", "", company_name)
    url = f"https://api.smartrecruiters.com/v1/companies/{company_id}/postings?limit=1"
    try:
        resp = sess.get(url, timeout=_TIMEOUT)
        if resp.status_code == 200:
            return resp.json().get("totalFound", -1)
    except Exception:
        pass
    return -1


def _scrape_icims(company: dict, keywords: List[str], location: Optional[str],
                  sess: requests.Session) -> List[dict]:
    """
    iCIMS — scrape the HTML job search page.
    URL pattern: https://careers-{company}.icims.com/jobs/search
    """
    career_url = company.get("career_url", "")
    company_name = company["name"]

    # Attempt to derive subdomain
    m = re.search(r"careers-([^.]+)\.icims\.com", career_url)
    slug = m.group(1) if m else re.sub(r"[^a-z0-9]+", "-", company_name.lower()).strip("-")

    search_url = f"https://careers-{slug}.icims.com/jobs/search?ss=1&searchId=&in_iframe=1"
    jobs = []
    try:
        resp = sess.get(search_url, timeout=_TIMEOUT)
        if resp.status_code == 200:
            soup = BeautifulSoup(resp.text, "html.parser")
            for item in soup.select(".iCIMS_JobsTable .iCIMS_JobsTableRow, .iCIMS_Anchor"):
                title_el = item.select_one(".iCIMS_JobTitle, a")
                if not title_el:
                    continue
                title = title_el.get_text(strip=True)
                loc_el = item.select_one(".iCIMS_JobLocation")
                loc = loc_el.get_text(strip=True) if loc_el else ""
                href = title_el.get("href", career_url)
                if not title or not _match_keywords(title, keywords):
                    continue
                if not _match_location(loc, location):
                    continue
                jobs.append({
                    "job_id": _make_job_id(f"icims:{slug}", href + title),
                    "title": title,
                    "company": company_name,
                    "location": loc,
                    "url": href if href.startswith("http") else f"https://careers-{slug}.icims.com{href}",
                    "source": f"icims:{slug}",
                    "date_posted": _now_iso()[:10],
                    "description": "",
                    "tier": company.get("tier", 0),
                    "company_type": company.get("company_type", ""),
                })
    except Exception as e:
        logger.warning(f"  [icims:{slug}] error: {e}")
    return jobs


def _scrape_successfactors(company: dict, keywords: List[str], location: Optional[str],
                            sess: requests.Session) -> List[dict]:
    """
    SAP SuccessFactors — best-effort HTML scrape of the career portal.
    SF URLs typically look like: https://{company}.jobs.sapjobs.com
    or https://jobs.sap.com for SAP itself.
    """
    career_url = company.get("career_url", "")
    company_name = company["name"]

    jobs = []
    try:
        resp = sess.get(career_url, timeout=_TIMEOUT)
        if resp.status_code == 200:
            soup = BeautifulSoup(resp.text, "html.parser")
            count = _extract_count_from_html(resp.text)
            # SF renders jobs via JavaScript; we can only count from page text
            if count == -1:
                count = _count_job_links(soup)
            logger.debug(f"  [successfactors:{company_name}] estimated {count} jobs (HTML scrape)")
            # Return empty list — full scrape requires JS rendering
    except Exception as e:
        logger.warning(f"  [successfactors:{company_name}] error: {e}")
    return jobs  # JS-heavy, return empty list (count-only in validate mode)


def _scrape_custom(company: dict, keywords: List[str], location: Optional[str],
                   sess: requests.Session) -> List[dict]:
    """
    Custom career pages — best-effort HTML link counting.
    Returns an empty list (we can count but not reliably parse job dicts without
    page-specific selectors).
    """
    career_url = company.get("career_url", "")
    company_name = company["name"]
    try:
        resp = sess.get(career_url, timeout=_TIMEOUT)
        if resp.status_code == 200:
            soup = BeautifulSoup(resp.text, "html.parser")
            count = _extract_count_from_html(resp.text)
            if count == -1:
                count = _count_job_links(soup)
            logger.debug(f"  [custom:{company_name}] estimated {count} jobs (HTML)")
    except Exception as e:
        logger.debug(f"  [custom:{company_name}] error: {e}")
    return []  # structured job extraction not supported for arbitrary custom pages


# ── Dispatcher ───────────────────────────────────────────────────────────────

_SCRAPERS = {
    "greenhouse":     _scrape_greenhouse,
    "lever":          _scrape_lever,
    "workday":        _scrape_workday,
    "icims":          _scrape_icims,
    "successfactors": _scrape_successfactors,
    "smartrecruiters": _scrape_smartrecruiters,
    "custom":         _scrape_custom,
}

_COUNTERS = {
    "greenhouse":     _count_greenhouse,
    "lever":          _count_lever,
    "workday":        _count_workday,
    "smartrecruiters": _count_smartrecruiters,
}


# ── Public API ────────────────────────────────────────────────────────────────

def scrape_company_jobs(company: dict, keywords: Optional[List[str]] = None,
                        location: Optional[str] = None) -> List[dict]:
    """
    Scrape jobs from a single company's career page.

    Returns a list of job dicts compatible with job_store.py:
        {
            "job_id": str,       # stable MD5 hex ID
            "title": str,
            "company": str,
            "location": str,
            "url": str,
            "source": str,       # e.g. "greenhouse:atlassian"
            "date_posted": str,  # YYYY-MM-DD
            "description": str,
            "tier": int,
            "company_type": str  # "product" or "service"
        }
    """
    ats = company.get("ats_type", "custom").lower()
    keywords = keywords or []

    if ats in _AGGREGATOR_ATS:
        logger.info(f"  [{company['name']}] ats_type={ats} is aggregator — skipping direct scrape")
        return []

    sess = _make_session()
    scraper = _SCRAPERS.get(ats, _scrape_custom)

    logger.info(f"  Scraping [{ats}] {company['name']} ...")
    try:
        jobs = scraper(company, keywords, location, sess)
        logger.info(f"  [{company['name']}] => {len(jobs)} jobs matched")
        return jobs
    except Exception as e:
        logger.error(f"  [{company['name']}] Unhandled error: {e}")
        return []


def scrape_all_companies(
    tier_filter: Optional[List[int]] = None,
    type_filter: Optional[str] = None,
    keywords: Optional[List[str]] = None,
    location: Optional[str] = None,
    max_workers: int = 5,
) -> List[dict]:
    """
    Scrape jobs from all (or filtered) companies in company_database.

    Parameters
    ----------
    tier_filter  : list of tiers to include, e.g. [1, 2]. None = all tiers.
    type_filter  : "product" or "service". None = both.
    keywords     : title keyword filters.
    location     : location substring filter.
    max_workers  : concurrent threads (be polite — default 5).

    Returns combined list of job dicts.
    """
    companies = cdb.get_companies()

    if tier_filter:
        companies = [c for c in companies if c.get("tier") in tier_filter]
    if type_filter:
        companies = [c for c in companies if c.get("company_type") == type_filter]

    logger.info(f"Scraping {len(companies)} companies (workers={max_workers}) ...")
    all_jobs: List[dict] = []

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {
            pool.submit(scrape_company_jobs, co, keywords, location): co
            for co in companies
        }
        for i, fut in enumerate(as_completed(futures), 1):
            co = futures[fut]
            try:
                jobs = fut.result()
                all_jobs.extend(jobs)
            except Exception as e:
                logger.error(f"  [{co['name']}] future error: {e}")
            if i % 50 == 0:
                logger.info(f"  Progress: {i}/{len(companies)} companies done, {len(all_jobs)} jobs so far")

    logger.info(f"Done. Total jobs collected: {len(all_jobs)}")
    return all_jobs


def validate_career_urls(
    companies: Optional[List[dict]] = None,
    max_workers: int = 10,
) -> Dict[str, dict]:
    """
    Hit all career URLs concurrently and check reachability + job count.

    Returns dict keyed by company name:
        {
            "company_name": {
                "url": str,
                "status": "ok" | "error" | "redirect",
                "job_count": int,   # -1 if unknown
                "http_code": int,
                "error": str | None,
                "ats_type": str,
            }
        }
    """
    if companies is None:
        companies = cdb.get_companies()

    results: Dict[str, dict] = {}

    def _check_one(company: dict) -> tuple:
        name = company["name"]
        url = company.get("career_url", "")
        ats = company.get("ats_type", "custom").lower()
        sess = _make_session()
        entry = {
            "url": url,
            "status": "error",
            "job_count": -1,
            "http_code": 0,
            "error": None,
            "ats_type": ats,
        }
        if not url:
            entry["error"] = "No career_url defined"
            return name, entry

        if ats in _AGGREGATOR_ATS:
            entry["status"] = "aggregator"
            entry["error"] = f"Aggregator ({ats}) — direct count not supported"
            return name, entry

        try:
            resp = sess.get(url, timeout=_TIMEOUT, allow_redirects=True)
            entry["http_code"] = resp.status_code
            if resp.status_code == 200:
                entry["status"] = "ok"
            elif 300 <= resp.status_code < 400:
                entry["status"] = "redirect"
            else:
                entry["status"] = "error"
                entry["error"] = f"HTTP {resp.status_code}"
                return name, entry

            # Try to get a job count via ATS API
            counter = _COUNTERS.get(ats)
            if counter:
                try:
                    entry["job_count"] = counter(url, name, sess)
                except Exception as ce:
                    logger.debug(f"  [{name}] count error: {ce}")
            else:
                # Fallback: count from HTML
                try:
                    soup = BeautifulSoup(resp.text, "html.parser")
                    html_count = _extract_count_from_html(resp.text)
                    entry["job_count"] = html_count if html_count >= 0 else _count_job_links(soup)
                except Exception:
                    pass

        except requests.exceptions.SSLError as e:
            entry["error"] = f"SSL error: {e}"
        except requests.exceptions.Timeout:
            entry["error"] = "Timeout"
        except requests.exceptions.ConnectionError as e:
            entry["error"] = f"Connection error: {e}"
        except Exception as e:
            entry["error"] = str(e)

        return name, entry

    logger.info(f"Validating {len(companies)} career URLs (workers={max_workers}) ...")
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(_check_one, co): co for co in companies}
        for i, fut in enumerate(as_completed(futures), 1):
            name, entry = fut.result()
            results[name] = entry
            status_sym = {"ok": "OK", "error": "ERR", "redirect": "RDR", "aggregator": "AGG"}.get(
                entry["status"], "???"
            )
            jobs_str = str(entry["job_count"]) if entry["job_count"] >= 0 else "?"
            logger.info(
                f"  [{i:4d}/{len(companies)}] [{status_sym}] {name:<40s} "
                f"jobs={jobs_str:<6s} {entry.get('error') or ''}"
            )

    return results


def count_total_available_jobs(sample_size: int = 100) -> dict:
    """
    Sample companies to estimate total jobs available across all companies.

    Sampling strategy:
    - Take up to `sample_size` companies, stratified by tier and ats_type.
    - Extrapolate to the full population.

    Returns:
        {
            "sampled": int,
            "total_companies": int,
            "total_estimated": int,
            "avg_jobs_per_company": float,
            "by_tier": {1: count, 2: count, 3: count},
            "by_ats": {"greenhouse": count, ...},
            "sample_results": {company_name: job_count, ...},
        }
    """
    all_companies = cdb.get_companies()
    total_companies = len(all_companies)

    # Stratified sample: up to sample_size companies
    import random
    random.seed(42)
    sample = random.sample(all_companies, min(sample_size, total_companies))

    logger.info(f"Counting jobs: sampling {len(sample)}/{total_companies} companies ...")
    validation = validate_career_urls(companies=sample, max_workers=10)

    by_tier: Dict[int, int] = {}
    by_ats: Dict[str, int] = {}
    sample_results: Dict[str, int] = {}
    total_in_sample = 0
    known_count = 0

    for name, entry in validation.items():
        cnt = entry.get("job_count", -1)
        sample_results[name] = cnt

        # Find the company dict to get tier/ats
        co = next((c for c in sample if c["name"] == name), None)
        if not co:
            continue

        tier = co.get("tier", 0)
        ats = co.get("ats_type", "custom")

        if cnt >= 0:
            total_in_sample += cnt
            known_count += 1
            by_tier[tier] = by_tier.get(tier, 0) + cnt
            by_ats[ats] = by_ats.get(ats, 0) + cnt

    avg = total_in_sample / max(known_count, 1)
    total_estimated = int(avg * total_companies)

    return {
        "sampled": len(sample),
        "total_companies": total_companies,
        "total_estimated": total_estimated,
        "avg_jobs_per_company": round(avg, 1),
        "by_tier": by_tier,
        "by_ats": by_ats,
        "sample_results": sample_results,
    }


# ── CLI ───────────────────────────────────────────────────────────────────────

def _print_validate_table(results: Dict[str, dict]):
    """Print a formatted table of validation results to stdout."""
    # Header
    col_name = 38
    col_ats  = 15
    col_stat = 8
    col_jobs = 8
    col_err  = 40
    sep = "-" * (col_name + col_ats + col_stat + col_jobs + col_err + 16)
    header = (
        f"{'Company':<{col_name}}  {'ATS':<{col_ats}}  {'Status':<{col_stat}}  "
        f"{'Jobs':>{col_jobs}}  {'Error / Note':<{col_err}}"
    )
    print()
    print(header)
    print(sep)

    # Rows sorted by status then name
    order = {"error": 0, "redirect": 1, "ok": 2, "aggregator": 3}
    for name, entry in sorted(results.items(), key=lambda x: (order.get(x[1]["status"], 9), x[0])):
        status = entry["status"]
        jobs = str(entry["job_count"]) if entry["job_count"] >= 0 else "?"
        err = (entry.get("error") or "")[:col_err]
        print(
            f"{name:<{col_name}}  {entry['ats_type']:<{col_ats}}  {status:<{col_stat}}  "
            f"{jobs:>{col_jobs}}  {err:<{col_err}}"
        )

    print(sep)
    ok_count = sum(1 for e in results.values() if e["status"] == "ok")
    err_count = sum(1 for e in results.values() if e["status"] == "error")
    total_jobs = sum(e["job_count"] for e in results.values() if e["job_count"] >= 0)
    print(f"\nSummary: {ok_count} OK, {err_count} errors, {total_jobs:,} total known jobs")
    print()


def _print_count_summary(summary: dict):
    """Print the count_total_available_jobs summary."""
    print()
    print("=" * 55)
    print("  Job Count Estimation")
    print("=" * 55)
    print(f"  Companies sampled      : {summary['sampled']}")
    print(f"  Total companies in DB  : {summary['total_companies']}")
    print(f"  Avg jobs per company   : {summary['avg_jobs_per_company']}")
    print(f"  ESTIMATED total jobs   : {summary['total_estimated']:,}")
    print()
    print("  By Tier:")
    for tier, cnt in sorted(summary["by_tier"].items()):
        print(f"    Tier {tier}: {cnt:,} jobs (in sample)")
    print()
    print("  By ATS:")
    for ats, cnt in sorted(summary["by_ats"].items(), key=lambda x: -x[1]):
        print(f"    {ats:<20s}: {cnt:,} jobs (in sample)")
    print("=" * 55)
    print()


def _print_jobs_table(jobs: List[dict]):
    """Print scraped jobs in a compact table."""
    print()
    print(f"{'#':<5} {'Title':<45} {'Company':<30} {'Location':<25} {'Date':<12}")
    print("-" * 120)
    for i, j in enumerate(jobs, 1):
        print(
            f"{i:<5} {j['title'][:44]:<45} {j['company'][:29]:<30} "
            f"{j['location'][:24]:<25} {j['date_posted']:<12}"
        )
    print("-" * 120)
    print(f"\nTotal: {len(jobs)} jobs\n")


def main():
    parser = argparse.ArgumentParser(
        description="Career site scraper / validator for companies in company_database.py"
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--validate", action="store_true",
                      help="Check all career URLs and report reachability + job counts")
    mode.add_argument("--count", action="store_true",
                      help="Estimate total available jobs across all companies (sampling)")
    mode.add_argument("--scrape", action="store_true",
                      help="Fetch actual job listings and optionally save to --output")

    parser.add_argument("--tier", type=int, nargs="+", metavar="N",
                        help="Filter to tier(s): 1 2 3")
    parser.add_argument("--type", dest="company_type", choices=["product", "service"],
                        help="Filter to company type")
    parser.add_argument("--keywords", nargs="+", metavar="KW",
                        help="Title keyword filters (any match)")
    parser.add_argument("--location", metavar="LOC",
                        help="Location substring filter")
    parser.add_argument("--output", metavar="FILE",
                        help="Save scraped jobs to this JSON file")
    parser.add_argument("--sample-size", type=int, default=100, metavar="N",
                        help="Number of companies to sample in --count mode (default: 100)")
    parser.add_argument("--workers", type=int, default=10, metavar="N",
                        help="Concurrent HTTP workers (default: 10)")
    parser.add_argument("--verbose", action="store_true",
                        help="Enable DEBUG-level logging")

    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    if args.validate:
        companies = cdb.get_companies()
        if args.tier:
            companies = [c for c in companies if c.get("tier") in args.tier]
        if args.company_type:
            companies = [c for c in companies if c.get("company_type") == args.company_type]

        results = validate_career_urls(companies=companies, max_workers=args.workers)
        _print_validate_table(results)

        if args.output:
            with open(args.output, "w", encoding="utf-8") as f:
                json.dump(results, f, indent=2, ensure_ascii=False)
            print(f"Results saved to {args.output}")

    elif args.count:
        summary = count_total_available_jobs(sample_size=args.sample_size)
        _print_count_summary(summary)

        if args.output:
            with open(args.output, "w", encoding="utf-8") as f:
                json.dump(summary, f, indent=2, ensure_ascii=False)
            print(f"Summary saved to {args.output}")

    elif args.scrape:
        jobs = scrape_all_companies(
            tier_filter=args.tier,
            type_filter=args.company_type,
            keywords=args.keywords,
            location=args.location,
            max_workers=args.workers,
        )
        _print_jobs_table(jobs)

        if args.output:
            with open(args.output, "w", encoding="utf-8") as f:
                json.dump(jobs, f, indent=2, ensure_ascii=False)
            print(f"Jobs saved to {args.output}")


if __name__ == "__main__":
    main()
