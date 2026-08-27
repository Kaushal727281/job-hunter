#!/usr/bin/env python3
"""
fetch_phenom.py
---------------
Fetches jobs from Phenom-powered career portals and saves them into
the active profile's job store.

Portals handled:
  - Cognizant (careers.cognizant.com) — Umbraco CMS, SSR HTML parsing
  - Netflix (explore.jobs.netflix.net) — Phenom platform (tries REST API)
  - American Express (aexp.phenompro.com) — Phenom standard API
  - Hitachi Vantara (careers.hitachi.com) — Phenom platform (tries REST API)

Cognizant approach (SSR HTML):
  GET https://careers.cognizant.com/global-en/jobs/?keyword=<kw>&location=<city>
      &radius=100&lat=<lat>&lng=<lng>&cname=<city>&ccode=IN&pagesize=20&page=<n>
  Parses HTML for: href="/global-en/jobs/{id}/{slug}/" + data-jobtitle="{title}"

Phenom standard API (AmEx, Netflix, Hitachi):
  GET {host}/global/en/api/jobs?keyword=<kw>&from=0&size=100&location=India
  Header: x-ph: internal

Usage:
    python fetch_phenom.py                          # all phenom companies
    python fetch_phenom.py --company "Cognizant"    # single company
    python fetch_phenom.py --no-score               # skip AI fit-scoring
    python fetch_phenom.py --dry-run                # fetch but don't save
    python fetch_phenom.py --profile <your-profile-slug>
"""

import argparse
import hashlib
import html as html_lib
import logging
import os
import re
import sys
import time
from datetime import date
from urllib.parse import urljoin

import dotenv
dotenv.load_dotenv(os.path.join(os.path.dirname(__file__), ".env"), override=False)

import truststore
import requests
import urllib3

truststore.inject_into_ssl()
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────

DEFAULT_TITLE_FILTER = [
    "software engineer", "fullstack", "full stack",
    "java", "backend", "platform engineer", "lead engineer", "staff engineer",
    "application development", "development engineer", "software developer",
    "site reliability", "devops", "AI/ML", "machine learning",
]
RATE_SLEEP   = 1.5
TIMEOUT      = 20
MAX_PAGES    = 10   # per city per keyword
PAGE_SIZE    = 20

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# India city geocoords for Cognizant's location radius search
_INDIA_CITIES = [
    ("Bangalore",  12.9716,  77.5946),
    ("Hyderabad",  17.3850,  78.4867),
    ("Chennai",    13.0827,  80.2707),
    ("Pune",       18.5204,  73.8567),
    ("Mumbai",     19.0760,  72.8777),
    ("Kolkata",    22.5726,  88.3639),
    ("Noida",      28.5355,  77.3910),
    ("Gurugram",   28.4595,  77.0266),
    ("Hyderabad",  17.3850,  78.4867),
]


# ── Helpers ───────────────────────────────────────────────────────────────────

def _job_id(company_slug: str, job_id_raw: str) -> str:
    raw = f"phenom:{company_slug}:{job_id_raw}"
    return "ph_" + hashlib.md5(raw.encode()).hexdigest()[:12]


def _match_keywords(title: str, keywords: list) -> bool:
    t = title.lower()
    return any(kw.lower() in t for kw in keywords)


def _estimate_salary(title: str) -> str:
    t = title.lower()
    if any(w in t for w in ["principal", "distinguished", "vp", "vice president"]):
        lo, hi = 78, 143
    elif any(w in t for w in ["lead", "staff", "head", "director"]):
        lo, hi = 46, 91
    elif any(w in t for w in ["senior", "sr."]):
        lo, hi = 23, 52
    else:
        lo, hi = 16, 33
    return f"Rs.{lo}--{hi} LPA (est.)"


def _company_slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


# ── Cognizant (Umbraco CMS SSR) ───────────────────────────────────────────────

def fetch_cognizant(company: dict, title_filter: list, sess: requests.Session) -> list:
    """
    Fetches Cognizant jobs via SSR HTML scraping across Indian cities.
    Deduplicates by job ID (Cognizant job number like 00068036101).
    """
    company_name = company["name"]
    base_url = "https://careers.cognizant.com"
    search_path = "/global-en/jobs/"
    apply_base = base_url + "/global-en/job-details/"

    seen_ids = set()
    jobs = []

    keywords_list = ["software engineer", "java developer", "lead engineer", "staff engineer",
                     "fullstack developer", "backend engineer", "devops", "site reliability"]

    for keyword in keywords_list[:4]:  # limit keywords to avoid too many requests
        for city, lat, lng in _INDIA_CITIES[:5]:
            for page_n in range(1, MAX_PAGES + 1):
                params = {
                    "keyword": keyword,
                    "location": f"{city}, India",
                    "radius": "100",
                    "lat": str(lat),
                    "lng": str(lng),
                    "cname": city,
                    "ccode": "IN",
                    "pagesize": str(PAGE_SIZE),
                    "page": str(page_n),
                }
                try:
                    resp = sess.get(base_url + search_path, params=params, timeout=TIMEOUT)
                    if resp.status_code != 200:
                        logger.debug(f"  [{company_name}] HTTP {resp.status_code} for {city} p{page_n}")
                        break
                    page_html = resp.text
                except Exception as exc:
                    logger.debug(f"  [{company_name}] request error: {exc}")
                    break

                # Extract jobs: href="/global-en/jobs/{id}/{slug}/" with title
                entries = re.findall(
                    r'href="(/global-en/jobs/(\d+)/[^"]+)"[^>]*>([^<]{3,80})</a>',
                    page_html,
                )
                if not entries:
                    break  # no more pages

                found_new = False
                for job_path, raw_id, raw_title in entries:
                    if raw_id in seen_ids:
                        continue
                    seen_ids.add(raw_id)
                    found_new = True

                    title = html_lib.unescape(raw_title.strip())
                    if not _match_keywords(title, title_filter):
                        continue

                    # Extract location from nearby meta list
                    # Look for the meta list after this job href in HTML
                    idx = page_html.find(f'data-id="{raw_id}"')
                    loc_text = ""
                    if idx > 0:
                        meta_snip = page_html[idx:idx+500]
                        meta_items = re.findall(r'<li[^>]*>(.*?)</li>', meta_snip, re.DOTALL)
                        for item in meta_items[:2]:
                            txt = re.sub(r"<[^>]+>", "", item).strip()
                            txt = html_lib.unescape(txt)
                            if txt and "india" in txt.lower():
                                loc_text = txt
                                break
                        if not loc_text and meta_items:
                            loc_text = html_lib.unescape(re.sub(r"<[^>]+>", "", meta_items[0]).strip())

                    apply_link = base_url + job_path

                    jobs.append({
                        "id":              _job_id(_company_slug(company_name), raw_id),
                        "title":           title,
                        "company":         company_name,
                        "location":        loc_text or f"{city}, India",
                        "experience":      "",
                        "is_remote":       "remote" in (loc_text or "").lower(),
                        "salary":          "Not disclosed",
                        "apply_link":      apply_link,
                        "description":     "",
                        "tags":            [],
                        "posted_at":       str(date.today()),
                        "source":          f"phenom:cognizant",
                        "fetched_date":    str(date.today()),
                        "tailor_result":   None,
                        "pdf_path":        None,
                        "company_type":    company.get("company_type", "service"),
                        "company_rating":  3.6,
                        "company_tags":    "",
                        "salary_estimate": _estimate_salary(title),
                        "ats_type":        "phenom",
                        "phenom_host":     base_url,
                        "phenom_job_id":   raw_id,
                    })

                if not found_new:
                    break  # all jobs on this page already seen
                time.sleep(0.5)

            time.sleep(RATE_SLEEP)

    logger.info(f"[{company_name}] {len(jobs)} matching jobs across India")
    return jobs


# ── Phenom standard API (AmEx, Netflix, Hitachi) ─────────────────────────────

def fetch_phenom_api(company: dict, title_filter: list, sess: requests.Session) -> list:
    """
    Tries the standard Phenom REST API: {host}/global/en/api/jobs
    Falls back to /api/jobs and /api/v1/jobs.
    Returns empty list if all endpoints fail.
    """
    company_name = company["name"]
    url_base = company.get("url_base", "")
    career_url = company.get("career_url", "")

    if not url_base:
        logger.warning(f"[{company_name}] No url_base configured")
        return []

    from urllib.parse import urlparse
    parsed = urlparse(url_base)
    host = f"{parsed.scheme}://{parsed.netloc}"

    phenom_headers = dict(sess.headers)
    phenom_headers.update({
        "x-ph": "internal",
        "Accept": "application/json, text/plain, */*",
        "Origin": host,
        "Referer": career_url or host,
    })

    for api_path in ["/global/en/api/jobs", "/api/jobs", "/api/v1/jobs"]:
        api_url = host + api_path
        params = {"keyword": "software engineer", "from": "0", "size": "100", "location": "India"}
        try:
            resp = sess.get(api_url, params=params, headers=phenom_headers, timeout=TIMEOUT)
            if resp.status_code == 200 and "json" in resp.headers.get("Content-Type", ""):
                data = resp.json()
                raw = (data.get("data") or data.get("jobs") or data.get("results")
                       or (data if isinstance(data, list) else []))
                if not raw:
                    continue

                jobs = []
                for j in raw:
                    if not isinstance(j, dict):
                        continue
                    title = j.get("title") or j.get("name") or ""
                    if not title or not _match_keywords(title, title_filter):
                        continue
                    loc = j.get("city") or j.get("location") or ""
                    if isinstance(loc, dict):
                        loc = loc.get("city") or loc.get("name") or ""
                    job_url = j.get("applyUrl") or j.get("url") or career_url
                    job_raw_id = j.get("id") or j.get("jobId") or (job_url + title)

                    jobs.append({
                        "id":              _job_id(_company_slug(company_name), str(job_raw_id)),
                        "title":           title,
                        "company":         company_name,
                        "location":        str(loc) or "India",
                        "experience":      "",
                        "is_remote":       "remote" in str(loc).lower(),
                        "salary":          "Not disclosed",
                        "apply_link":      job_url,
                        "description":     str(j.get("description") or "")[:500],
                        "tags":            [],
                        "posted_at":       (j.get("postedDate") or j.get("date_posted") or str(date.today()))[:10],
                        "source":          f"phenom:{_company_slug(company_name)}",
                        "fetched_date":    str(date.today()),
                        "tailor_result":   None,
                        "pdf_path":        None,
                        "company_type":    company.get("company_type", "product"),
                        "company_rating":  3.8,
                        "company_tags":    "",
                        "salary_estimate": _estimate_salary(title),
                        "ats_type":        "phenom",
                        "phenom_host":     host,
                        "phenom_job_id":   str(job_raw_id),
                    })

                if jobs:
                    logger.info(f"[{company_name}] {len(jobs)} jobs via {api_path}")
                    return jobs

        except Exception as exc:
            logger.debug(f"  [{company_name}] {api_path} failed: {exc}")
            continue

    logger.warning(f"[{company_name}] All Phenom API paths failed — portal may require browser/auth")
    return []


# ── Dispatcher ────────────────────────────────────────────────────────────────

def fetch_phenom_company(company: dict, title_filter: list, sess: requests.Session) -> list:
    """Route to the right fetcher based on company."""
    name_lower = company["name"].lower()

    # Cognizant uses Umbraco CMS (SSR HTML)
    if "cognizant" in name_lower:
        return fetch_cognizant(company, title_filter, sess)

    # Others: try standard Phenom REST API
    return fetch_phenom_api(company, title_filter, sess)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Fetch Phenom-portal jobs into job store")
    parser.add_argument("--company",  help="Filter to single company name (substring match)")
    parser.add_argument("--filter",   default=None, help="Comma-separated title filter words")
    parser.add_argument("--no-score", action="store_true", help="Skip AI fit-scoring")
    parser.add_argument("--dry-run",  action="store_true", help="Fetch and print but don't save")
    parser.add_argument("--profile",  default=os.environ.get("CANDIDATE_PROFILE_SLUG", ""), help="Profile slug")
    args = parser.parse_args()

    import profiles
    profiles.set_active_profile(args.profile)
    import job_store
    import company_database as cdb

    all_companies = (
        cdb.PRODUCT_TIER1 + cdb.PRODUCT_TIER2 + cdb.PRODUCT_TIER3
        + cdb.SERVICE_TIER1 + cdb.SERVICE_TIER2 + cdb.SERVICE_TIER3
    )
    phenom_cos = [c for c in all_companies if c.get("ats_type") == "phenom"]

    # Deduplicate by career_url (Cognizant India / CTS / AI Practice all share same portal)
    seen_urls = set()
    unique_cos = []
    for c in phenom_cos:
        url_key = c.get("url_base") or c.get("career_url", "")
        if url_key not in seen_urls:
            seen_urls.add(url_key)
            unique_cos.append(c)

    if args.company:
        needle = args.company.lower()
        unique_cos = [c for c in unique_cos if needle in c["name"].lower()]
        if not unique_cos:
            logger.error(f"No Phenom company matches '{args.company}'")
            sys.exit(1)

    title_filter = (
        [k.strip() for k in args.filter.split(",") if k.strip()]
        if args.filter else DEFAULT_TITLE_FILTER
    )

    logger.info(f"Fetching Phenom jobs -- {len(unique_cos)} portals")

    sess = requests.Session()
    sess.headers.update(_HEADERS)
    sess.verify = False

    all_jobs = []
    for co in unique_cos:
        logger.info(f"[{co['name']}] fetching...")
        try:
            jobs = fetch_phenom_company(co, title_filter, sess)
            all_jobs.extend(jobs)
        except Exception as exc:
            logger.warning(f"[{co['name']}] unexpected error: {exc}")
        time.sleep(RATE_SLEEP)

    logger.info(f"\nTotal matching jobs fetched: {len(all_jobs)}")

    if args.dry_run:
        for j in all_jobs:
            print(f"  [{j['company']}] {j['title']} -- {j['location']}")
            print(f"    {j['apply_link']}")
        return

    new_ids = job_store.upsert_jobs_return_ids(all_jobs)
    logger.info(f"New jobs added to store: {len(new_ids)}")

    if new_ids and not args.no_score:
        try:
            import job_scorer
            logger.info("Running AI fit-scoring on new jobs...")
            job_scorer.score_jobs(new_ids)
        except Exception as exc:
            logger.warning(f"Fit-scoring skipped: {exc}")

    print("\n" + "=" * 60)
    print(f"  Phenom fetch complete -- {len(new_ids)} new jobs added")
    print(f"  Total fetched: {len(all_jobs)}")
    print("=" * 60)

    if new_ids and not args.no_score:
        scored = sorted(
            [j for j in job_store.all_jobs() if j["id"] in set(new_ids) and j.get("fit_score")],
            key=lambda j: j.get("fit_score", 0), reverse=True,
        )
        if scored:
            print("\nTop matching jobs by fit score:")
            for j in scored[:15]:
                print(f"  [{j.get('fit_score','?')}/10] {j['title']} @ {j['company']}"
                      f" -- {j['location']}")
                print(f"    {j['apply_link']}")


if __name__ == "__main__":
    main()
