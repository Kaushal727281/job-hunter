#!/usr/bin/env python3
"""
fetch_greenhouse.py
-------------------
Fetches jobs from all Greenhouse-ATS companies in company_database.py
and saves them into the active profile's job store.

Greenhouse public API (no auth required):
    GET https://boards.greenhouse.io/v1/boards/{board}/jobs?content=true
    Response: {"jobs": [{id, title, location, absolute_url, updated_at, ...}]}

Board identifier is taken from (in order of priority):
    greenhouse_board  → explicit board slug (e.g. "atlassian", "stripe")
    ats_token         → legacy fallback (e.g. "databricks", "okta")
    career_url        → parsed from boards.greenhouse.io/{board}

Usage:
    python fetch_greenhouse.py                           # all greenhouse companies
    python fetch_greenhouse.py --company "Atlassian"    # single company
    python fetch_greenhouse.py --location "India"       # filter by location (default)
    python fetch_greenhouse.py --no-score               # skip AI fit-scoring
    python fetch_greenhouse.py --dry-run
"""

import argparse
import hashlib
import logging
import re
import sys
import time
from datetime import date

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

# ── Defaults ─────────────────────────────────────────────────────────────────

DEFAULT_LOCATION = "India"
DEFAULT_TITLE_FILTER = [
    "software engineer",
    "fullstack",
    "full stack",
    "java",
    "backend",
    "platform engineer",
    "lead engineer",
    "staff engineer",
    "application development",
    "development engineer",
    "software developer",
    "site reliability",
    "devops",
    "AI/ML",
    "machine learning",
]
RATE_SLEEP = 1.0
TIMEOUT    = 20

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
}

_INDIA_KEYWORDS = {
    "india", "bangalore", "bengaluru", "hyderabad", "pune", "noida",
    "gurugram", "gurgaon", "chennai", "mumbai", "delhi", "remote",
}

# ── Helpers ───────────────────────────────────────────────────────────────────

def _job_id(board: str, job_id) -> str:
    raw = f"gh:{board}:{job_id}"
    return "gh_" + hashlib.md5(raw.encode()).hexdigest()[:12]


def _match_keywords(title: str, keywords: list) -> bool:
    t = title.lower()
    return any(kw.lower() in t for kw in keywords)


def _match_location(loc_name: str, location_filter: str) -> bool:
    """Returns True if the job location matches the location filter."""
    if not location_filter:
        return True
    l = loc_name.lower()
    for part in location_filter.lower().split(","):
        part = part.strip()
        if not part:
            continue
        if part in l:
            return True
        # When filtering "india", also accept remote and Indian cities
        if part == "india" and any(city in l for city in _INDIA_KEYWORDS):
            return True
    return False


def _estimate_salary(title: str) -> str:
    t = title.lower()
    if any(w in t for w in ["principal", "distinguished", "vp", "vice president"]):
        lo, hi = 60, 110
    elif any(w in t for w in ["lead", "staff", "head", "director"]):
        lo, hi = 35, 70
    elif any(w in t for w in ["senior", "sr."]):
        lo, hi = 18, 40
    else:
        lo, hi = 12, 25
    lo = int(lo * 1.3)
    hi = int(hi * 1.3)
    return f"₹{lo}–{hi} LPA (est.)"


def _board_slug(company: dict) -> str | None:
    """Determine the Greenhouse board slug for a company."""
    # Explicit override takes priority
    if company.get("greenhouse_board"):
        return company["greenhouse_board"]
    if company.get("ats_token"):
        return company["ats_token"]
    # Try to parse from career_url: boards.greenhouse.io/{slug}
    url = company.get("career_url", "")
    m = re.search(r"boards\.greenhouse\.io/([^/?#]+)", url)
    if m:
        return m.group(1)
    return None


# ── Greenhouse fetcher ────────────────────────────────────────────────────────

def fetch_greenhouse_company(company: dict, title_filter: list,
                              location_filter: str, sess: requests.Session) -> list:
    """Fetch all jobs for one Greenhouse company board. Returns job-store dicts."""
    company_name = company["name"]
    board = _board_slug(company)

    if not board:
        logger.warning(f"[{company_name}] Cannot determine Greenhouse board slug")
        return []

    api_url = f"https://boards.greenhouse.io/v1/boards/{board}/jobs?content=true"
    logger.info(f"[{company_name}] Fetching {api_url}")

    try:
        resp = sess.get(api_url, timeout=TIMEOUT)
        if resp.status_code == 404:
            logger.warning(f"[{company_name}] 404 — board '{board}' not found")
            return []
        if resp.status_code != 200:
            logger.warning(f"[{company_name}] HTTP {resp.status_code}")
            return []
        data = resp.json()
    except Exception as exc:
        logger.warning(f"[{company_name}] request error: {exc}")
        return []

    all_jobs = data.get("jobs", [])
    logger.info(f"[{company_name}] {len(all_jobs)} total listings on board '{board}'")

    jobs = []
    for j in all_jobs:
        title = j.get("title", "")
        if not title:
            continue

        # Location: Greenhouse returns {"name": "Remote"} or {"name": "Bengaluru, India"}
        loc_obj  = j.get("location", {})
        loc_name = loc_obj.get("name", "") if isinstance(loc_obj, dict) else str(loc_obj)

        # Title filter
        if title_filter and not _match_keywords(title, title_filter):
            continue

        # Location filter
        if location_filter and not _match_location(loc_name, location_filter):
            continue

        is_remote = "remote" in loc_name.lower()
        apply_link = j.get("absolute_url", "")
        posted_at = (j.get("updated_at") or "")[:10]

        jobs.append({
            "id":              _job_id(board, j.get("id", "")),
            "title":           title,
            "company":         company_name,
            "location":        loc_name or location_filter,
            "experience":      "",
            "is_remote":       is_remote,
            "salary":          "Not disclosed",
            "apply_link":      apply_link,
            "description":     "",
            "tags":            [],
            "posted_at":       posted_at,
            "source":          f"greenhouse:{board}",
            "fetched_date":    str(date.today()),
            "tailor_result":   None,
            "pdf_path":        None,
            "company_type":    company.get("company_type", "product"),
            "company_rating":  3.8,
            "company_tags":    "",
            "salary_estimate": _estimate_salary(title),
            "ats_type":        "greenhouse",
            "gh_board":        board,
            "gh_job_id":       str(j.get("id", "")),
        })

    logger.info(f"[{company_name}] → {len(jobs)} matching jobs")
    return jobs


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Fetch Greenhouse jobs into job store")
    parser.add_argument("--company",  help="Filter to single company name (substring match)")
    parser.add_argument("--location", default=DEFAULT_LOCATION,
                        help=f"Location filter (default: '{DEFAULT_LOCATION}')")
    parser.add_argument("--filter",   default=None,
                        help="Comma-separated title filter words")
    parser.add_argument("--no-score", action="store_true", help="Skip AI fit-scoring")
    parser.add_argument("--dry-run",  action="store_true", help="Fetch and print but don't save")
    parser.add_argument("--profile",  default="kaushal-kumar-jha", help="Profile slug")
    args = parser.parse_args()

    import profiles
    profiles.set_active_profile(args.profile)
    import job_store
    import company_database as cdb

    all_companies = (
        cdb.PRODUCT_TIER1 + cdb.PRODUCT_TIER2 + cdb.PRODUCT_TIER3
        + cdb.SERVICE_TIER1 + cdb.SERVICE_TIER2 + cdb.SERVICE_TIER3
    )
    gh_cos = [c for c in all_companies if c.get("ats_type") == "greenhouse"]

    if args.company:
        needle = args.company.lower()
        gh_cos = [c for c in gh_cos if needle in c["name"].lower()]
        if not gh_cos:
            logger.error(f"No Greenhouse company matches '{args.company}'")
            sys.exit(1)

    title_filter = (
        [k.strip() for k in args.filter.split(",") if k.strip()]
        if args.filter else DEFAULT_TITLE_FILTER
    )

    logger.info(f"Fetching Greenhouse jobs — {len(gh_cos)} companies, location={args.location!r}")

    sess = requests.Session()
    sess.headers.update(_HEADERS)
    sess.verify = False

    all_jobs = []
    for co in gh_cos:
        try:
            jobs = fetch_greenhouse_company(co, title_filter, args.location, sess)
            all_jobs.extend(jobs)
        except Exception as exc:
            logger.warning(f"[{co['name']}] unexpected error: {exc}")
        time.sleep(RATE_SLEEP)

    logger.info(f"\nTotal matching jobs fetched: {len(all_jobs)}")

    if args.dry_run:
        for j in all_jobs:
            print(f"  [{j['company']}] {j['title']} — {j['location']}")
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
    print(f"  Greenhouse fetch complete — {len(new_ids)} new jobs added")
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
                      f" — {j['location']}")
                print(f"    {j['apply_link']}")


if __name__ == "__main__":
    main()
