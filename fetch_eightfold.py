#!/usr/bin/env python3
"""
fetch_eightfold.py
------------------
Fetches jobs from Eightfold AI-powered career portals.

Companies using Eightfold: Qualcomm, Microsoft, Micron, Applied Materials, etc.
The search API is public (no auth needed) — CSRF only required for applications.

Eightfold search API:
    GET https://{host}/api/pcsx/search
    Params: domain={domain}&query={text}&location={city}&start={offset}&sort_by=match

Usage:
    python fetch_eightfold.py                           # all eightfold companies
    python fetch_eightfold.py --company "Qualcomm"      # single company
    python fetch_eightfold.py --location "Bengaluru, KA, India"
    python fetch_eightfold.py --no-score                # skip AI fit-scoring
    python fetch_eightfold.py --dry-run
"""

import argparse
import hashlib
import logging
import os
import sys
import time
from datetime import date

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

# ── Defaults ─────────────────────────────────────────────────────────────────

DEFAULT_QUERY        = "software engineer"
DEFAULT_LOCATION     = "Bangalore, India"   # Qualcomm/Eightfold uses "Bangalore" not "Bengaluru"
DEFAULT_TITLE_FILTER = [
    "software engineer", "fullstack", "full stack",
    "java", "backend", "platform engineer", "lead engineer", "staff engineer",
    "application development", "development engineer", "AI/ML",
    "software developer", "site reliability", "devops",
]
RATE_SLEEP    = 1.5
TIMEOUT       = 20
MAX_PER_CO    = 200
PAGE_SIZE     = 10   # Eightfold default page size

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://careers.qualcomm.com/",
}

# ── Helpers ───────────────────────────────────────────────────────────────────

def _job_id(domain: str, position_id) -> str:
    raw = f"ef:{domain}:{position_id}"
    return "ef_" + hashlib.md5(raw.encode()).hexdigest()[:12]


def _match_keywords(title: str, keywords: list) -> bool:
    t = title.lower()
    return any(kw.lower() in t for kw in keywords)


def _estimate_salary(title: str) -> str:
    t = title.lower()
    if any(w in t for w in ["principal", "distinguished", "vp", "vice president"]):
        lo, hi = 60, 110
    elif any(w in t for w in ["lead", "staff", "sr. staff", "head", "director"]):
        lo, hi = 35, 70
    elif any(w in t for w in ["senior", "sr."]):
        lo, hi = 18, 40
    else:
        lo, hi = 12, 25
    lo = int(lo * 1.3)
    hi = int(hi * 1.3)
    return f"₹{lo}–{hi} LPA (est.)"


# ── Eightfold fetcher ─────────────────────────────────────────────────────────

def fetch_eightfold_company(company: dict, query: str, location: str,
                             title_filter: list, sess: requests.Session) -> list:
    """Fetch jobs for one Eightfold company."""
    host         = company.get("eightfold_host", "")
    domain       = company.get("eightfold_domain", "")
    company_name = company["name"]

    if not host or not domain:
        logger.warning(f"[{company_name}] Missing eightfold_host or eightfold_domain")
        return []

    search_url = f"https://{host}/api/pcsx/search"
    apply_base = f"https://{host}/careers"

    jobs  = []
    start = 0
    total = None

    while True:
        params = {
            "domain":      domain,
            "query":       query,
            "location":    location,
            "start":       start,
            "sort_by":     "match",
            "filter_distance": 80,
        }
        try:
            resp = sess.get(search_url, params=params, timeout=TIMEOUT)
            if resp.status_code != 200:
                logger.warning(f"[{company_name}] HTTP {resp.status_code}")
                break
            data = resp.json()
        except Exception as exc:
            logger.warning(f"[{company_name}] request error: {exc}")
            break

        # Eightfold wraps payload under a "data" key
        inner = data.get("data", data) if isinstance(data, dict) else data

        if total is None:
            total = inner.get("count", 0)
            logger.info(f"[{company_name}] {total} total jobs (query: {query!r}, location: {location!r})")

        positions = inner.get("positions", [])
        if not positions:
            break

        for p in positions:
            title = p.get("name", "")
            if not title:
                continue
            if title_filter and not _match_keywords(title, title_filter):
                continue

            position_id  = p.get("id", "")
            pos_url      = p.get("positionUrl", "")
            apply_link   = f"https://{host}{pos_url}" if pos_url else f"{apply_base}/{position_id}"

            # Location: Eightfold returns array of location strings
            locs     = p.get("locations", [])
            loc_str  = ", ".join(locs[:2]) if locs else location
            is_remote = any("remote" in l.lower() for l in locs) or p.get("workLocationOption", "") == "remote"

            jobs.append({
                "id":              _job_id(domain, position_id),
                "title":           title,
                "company":         company_name,
                "location":        loc_str,
                "experience":      "",
                "is_remote":       is_remote,
                "salary":          "Not disclosed",
                "apply_link":      apply_link,
                "description":     "",
                "tags":            [],
                "posted_at":       str(date.fromtimestamp(p["postedTs"] / 1000))
                                   if p.get("postedTs") else "",
                "source":          f"eightfold:{domain}",
                "fetched_date":    str(date.today()),
                "tailor_result":   None,
                "pdf_path":        None,
                "company_type":    company.get("company_type", "product"),
                "company_rating":  3.8,
                "company_tags":    "",
                "salary_estimate": _estimate_salary(title),
                "ats_type":        "eightfold",
                "ef_domain":       domain,
                "ef_host":         host,
                "ef_position_id":  str(position_id),
            })

        start += len(positions)
        if total and start >= total:
            break
        if start >= MAX_PER_CO:
            break
        time.sleep(RATE_SLEEP)

    logger.info(f"[{company_name}] → {len(jobs)} matching jobs")
    return jobs


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Fetch Eightfold AI jobs into job store")
    parser.add_argument("--company",  help="Filter to single company name (substring match)")
    parser.add_argument("--query",    default=DEFAULT_QUERY, help="Search query text")
    parser.add_argument("--location", default=DEFAULT_LOCATION,
                        help=f"Location string (default: '{DEFAULT_LOCATION}')")
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
    ef_cos = [c for c in all_companies if c.get("ats_type") == "eightfold"]

    if args.company:
        needle = args.company.lower()
        ef_cos = [c for c in ef_cos if needle in c["name"].lower()]
        if not ef_cos:
            logger.error(f"No Eightfold company matches '{args.company}'")
            sys.exit(1)

    title_filter = (
        [k.strip() for k in args.filter.split(",") if k.strip()]
        if args.filter else DEFAULT_TITLE_FILTER
    )

    logger.info(f"Fetching Eightfold jobs — {len(ef_cos)} companies, "
                f"query={args.query!r}, location={args.location!r}")

    sess = requests.Session()
    sess.headers.update(_HEADERS)
    sess.verify = False

    all_jobs = []
    for co in ef_cos:
        try:
            jobs = fetch_eightfold_company(co, args.query, args.location, title_filter, sess)
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
    print(f"  Eightfold fetch complete — {len(new_ids)} new jobs added")
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
