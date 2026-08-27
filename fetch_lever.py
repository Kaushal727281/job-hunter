#!/usr/bin/env python3
"""
fetch_lever.py
--------------
Fetches jobs from Lever ATS companies in company_database.py
and saves them into the active profile's job store.

Lever public jobs API:
    GET https://api.lever.co/v0/postings/{slug}?mode=json&limit=100&offset=0
    Returns a JSON list of job objects.

Usage:
    python fetch_lever.py                              # all Lever companies
    python fetch_lever.py --company "Meesho"           # single company
    python fetch_lever.py --filter "java,backend"      # custom title filter
    python fetch_lever.py --no-loc                     # skip location filter
    python fetch_lever.py --no-score                   # skip AI fit-scoring
    python fetch_lever.py --dry-run                    # fetch and print, don't save
"""

import argparse
import hashlib
import logging
import os
import re
import sys
import time
from datetime import date, datetime

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
    "senior engineer", "principal engineer", "engineering manager",
]
DEFAULT_LOCATION_FILTER = [
    "india", "bengaluru", "bangalore", "mumbai", "hyderabad",
    "chennai", "pune", "noida", "gurugram", "gurgaon", "remote",
]
RATE_SLEEP      = 1.0
TIMEOUT         = 20
MAX_JOBS_PER_CO = 200

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
}

# Known Lever slugs for companies without ats_token
_KNOWN_SLUGS = {
    "BrowserStack":                 "browserstack",
    "Groww":                        "groww",
    "Unacademy":                    "unacademy",
    "BlackBuck":                    "blackbuck",
    "ShareChat":                    "sharechat",
    "Harness":                      "harness",
    "Harness.io":                   "harness",
    "Gupshup":                      "gupshup",
    "Open Financial (Fi Money)":    "fi",
    "Jupiter Money":                "jupitermoney",
    "Cashfree Payments":            "cashfree",
    "Chargebee":                    "chargebee",
    "Clevertap":                    "clevertap",
    "MoEngage":                     "moengage",
    "Fractal Analytics":            "fractal",
    "Rippling":                     "rippling",
    "Sprinto":                      "sprinto",
    "Spendflo":                     "spendflo",
    "Zluri":                        "zluri",
    "Hubilo":                       "hubilo",
    "Airmeet":                      "airmeet",
    "LambdaTest":                   "lambdatest",
    "Testsigma":                    "testsigma",
    "Classplus":                    "classplus",
    "DeHaat":                       "dehaat",
    "Smallcase":                    "smallcase",
    "Setu":                         "setu",
    "Appsmith":                     "appsmith",
    "Yellow.ai":                    "yellow-ai",
    "Yellow Messenger (Yellow.ai)": "yellow-ai",
    "Vernacular AI (Skit.ai)":      "vernacular",
    "Moglix":                       "moglix",
    "Zetwerk":                      "zetwerk",
    "Apna.co":                      "apna",
    "Hasura":                       "hasura",
    "Whatfix Platform":             "whatfix",
    "Front App":                    "frontapp",
    "Hiver (email collab)":         "hiver",
}


# ── Helpers ───────────────────────────────────────────────────────────────────

def get_lever_slug(company: dict) -> str:
    """Resolve the Lever posting slug for a company."""
    # 1. Explicit ats_token (used for cred, meesho, spotify)
    if company.get("ats_token"):
        return company["ats_token"]
    # 2. Explicit lever_slug field
    if company.get("lever_slug"):
        return company["lever_slug"]
    # 3. Extract from jobs.lever.co URL
    career_url = company.get("career_url", "")
    m = re.search(r"jobs\.lever\.co/([^/?#\s]+)", career_url)
    if m:
        return m.group(1)
    # 4. Known slug lookup
    name = company.get("name", "")
    if name in _KNOWN_SLUGS:
        return _KNOWN_SLUGS[name]
    # 5. Slugify company name as fallback
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def _job_id(slug: str, posting_id: str) -> str:
    raw = f"lever:{slug}:{posting_id}"
    return "lv_" + hashlib.md5(raw.encode()).hexdigest()[:12]


def _match_keywords(title: str, keywords: list) -> bool:
    t = title.lower()
    return not keywords or any(kw.lower() in t for kw in keywords)


def _match_location(location: str, remote: bool, loc_filter: list) -> bool:
    if not loc_filter:
        return True
    if remote:
        return True
    loc_lower = location.lower()
    return any(lf in loc_lower for lf in loc_filter)


def _estimate_salary(title: str) -> str:
    t = title.lower()
    if any(w in t for w in ["principal", "distinguished", "vp", "vice president"]):
        lo, hi = 60, 110
    elif any(w in t for w in ["lead", "staff", "head", "director", "manager"]):
        lo, hi = 35, 70
    elif any(w in t for w in ["senior", "sr."]):
        lo, hi = 18, 40
    else:
        lo, hi = 12, 25
    return f"₹{int(lo * 1.3)}–{int(hi * 1.3)} LPA (est.)"


# ── Lever API fetcher ──────────────────────────────────────────────────────────

def fetch_lever_company(company: dict, title_filter: list, loc_filter: list,
                        sess: requests.Session) -> list:
    """Fetch and filter jobs for one Lever company."""
    slug         = get_lever_slug(company)
    company_name = company["name"]

    if not slug:
        logger.warning(f"[{company_name}] No Lever slug available, skipping")
        return []

    api_url = f"https://api.lever.co/v0/postings/{slug}"
    jobs    = []
    offset  = 0

    while True:
        params = {"mode": "json", "limit": 100, "offset": offset}
        try:
            resp = sess.get(api_url, params=params, timeout=TIMEOUT)
            if resp.status_code == 404:
                logger.warning(f"[{company_name}] 404 — slug '{slug}' not found on Lever")
                break
            if resp.status_code != 200:
                logger.warning(f"[{company_name}] HTTP {resp.status_code}")
                break
            data = resp.json()
        except Exception as exc:
            logger.warning(f"[{company_name}] request error: {exc}")
            break

        # Lever returns a list directly
        postings = data if isinstance(data, list) else data.get("data", [])
        if not postings:
            if offset == 0:
                logger.info(f"[{company_name}] 0 postings (slug: {slug})")
            break

        if offset == 0:
            logger.info(f"[{company_name}] {len(postings)} postings (slug: {slug})")

        for p in postings:
            title = p.get("text", "")
            if not title:
                continue

            if not _match_keywords(title, title_filter):
                continue

            cats      = p.get("categories", {})
            location  = cats.get("location", "") or cats.get("country", "") or ""
            team      = cats.get("team", "") or cats.get("department", "") or ""
            work_type = p.get("workplaceType", "")
            is_remote = work_type in ("remote", "hybrid")

            if loc_filter and not _match_location(location, is_remote, loc_filter):
                continue

            posting_id  = p.get("id", "")
            hosted_url  = p.get("hostedUrl",  f"https://jobs.lever.co/{slug}/{posting_id}")
            apply_url   = p.get("applyUrl",   f"https://jobs.lever.co/{slug}/{posting_id}/apply")
            description = (p.get("descriptionPlain", "") or p.get("description", ""))[:4000]

            created_ms = p.get("createdAt", 0)
            posted_at  = datetime.fromtimestamp(created_ms / 1000).strftime("%Y-%m-%d") if created_ms else ""

            jobs.append({
                "id":               _job_id(slug, posting_id),
                "title":            title,
                "company":          company_name,
                "location":         location or "India",
                "experience":       "",
                "is_remote":        is_remote,
                "salary":           "Not disclosed",
                "apply_link":       apply_url,
                "description":      description,
                "tags":             [t for t in [team] if t],
                "posted_at":        posted_at,
                "source":           f"lever:{slug}",
                "fetched_date":     str(date.today()),
                "tailor_result":    None,
                "pdf_path":         None,
                "company_type":     company.get("company_type", "product"),
                "company_rating":   3.8,
                "company_tags":     "",
                "salary_estimate":  _estimate_salary(title),
                "ats_type":         "lever",
                "lever_slug":       slug,
                "lever_job_id":     posting_id,
                "lever_hosted_url": hosted_url,
            })

        offset += len(postings)
        if len(postings) < 100:   # fewer than limit → last page
            break
        if offset >= MAX_JOBS_PER_CO:
            break
        time.sleep(RATE_SLEEP)

    logger.info(f"[{company_name}] → {len(jobs)} matching jobs")
    return jobs


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Fetch Lever jobs into job store")
    parser.add_argument("--company",  help="Filter to single company name (substring match)")
    parser.add_argument("--filter",   default=None, help="Comma-separated title filter words")
    parser.add_argument("--no-loc",   action="store_true", help="Skip location filter (fetch all locations)")
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
    lever_cos = [c for c in all_companies if c.get("ats_type") == "lever"]

    if args.company:
        needle = args.company.lower()
        lever_cos = [c for c in lever_cos if needle in c["name"].lower()]
        if not lever_cos:
            logger.error(f"No Lever company matches '{args.company}'")
            sys.exit(1)

    title_filter = (
        [k.strip() for k in args.filter.split(",") if k.strip()]
        if args.filter
        else DEFAULT_TITLE_FILTER
    )
    loc_filter = [] if args.no_loc else DEFAULT_LOCATION_FILTER

    logger.info(f"Fetching Lever jobs — {len(lever_cos)} companies")

    sess = requests.Session()
    sess.headers.update(_HEADERS)
    sess.verify = False

    all_jobs: list[dict] = []
    for co in lever_cos:
        try:
            jobs = fetch_lever_company(co, title_filter, loc_filter, sess)
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
    print(f"  Lever fetch complete — {len(new_ids)} new jobs added")
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
