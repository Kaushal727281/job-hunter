#!/usr/bin/env python3
"""
apply_greenhouse.py
-------------------
Applies to Greenhouse-ATS jobs via the public Greenhouse Application API.
No browser / Playwright needed for standard Greenhouse boards.

API flow:
  1. GET  https://boards-api.greenhouse.io/v1/boards/{board}/jobs/{job_id}?questions=true
         -> returns job metadata + list of required/optional questions
  2. POST https://boards-api.greenhouse.io/v1/applications?token={job_id}
         -> multipart/form-data: first_name, last_name, email, phone, resume, answers
  3. 200 -> applied; 4xx -> log and skip

Jobs are read from the profile's job store (ats_type=greenhouse),
sorted by fit_score descending, and skipped if already applied.

Usage:
    python3 apply_greenhouse.py                          # all 7+ score greenhouse jobs
    python3 apply_greenhouse.py --min-score 8
    python3 apply_greenhouse.py --company "Stripe"
    python3 apply_greenhouse.py --job-id 8031833 --board stripe
    python3 apply_greenhouse.py --dry-run
    python3 apply_greenhouse.py --limit 5
"""

import argparse
import logging
import os
import sys
import time

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

# ── Candidate profile ────────────────────────────────────────────────────────

PROFILE = {
    "first_name": "Kaushal Kumar",
    "last_name":  "Jha",
    "email":      "kaushalkumarjha727219@gmail.com",
    "phone":      "9818147393",
    "phone_e164": "+919818147393",
    "linkedin":   "https://www.linkedin.com/in/kaushal-kumar-jha-93b77512a/",
    "website":    "https://github.com/Kaushal727281",
    # Default resume PDF (overridden per-job if tailored PDF exists)
    "resume_pdf": os.path.expanduser(
        "~/gitQW/IO/Resume/job-hunter/profiles/kaushal-kumar-jha/output/"
        "2026-07-31/Okta-Staff_Fullstack_Engineer/resume.pdf"
    ),
    # EEO / compliance defaults
    "gender":            "1",   # 1=Male
    "race":              "2",   # 2=Decline to identify
    "veteran":           "3",   # 3=Not a protected veteran (US), skip if not asked
    "disability":        "2",   # 2=Decline to answer
}

# Keywords that trigger a "Yes" answer on screening questions
_YES_KEYWORDS = (
    "legally authorized", "authorized to work", "eligible to work",
    "right to work", "sponsorship not required",
    "citizen or permanent resident",
    "legal right to work",
)
# Keywords that trigger a "No" answer
_NO_KEYWORDS = (
    "require sponsorship", "need sponsorship", "visa sponsorship required",
    "not authorized", "will you now or in the future require",
    "require a work permit", "visa or additional right",
)

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, */*",
}

RATE_SLEEP = 2.0
TIMEOUT    = 20


# ── Greenhouse API helpers ───────────────────────────────────────────────────

def _gh_job_details(board: str, job_id: str, sess: requests.Session) -> dict:
    """Fetch job details + questions from Greenhouse boards API."""
    url = (
        f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs/{job_id}"
        "?questions=true"
    )
    r = sess.get(url, timeout=TIMEOUT)
    if r.status_code != 200:
        logger.warning(f"  Job details {board}/{job_id}: HTTP {r.status_code}")
        return {}
    return r.json()


def _answer_for_question(q: dict) -> str | None:
    """
    Return an answer value for a Greenhouse application question.
    Returns None for fields handled at the top-level (first_name/last_name/email/resume).
    """
    label  = (q.get("label") or "").lower()
    q_type = q.get("type", "")

    # Top-level fields already set in form_data — skip here
    if label in ("first name", "last name", "email", "resume/cv", "resume",
                 "cover letter", "phone"):
        return None

    # Phone (alternate labels)
    if "phone" in label and "number" in label:
        return PROFILE["phone_e164"]
    # LinkedIn
    if "linkedin" in label:
        return PROFILE["linkedin"]
    # Website / portfolio / GitHub
    if any(k in label for k in ("website", "portfolio", "github", "url", "blog")):
        return PROFILE["website"]
    # Sponsorship / work permit — check FIRST (these overlap with "right to work" pattern)
    if any(kw in label for kw in _NO_KEYWORDS):
        return "No"
    # Work authorization — Yes
    if any(kw in label for kw in _YES_KEYWORDS):
        return "Yes"
    # Current employer / company
    if any(k in label for k in ("current company", "current employer", "employer")):
        return "FICO (Fair Isaac Corporation)"
    # Current title / role
    if any(k in label for k in ("current title", "current role", "job title", "current position")):
        return "Lead Software Engineer"
    # Location / city / address
    if any(k in label for k in ("city", "home address", "location", "city and state")):
        return "Bengaluru, Karnataka, India"
    # Notice period
    if "notice" in label:
        return "30 days"
    # Salary/compensation
    if any(k in label for k in ("salary", "compensation", "expected", "current ctc", "pay")):
        return "Open to discussion"
    # How did you hear about us
    if any(k in label for k in ("hear about", "source", "referred", "learn about")):
        return "LinkedIn"
    # Years of experience
    if any(k in label for k in ("years of experience", "years experience")):
        return "6"
    # Previously worked here? No.
    if any(k in label for k in ("previously worked", "worked for", "employed by", "former employee")):
        return "No"
    # Conflict of interest / outside activity / family member — No
    if any(k in label for k in ("conflict", "outside business", "family member", "relatives",
                                 "procurement", "government employee")):
        return "No"
    # Privacy/consent/acknowledgement checkboxes — agree
    if any(k in label for k in ("privacy policy", "consent", "acknowledge", "agree", "i agree",
                                 "confidential information")):
        return "Yes"
    # Generic boolean/checkbox → No (safe default for unknown Yes/No questions)
    if q_type == "boolean":
        return "No"
    return None


def _resume_for_job(job: dict) -> str:
    pdf = job.get("pdf_path")
    if pdf and os.path.isfile(pdf):
        return pdf
    return PROFILE["resume_pdf"]


# ── Apply to one Greenhouse job ──────────────────────────────────────────────

def apply_to_job(job: dict, sess: requests.Session, dry_run: bool = False) -> bool:
    """
    Apply to one Greenhouse job via the Application API.
    Returns True on success.
    """
    board  = job.get("gh_board", "")
    job_id = job.get("gh_job_id", "")

    if not board or not job_id:
        logger.warning(f"  [{job.get('company')}] Missing gh_board/gh_job_id — skipping")
        return False

    resume_pdf = _resume_for_job(job)
    if not os.path.isfile(resume_pdf):
        logger.warning(f"  [{job.get('company')}] Resume PDF not found: {resume_pdf}")
        return False

    print(f"\n{'='*60}")
    print(f"  JOB  : {job['title']} @ {job['company']}")
    print(f"  SCORE: {job.get('fit_score','?')}/10  {job.get('fit_reason','')}")
    print(f"  LINK : {job.get('apply_link','')}")
    print(f"  BOARD: {board}  JOB_ID: {job_id}")
    print(f"{'='*60}")

    # ── Fetch job details + questions ──────────────────────────────────────
    details = _gh_job_details(board, job_id, sess)
    if not details:
        return False

    questions = details.get("questions", [])
    logger.info(f"  [{job.get('company')}] {len(questions)} application questions")

    if dry_run:
        for q in questions:
            ans = _answer_for_question(q)
            print(f"    Q: {q.get('label')!r:50s}  -> {ans!r}")
        return True

    # ── Build multipart form ───────────────────────────────────────────────
    # Required base fields
    form_data = {
        "first_name": PROFILE["first_name"],
        "last_name":  PROFILE["last_name"],
        "email":      PROFILE["email"],
        "phone":      PROFILE["phone_e164"],
    }

    # Answer questions
    for q in questions:
        fields = q.get("fields", [])
        for f in fields:
            fname = f.get("name")
            if not fname:
                continue
            # Resume field — handled via file upload below
            if f.get("type") == "input_file" or "resume" in (fname or "").lower():
                continue
            answer = _answer_for_question(q)
            if answer is not None:
                form_data[fname] = answer

    # EEO compliance fields (Greenhouse standard names)
    form_data["job_application[answers_attributes][0][question_id]"] = ""
    # These are submitted under the `demographics` key if present
    for q in questions:
        label = (q.get("label") or "").lower()
        fields = q.get("fields", [])
        for f in fields:
            fname = f.get("name", "")
            if "gender" in fname:
                form_data[fname] = PROFILE["gender"]
            elif "race" in fname:
                form_data[fname] = PROFILE["race"]
            elif "veteran" in fname:
                form_data[fname] = PROFILE["veteran"]
            elif "disability" in fname:
                form_data[fname] = PROFILE["disability"]

    # ── POST application ───────────────────────────────────────────────────
    post_url = f"https://boards-api.greenhouse.io/v1/applications?token={job_id}"
    logger.info(f"  Submitting to {post_url}")

    try:
        with open(resume_pdf, "rb") as rf:
            files = {"resume": (os.path.basename(resume_pdf), rf, "application/pdf")}
            resp = sess.post(
                post_url,
                data=form_data,
                files=files,
                timeout=30,
            )

        if resp.status_code in (200, 201):
            print(f"  [SUCCESS] Application submitted for {job['title']} @ {job['company']}")
            return True
        else:
            logger.warning(
                f"  [{job.get('company')}] HTTP {resp.status_code}: {resp.text[:300]}"
            )
            # 422 = validation error (common for questions needing more specific answers)
            if resp.status_code == 422:
                try:
                    errs = resp.json().get("errors", [])
                    for e in errs:
                        print(f"    Validation: {e}")
                except Exception:
                    pass
            return False

    except Exception as exc:
        logger.warning(f"  [{job.get('company')}] POST error: {exc}")
        return False


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Apply to Greenhouse jobs from job store")
    parser.add_argument("--min-score", type=int, default=7,
                        help="Minimum fit_score to apply (default: 7)")
    parser.add_argument("--company",  help="Filter to a single company (substring match)")
    parser.add_argument("--job-id",   help="Apply to a specific Greenhouse job ID")
    parser.add_argument("--board",    help="Board slug (required with --job-id)")
    parser.add_argument("--limit",    type=int, help="Max jobs to apply per run")
    parser.add_argument("--dry-run",  action="store_true", help="Print questions but don't submit")
    parser.add_argument("--profile",  default="kaushal-kumar-jha", help="Profile slug")
    args = parser.parse_args()

    import profiles
    profiles.set_active_profile(args.profile)
    import job_store

    if args.job_id:
        if not args.board:
            print("--board is required with --job-id")
            sys.exit(1)
        jobs = [{
            "id":          f"gh_{args.job_id}",
            "title":       "Job from CLI",
            "company":     args.board,
            "apply_link":  f"https://boards.greenhouse.io/{args.board}/jobs/{args.job_id}",
            "fit_score":   10,
            "fit_reason":  "Manually specified",
            "gh_board":    args.board,
            "gh_job_id":   args.job_id,
            "pdf_path":    None,
        }]
    else:
        all_jobs = job_store.all_jobs()
        jobs = [
            j for j in all_jobs
            if j.get("ats_type") == "greenhouse"
            and j.get("fit_score", 0) >= args.min_score
            and not j.get("applied_at")
            and not j.get("removed")
            and j.get("gh_board")
            and j.get("gh_job_id")
        ]
        if args.company:
            needle = args.company.lower()
            jobs = [j for j in jobs if needle in j.get("company", "").lower()]

        # Sort by fit_score descending (highest first)
        jobs.sort(key=lambda j: j.get("fit_score", 0), reverse=True)

        if args.limit:
            jobs = jobs[:args.limit]

    if not jobs:
        print(f"No Greenhouse jobs with fit_score >= {args.min_score} ready to apply.")
        print("Run:  python3 fetch_greenhouse.py  to fetch jobs first.")
        sys.exit(0)

    print(f"\nFound {len(jobs)} Greenhouse job(s) to apply to (sorted by fit score):\n")
    for j in jobs:
        print(f"  [{j.get('fit_score','?')}/10] {j.get('title','?')[:55]} @ {j.get('company','?')}")
        print(f"    board={j.get('gh_board')}  id={j.get('gh_job_id')}")
    print()

    if args.dry_run:
        sess = requests.Session()
        sess.headers.update(_HEADERS)
        sess.verify = False
        for j in jobs:
            apply_to_job(j, sess, dry_run=True)
        return

    sess = requests.Session()
    sess.headers.update(_HEADERS)
    sess.verify = False

    applied_ids = []
    skipped     = []

    for j in jobs:
        try:
            ok = apply_to_job(j, sess)
            if ok:
                job_store.mark_applied(j["id"])
                applied_ids.append(j["id"])
                print(f"  Marked applied: {j['id']}")
            else:
                skipped.append(j)
        except Exception as exc:
            logger.warning(f"  [{j.get('company')}] Unexpected error: {exc}")
            skipped.append(j)
        time.sleep(RATE_SLEEP)

    print("\n" + "=" * 60)
    print(f"  Greenhouse apply complete")
    print(f"  Applied:  {len(applied_ids)}")
    print(f"  Skipped/failed: {len(skipped)}")
    print("=" * 60)

    if skipped:
        print("\nNeeds manual attention:")
        for j in skipped:
            print(f"  {j.get('title')} @ {j.get('company')}")
            print(f"    {j.get('apply_link')}")


if __name__ == "__main__":
    main()
