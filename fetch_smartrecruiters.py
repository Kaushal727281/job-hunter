#!/usr/bin/env python3
"""
fetch_smartrecruiters.py
------------------------
Fetches jobs from SmartRecruiters ATS companies in company_database.py
and saves them into the active profile's job store.

SmartRecruiters public jobs API:
    GET https://api.smartrecruiters.com/v1/companies/{slug}/postings
    Query params: q=<keyword>, limit=100, offset=0, country=IN

Usage:
    python fetch_smartrecruiters.py                              # all SR companies
    python fetch_smartrecruiters.py --company "ServiceNow"       # single company
    python fetch_smartrecruiters.py --search "java"              # custom keywords
    python fetch_smartrecruiters.py --no-score                   # skip AI fit-scoring
    python fetch_smartrecruiters.py --dry-run                    # fetch but don't save
"""

import argparse
import hashlib
import logging
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

# ── Config ────────────────────────────────────────────────────────────────────

DEFAULT_SEARCH_TEXT  = "software engineer"
DEFAULT_TITLE_FILTER = [
    "software engineer", "fullstack", "full stack",
    "java", "backend", "platform engineer", "lead engineer", "staff engineer",
]
DEFAULT_COUNTRY  = "in"   # ISO 3166-1 alpha-2 for India (SmartRecruiters uses lowercase)
RATE_SLEEP       = 1.5
TIMEOUT          = 20
MAX_JOBS_PER_CO  = 200

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
}

# ── Helpers ───────────────────────────────────────────────────────────────────

def _job_id(slug: str, posting_id: str) -> str:
    raw = f"sr:{slug}:{posting_id}"
    return "sr_" + hashlib.md5(raw.encode()).hexdigest()[:12]


def _match_keywords(title: str, keywords: list) -> bool:
    t = title.lower()
    return any(kw.lower() in t for kw in keywords)


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


# ── SmartRecruiters API fetcher ───────────────────────────────────────────────

def fetch_sr_company(company: dict, search_text: str, title_filter: list,
                     country: str, sess: requests.Session) -> list:
    """Fetch jobs for one SmartRecruiters company."""
    slug         = company.get("sr_company_slug", "")
    company_name = company["name"]

    if not slug:
        logger.warning(f"[{company_name}] No sr_company_slug configured")
        return []

    base_api = f"https://api.smartrecruiters.com/v1/companies/{slug}/postings"
    apply_base = f"https://jobs.smartrecruiters.com/{slug}"

    jobs   = []
    offset = 0
    total  = None

    while True:
        params = {
            "q":       search_text,
            "limit":   100,
            "offset":  offset,
        }
        if country:
            params["country"] = country

        try:
            resp = sess.get(base_api, params=params, timeout=TIMEOUT)
            if resp.status_code == 404:
                logger.warning(f"[{company_name}] 404 — company slug '{slug}' not found")
                break
            if resp.status_code != 200:
                logger.warning(f"[{company_name}] HTTP {resp.status_code}")
                break
            data = resp.json()
        except Exception as exc:
            logger.warning(f"[{company_name}] request error: {exc}")
            break

        if total is None:
            total = data.get("totalFound", 0)
            logger.info(f"[{company_name}] {total} total jobs (search: {search_text!r})")

        postings = data.get("content", [])
        if not postings:
            break

        for p in postings:
            title = p.get("name", "")
            if not title:
                continue
            if title_filter and not _match_keywords(title, title_filter):
                continue

            # Location: SmartRecruiters returns fullLocation like "Hyderabad, , India"
            loc_obj   = p.get("location", {})
            loc_str   = loc_obj.get("fullLocation", "") or loc_obj.get("city", "")

            posting_id  = p.get("id", "")
            apply_link  = f"{apply_base}/{posting_id}"

            jobs.append({
                "id":              _job_id(slug, posting_id),
                "title":           title,
                "company":         company_name,
                "location":        loc_str or country,
                "experience":      "",
                "is_remote":       p.get("location", {}).get("remote", False),
                "salary":          "Not disclosed",
                "apply_link":      apply_link,
                "description":     p.get("jobAd", {}).get("sections", {}).get("jobDescription", {}).get("text", ""),
                "tags":            [],
                "posted_at":       (p.get("createdOn") or "")[:10],
                "source":          f"smartrecruiters:{slug}",
                "fetched_date":    str(date.today()),
                "tailor_result":   None,
                "pdf_path":        None,
                "company_type":    company.get("company_type", "product"),
                "company_rating":  3.8,
                "company_tags":    "",
                "salary_estimate": _estimate_salary(title),
                "ats_type":        "smartrecruiters",
                "sr_slug":         slug,
                "sr_posting_id":   posting_id,
            })

        offset += len(postings)
        if total and offset >= total:
            break
        if offset >= MAX_JOBS_PER_CO:
            break
        time.sleep(RATE_SLEEP)

    logger.info(f"[{company_name}] → {len(jobs)} matching jobs")
    return jobs


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Fetch SmartRecruiters jobs into job store")
    parser.add_argument("--company",  help="Filter to single company name (substring match)")
    parser.add_argument("--search",   default=DEFAULT_SEARCH_TEXT, help="Search text")
    parser.add_argument("--filter",   default=None, help="Comma-separated title filter words")
    parser.add_argument("--country",  default=DEFAULT_COUNTRY, help="Country code (default: IN)")
    parser.add_argument("--no-score", action="store_true", help="Skip AI fit-scoring")
    parser.add_argument("--dry-run",  action="store_true", help="Fetch and print but don't save")
    parser.add_argument("--profile",  default="kaushal-kumar-jha", help="Profile slug to use")
    args = parser.parse_args()

    import profiles
    profiles.set_active_profile(args.profile)
    import job_store
    import company_database as cdb

    all_companies = (
        cdb.PRODUCT_TIER1 + cdb.PRODUCT_TIER2 + cdb.PRODUCT_TIER3
        + cdb.SERVICE_TIER1 + cdb.SERVICE_TIER2 + cdb.SERVICE_TIER3
    )
    sr_cos = [c for c in all_companies if c.get("ats_type") == "smartrecruiters"]

    if args.company:
        needle = args.company.lower()
        sr_cos = [c for c in sr_cos if needle in c["name"].lower()]
        if not sr_cos:
            logger.error(f"No SmartRecruiters company matches '{args.company}'")
            sys.exit(1)

    title_filter = (
        [k.strip() for k in args.filter.split(",") if k.strip()]
        if args.filter
        else DEFAULT_TITLE_FILTER
    )

    logger.info(f"Fetching SmartRecruiters jobs — {len(sr_cos)} companies, search={args.search!r}")

    sess = requests.Session()
    sess.headers.update(_HEADERS)
    sess.verify = False

    all_jobs = []
    for co in sr_cos:
        try:
            jobs = fetch_sr_company(co, args.search, title_filter, args.country, sess)
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
    print(f"  SmartRecruiters fetch complete — {len(new_ids)} new jobs added")
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
                print(
                    f"  [{j.get('fit_score', '?')}/10] {j['title']} @ {j['company']}"
                    f" — {j['location']}"
                )
                print(f"    {j['apply_link']}")


if __name__ == "__main__":
    main()
