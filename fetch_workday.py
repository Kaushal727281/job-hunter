#!/usr/bin/env python3
"""
fetch_workday.py
----------------
Fetches jobs from all Workday-ATS companies in company_database.py
and saves them into the active profile's job store.

Usage:
    python fetch_workday.py                              # all workday companies, default keywords
    python fetch_workday.py --company "Morgan Stanley"   # single company
    python fetch_workday.py --keywords "java spring boot" # custom keywords
    python fetch_workday.py --location "Bengaluru"       # filter by location
    python fetch_workday.py --tier 1                     # only tier-1 companies
    python fetch_workday.py --no-score                   # skip AI fit-scoring
    python fetch_workday.py --dry-run                    # fetch but don't save
"""

import argparse
import hashlib
import json
import logging
import re
import sys
import time
from datetime import date
from pathlib import Path

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

# ── Default search config ────────────────────────────────────────────────────
# searchText is sent verbatim to Workday's JSON API — use plain spaces, no +
DEFAULT_SEARCH_TEXT = "software engineer"

# Additional title-based filter: a fetched job must match at least one of these
# (case-insensitive substring match against job title)
DEFAULT_TITLE_FILTER = [
    "software engineer",
    "fullstack",
    "full stack",
    "java",
    "backend",
    "platform engineer",
    "lead engineer",
    "staff engineer",
    "application development",   # Intel: "Software Application Development Engineer"
    "development engineer",       # Intel: "Enterprise Application Development Engineer"
    "AI/ML",                      # Intel: "AI/ML Technologist"
    "software developer",
]

DEFAULT_LOCATION = "India"   # broad filter — accepts Bengaluru, Hyderabad, etc.
RATE_SLEEP       = 1.5       # seconds between company API calls
TIMEOUT          = 20        # HTTP timeout (seconds)
MAX_JOBS_PER_CO  = 100       # page up to 5 pages × 20 per company

# ── Headers ──────────────────────────────────────────────────────────────────

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json,*/*;q=0.9",
    "Accept-Language": "en-US,en;q=0.8",
}

# ── Helpers ───────────────────────────────────────────────────────────────────

def _job_id(tenant: str, ext_path: str) -> str:
    raw = f"workday:{tenant}:{ext_path}"
    return "wd_" + hashlib.md5(raw.encode()).hexdigest()[:12]


def _match_keywords(title: str, keywords: list[str]) -> bool:
    t = title.lower()
    return any(kw.lower() in t for kw in keywords)


_INDIA_CITIES = {
    "bangalore", "bengaluru", "hyderabad", "pune", "noida", "gurugram",
    "gurgaon", "chennai", "mumbai", "delhi", "kolkata", "ahmedabad",
    "india",
}

def _match_location(loc: str, location: str) -> bool:
    if not location:
        return True
    l = loc.lower()
    for part in location.lower().split(","):
        part = part.strip()
        if not part:
            continue
        if part in l:
            return True
        # When filtering for "India", also match bare Indian city names
        if part == "india" and any(city in l for city in _INDIA_CITIES):
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
    if any(w in t for w in ["machine learning", "ml engineer", "ai engineer"]):
        lo, hi = int(lo * 1.3), int(hi * 1.3)
    # Product company premium already baked-in via 1.3 × company_type multiplier
    lo = int(lo * 1.3)
    hi = int(hi * 1.3)
    return f"₹{lo}–{hi} LPA (est.)"


# ── Workday API fetcher ───────────────────────────────────────────────────────

def fetch_workday_company(company: dict, search_text: str, title_filter: list[str],
                           location: str, sess: requests.Session) -> list[dict]:
    """Fetch jobs for one Workday company. Returns list of job-store-compatible dicts."""
    career_url   = company.get("career_url", "")
    company_name = company["name"]

    # Parse tenant + site from career URL
    # Handles: https://{tenant}.wd{n}.myworkdayjobs.com/{en-US/}{site}
    m = re.match(
        r"https?://([^.]+)\.(wd\d+)\.myworkdayjobs\.com/(?:en-US/)?([^/?#]+)",
        career_url, re.I,
    )
    if not m:
        logger.warning(f"[{company_name}] Cannot parse Workday URL: {career_url}")
        return []

    tenant, wd_instance, site_path = m.group(1), m.group(2), m.group(3)
    api_url = (
        f"https://{tenant}.{wd_instance}.myworkdayjobs.com"
        f"/wday/cxs/{tenant}/{site_path}/jobs"
    )
    # Base URL used to build full job URLs
    base_url = f"https://{tenant}.{wd_instance}.myworkdayjobs.com/en-US/{site_path}"

    jobs   = []
    offset = 0
    total  = None

    # Some companies (e.g. Intel) use appliedFacets.locations with Workday location IDs
    # instead of relying on location text filtering. Use them when provided.
    location_ids = company.get("workday_location_ids", [])

    while True:
        if location_ids:
            payload = {
                "appliedFacets": {"locations": location_ids},
                "limit": 20,
                "offset": offset,
                "searchText": search_text,
            }
        else:
            payload = {
                "limit": 20,
                "offset": offset,
                "searchText": search_text,   # plain text, no + signs
                "locations": [],
            }
        try:
            resp = sess.post(
                api_url,
                json=payload,
                headers={"Content-Type": "application/json"},
                timeout=TIMEOUT,
            )
            if resp.status_code != 200:
                logger.warning(f"[{company_name}] HTTP {resp.status_code} from {api_url}")
                break
            data = resp.json()
        except Exception as exc:
            logger.warning(f"[{company_name}] request error: {exc}")
            break

        if total is None:
            total = data.get("total", 0)
            logger.info(f"[{company_name}] {total} total jobs (search: {search_text!r})")

        postings = data.get("jobPostings", [])
        if not postings:
            break

        for jp in postings:
            title = jp.get("title", "")
            loc   = jp.get("locationsText", "")
            if not title:
                continue
            # Title filter (must match at least one keyword)
            if title_filter and not _match_keywords(title, title_filter):
                continue
            # Location filter — skip if API already filtered by location_ids
            if not location_ids and not _match_location(loc, location):
                continue

            ext_path  = jp.get("externalPath", "")
            apply_link = base_url.rstrip("/") + ext_path

            jobs.append({
                "id":             _job_id(tenant, ext_path or title),
                "title":          title,
                "company":        company_name,
                "location":       loc or location,
                "experience":     "",
                "is_remote":      "remote" in loc.lower() or "remote" in title.lower(),
                "salary":         "Not disclosed",
                "apply_link":     apply_link,
                "description":    jp.get("jobDescription", {}).get("item", "") if isinstance(jp.get("jobDescription"), dict) else "",
                "tags":           [],
                "posted_at":      jp.get("postedOn", "")[:10],
                "source":         f"workday:{tenant}",
                "fetched_date":   str(date.today()),
                "tailor_result":  None,
                "pdf_path":       None,
                "company_type":   company.get("company_type", "product"),
                "company_rating": 3.8,
                "company_tags":   "",
                "salary_estimate": _estimate_salary(title),
                # Extra Workday-specific metadata
                "ats_type":       "workday",
                "wd_tenant":      tenant,
                "wd_instance":    wd_instance,
                "wd_site":        site_path,
                "wd_ext_path":    ext_path,
            })

        offset += len(postings)
        if total and offset >= total:
            break
        if offset >= MAX_JOBS_PER_CO:
            break
        time.sleep(RATE_SLEEP)

    logger.info(f"[{company_name}] → {len(jobs)} matching jobs saved")
    return jobs


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Fetch Workday jobs into job store")
    parser.add_argument("--company",  help="Filter to single company name (substring match)")
    parser.add_argument("--search",   default=DEFAULT_SEARCH_TEXT,
                        help=f"Workday searchText sent to API (default: '{DEFAULT_SEARCH_TEXT}')")
    parser.add_argument("--filter",   default=None,
                        help="Comma-separated title filter words (default: java,fullstack,...)")
    parser.add_argument("--location", default=DEFAULT_LOCATION, help="Location filter (default: India)")
    parser.add_argument("--tier",     type=int, choices=[1, 2, 3], help="Only companies of this tier")
    parser.add_argument("--no-score", action="store_true", help="Skip AI fit-scoring after fetch")
    parser.add_argument("--dry-run",  action="store_true", help="Fetch and print but don't save")
    parser.add_argument("--profile",  default="kaushal-kumar-jha", help="Profile slug to use")
    args = parser.parse_args()

    # ── Activate profile so job_store / profiles module work ─────────────────
    import profiles
    profiles.set_active_profile(args.profile)
    import job_store
    import company_database as cdb

    # ── Build company list ────────────────────────────────────────────────────
    all_companies = (
        cdb.PRODUCT_TIER1 + cdb.PRODUCT_TIER2 + cdb.PRODUCT_TIER3
        + cdb.SERVICE_TIER1 + cdb.SERVICE_TIER2 + cdb.SERVICE_TIER3
    )
    workday_cos = [c for c in all_companies if c.get("ats_type") == "workday"]

    if args.company:
        needle = args.company.lower()
        workday_cos = [c for c in workday_cos if needle in c["name"].lower()]
        if not workday_cos:
            logger.error(f"No Workday company matches '{args.company}'")
            sys.exit(1)

    if args.tier:
        workday_cos = [c for c in workday_cos if c.get("tier") == args.tier]

    search_text  = args.search
    title_filter = (
        [k.strip() for k in args.filter.split(",") if k.strip()]
        if args.filter
        else DEFAULT_TITLE_FILTER
    )

    logger.info(
        f"Fetching Workday jobs — {len(workday_cos)} companies, "
        f"search={search_text!r}, location={args.location!r}"
    )

    # ── Fetch ─────────────────────────────────────────────────────────────────
    sess = requests.Session()
    sess.headers.update(_HEADERS)
    sess.verify = False

    all_jobs: list[dict] = []
    for co in workday_cos:
        try:
            jobs = fetch_workday_company(co, search_text, title_filter, args.location, sess)
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

    # ── Save to job store ─────────────────────────────────────────────────────
    new_ids = job_store.upsert_jobs_return_ids(all_jobs)
    logger.info(f"New jobs added to store: {len(new_ids)}")

    # ── AI fit-scoring ────────────────────────────────────────────────────────
    if new_ids and not args.no_score:
        try:
            import job_scorer
            logger.info("Running AI fit-scoring on new jobs…")
            job_scorer.score_jobs(new_ids)
        except Exception as exc:
            logger.warning(f"Fit-scoring skipped: {exc}")

    # ── Summary ───────────────────────────────────────────────────────────────
    print("\n" + "=" * 60)
    print(f"  Workday fetch complete — {len(new_ids)} new jobs added")
    print(f"  Total fetched: {len(all_jobs)}")
    print("=" * 60)

    # Show top-scored jobs if scoring ran
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
                if j.get("fit_reason"):
                    print(f"    Reason: {j['fit_reason']}")


if __name__ == "__main__":
    main()
