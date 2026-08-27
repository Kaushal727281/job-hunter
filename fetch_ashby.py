#!/usr/bin/env python3
"""
fetch_ashby.py
--------------
Fetches jobs from AshbyHQ career boards via the public Ashby Posting API.

API:
    GET https://api.ashbyhq.com/posting-api/job-board/{board}
    Returns: { "jobPostings": [ { "id", "title", "locationName", "applyUrl", ... } ] }

Usage:
    python3 fetch_ashby.py                          # all ashby companies
    python3 fetch_ashby.py --company "Snowflake"
    python3 fetch_ashby.py --no-score
    python3 fetch_ashby.py --dry-run
"""

import argparse
import hashlib
import logging
import os
import sys
import time

import dotenv
dotenv.load_dotenv(os.path.join(os.path.dirname(__file__), ".env"), override=False)

import requests
import truststore

truststore.inject_into_ssl()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

RATE_SLEEP    = 1.5
TIMEOUT       = 20
LOCATION_KEYS = ("india", "bengaluru", "hyderabad", "pune", "chennai",
                 "noida", "gurugram", "remote", "bangalore")

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
}


def _match_location(loc_str: str) -> bool:
    """Return True if the location string mentions India or is remote."""
    s = (loc_str or "").lower()
    return any(k in s for k in LOCATION_KEYS)


def fetch_company(company: dict, sess: requests.Session, dry_run: bool = False) -> list[dict]:
    board        = company.get("ashby_board", "")
    company_name = company["name"]

    if not board:
        logger.warning(f"[{company_name}] Missing ashby_board — skipping")
        return []

    url = f"https://api.ashbyhq.com/posting-api/job-board/{board}"
    logger.info(f"[{company_name}] GET {url}")

    try:
        r = sess.get(url, timeout=TIMEOUT)
        if r.status_code != 200:
            logger.warning(f"[{company_name}] HTTP {r.status_code}")
            return []
        data = r.json()
    except Exception as exc:
        logger.warning(f"[{company_name}] Request error: {exc}")
        return []

    postings = data.get("jobPostings", [])
    logger.info(f"[{company_name}] {len(postings)} total postings")

    jobs = []
    for p in postings:
        title    = p.get("title", "")
        loc      = p.get("locationName", "") or p.get("location", "")
        is_remote = p.get("isRemote", False)

        if not _match_location(loc) and not is_remote:
            continue

        job_id    = p.get("id", "")
        apply_url = p.get("applyUrl", "") or f"https://jobs.ashbyhq.com/{board}/{job_id}/application"

        uid = hashlib.md5(f"ashby_{board}_{job_id}".encode()).hexdigest()

        jobs.append({
            "id":          f"ashby_{uid[:12]}",
            "title":       title,
            "company":     company_name,
            "location":    loc,
            "apply_link":  apply_url,
            "ats_type":    "ashby",
            "ashby_board": board,
            "ashby_job_id": job_id,
            "description": p.get("descriptionPlain", "") or p.get("description", ""),
            "posted_date": (p.get("publishedAt") or "")[:10],
        })

    logger.info(f"[{company_name}] {len(jobs)} India/remote jobs found")
    if dry_run:
        for j in jobs:
            print(f"  {j['title']} — {j['location']}")
    return jobs


def main():
    parser = argparse.ArgumentParser(description="Fetch jobs from AshbyHQ boards")
    parser.add_argument("--profile",  default=os.environ.get("CANDIDATE_PROFILE_SLUG", ""))
    parser.add_argument("--company",  help="Filter to one company (substring match)")
    parser.add_argument("--no-score", action="store_true", help="Skip AI scoring after fetch")
    parser.add_argument("--dry-run",  action="store_true", help="Print jobs, don't save")
    args = parser.parse_args()

    import profiles
    profiles.set_active_profile(args.profile)
    import job_store
    from company_database import ALL_COMPANIES

    companies = [c for c in ALL_COMPANIES if c.get("ats_type") == "ashby"]
    if args.company:
        needle = args.company.lower()
        companies = [c for c in companies if needle in c["name"].lower()]

    if not companies:
        logger.info("No ashby companies found in database.")
        sys.exit(0)

    sess = requests.Session()
    sess.headers.update(_HEADERS)

    total_new = 0
    for company in companies:
        jobs = fetch_company(company, sess, dry_run=args.dry_run)
        if not args.dry_run:
            for j in jobs:
                if job_store.add_job(j):
                    total_new += 1
        time.sleep(RATE_SLEEP)

    logger.info(f"Ashby fetch complete — {total_new} new jobs added")

    if not args.dry_run and not args.no_score and total_new > 0:
        import job_scorer
        unscored = [j["id"] for j in job_store.all_jobs()
                    if j.get("ats_type") == "ashby" and not j.get("fit_score")]
        if unscored:
            logger.info(f"Scoring {len(unscored)} unscored Ashby jobs…")
            job_scorer.score_jobs(unscored)


if __name__ == "__main__":
    main()
