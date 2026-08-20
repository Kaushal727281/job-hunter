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
import re
import shutil
import sys
import tempfile
import time

import truststore
import requests
import urllib3
from playwright.sync_api import sync_playwright
from playwright_stealth import Stealth

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
    """Fetch job details + questions from Greenhouse boards API (read-only, public)."""
    url = (
        f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs/{job_id}"
        "?questions=true"
    )
    r = sess.get(url, timeout=TIMEOUT)
    if r.status_code != 200:
        logger.warning(f"  Job details {board}/{job_id}: HTTP {r.status_code}")
        return {}
    return r.json()


def _settle(page, ms: int = 2000):
    try:
        page.wait_for_load_state("networkidle", timeout=ms)
    except Exception:
        pass
    time.sleep(0.5)


def _fill_by_label(page, label_text: str, value: str, timeout: int = 4000):
    """Fill an input field whose visible label contains label_text."""
    try:
        # Try: label element -> associated input via for/id
        label = page.locator(f'label:has-text("{label_text}")').first
        for_id = label.get_attribute("for")
        if for_id:
            page.locator(f"#{for_id}").fill(value)
            return True
    except Exception:
        pass
    try:
        # Fallback: input immediately after label in DOM
        page.locator(
            f'label:has-text("{label_text}") ~ input, '
            f'label:has-text("{label_text}") + input'
        ).first.fill(value)
        return True
    except Exception:
        pass
    try:
        # input[name=...] by common field name derived from label
        fname = label_text.lower().replace(" ", "_").replace("/", "_")
        page.locator(f'input[name="{fname}"], input[id="{fname}"]').first.fill(value)
        return True
    except Exception:
        pass
    return False


def _answer_label_on_page(page, label_text: str, answer: str):
    """
    Given a question label, find its input/select/checkbox/radio on the page
    and fill it with the answer string.
    """
    label_lower = label_text.lower()

    # Try radio / checkbox group (Yes/No type)
    if answer in ("Yes", "No"):
        try:
            # Find label for the answer value
            radio = page.locator(
                f'label:has-text("{answer}"):near(:text("{label_text[:40]}"))'
            ).first
            radio.click()
            return
        except Exception:
            pass
        try:
            # radiogroup under the question label
            container = page.locator(
                f'[data-field-label*="{label_text[:30]}"], '
                f'*:has(> label:has-text("{label_text[:30]}"))'
            ).first
            container.locator(f'label:has-text("{answer}"), input[value="{answer}"]').first.click()
            return
        except Exception:
            pass

    # Text / textarea fill
    try:
        _fill_by_label(page, label_text[:40], answer)
        return
    except Exception:
        pass

    # Select dropdown
    try:
        sel = page.locator(
            f'label:has-text("{label_text[:40]}") ~ select, '
            f'label:has-text("{label_text[:40]}") + select'
        ).first
        sel.select_option(label=answer)
    except Exception:
        pass


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


# ── Apply to one Greenhouse job (Playwright) ─────────────────────────────────

def apply_to_job(job: dict, ctx, sess: requests.Session, dry_run: bool = False) -> bool:
    """
    Apply to one Greenhouse job via browser automation (job-boards.greenhouse.io).
    ctx = Playwright browser context.
    Returns True on success / reaching confirmation.
    """
    board      = job.get("gh_board", "")
    job_id     = job.get("gh_job_id", "")
    apply_link = job.get("apply_link", "")
    resume_pdf = _resume_for_job(job)

    if not board or not job_id:
        logger.warning(f"  [{job.get('company')}] Missing gh_board/gh_job_id — skipping")
        return False
    if not os.path.isfile(resume_pdf):
        logger.warning(f"  [{job.get('company')}] Resume PDF not found: {resume_pdf}")
        return False

    # Prefer job-boards.greenhouse.io canonical URL
    gh_url = f"https://job-boards.greenhouse.io/{board}/jobs/{job_id}"

    print(f"\n{'='*60}")
    print(f"  JOB  : {job['title']} @ {job['company']}")
    print(f"  SCORE: {job.get('fit_score','?')}/10  {job.get('fit_reason','')}")
    print(f"  URL  : {gh_url}")
    print(f"{'='*60}")

    # Fetch question list for dry-run preview
    details   = _gh_job_details(board, job_id, sess)
    questions = details.get("questions", []) if details else []

    if dry_run:
        for q in questions:
            ans = _answer_for_question(q)
            print(f"    Q: {q.get('label')!r:55s}  -> {ans!r}")
        return True

    page = ctx.new_page()
    Stealth().apply_stealth_sync(page)

    try:
        # ── Step 1: Load job page ──────────────────────────────────────────
        print("[1] Loading job page…")
        page.goto(gh_url, wait_until="domcontentloaded", timeout=30000)
        _settle(page, 3000)

        # ── Step 2: Click Apply button ─────────────────────────────────────
        print("[2] Clicking Apply…")
        for sel in [
            'a:has-text("Apply for this Job")',
            'button:has-text("Apply for this Job")',
            'a:has-text("Apply Now")',
            'button:has-text("Apply")',
            '[data-qa="btn-apply"]',
        ]:
            try:
                btn = page.locator(sel).first
                if btn.is_visible(timeout=4000):
                    btn.click()
                    _settle(page, 3000)
                    break
            except Exception:
                pass

        # ── Step 3: Fill standard contact fields ──────────────────────────
        print("[3] Filling contact info…")
        # By name attribute (most common in Greenhouse)
        for field, value in [
            ("first_name", PROFILE["first_name"]),
            ("last_name",  PROFILE["last_name"]),
            ("email",      PROFILE["email"]),
            ("phone",      PROFILE["phone_e164"]),
        ]:
            try:
                loc = page.locator(f'input[name="{field}"], input[id="{field}"]').first
                if loc.is_visible(timeout=3000):
                    loc.fill(value)
            except Exception:
                pass
        # LinkedIn / website by common ids
        for field, value in [
            ("linkedin_profile_url", PROFILE["linkedin"]),
            ("website",              PROFILE["website"]),
        ]:
            try:
                page.locator(
                    f'input[id="{field}"], input[name="{field}"]'
                ).first.fill(value)
            except Exception:
                pass

        # ── Step 4: Upload resume ──────────────────────────────────────────
        print("[4] Uploading resume…")
        try:
            fi = page.locator('input[type="file"]').first
            fi.wait_for(state="attached", timeout=10000)
            fi.set_input_files(resume_pdf)
            _settle(page, 4000)
            print("    Resume uploaded.")
        except Exception as exc:
            print(f"    [WARN] Resume upload: {exc}")

        # ── Step 5: Answer custom questions ───────────────────────────────
        print("[5] Answering questions…")
        for q in questions:
            label = q.get("label", "")
            answer = _answer_for_question(q)
            if answer is None or not label:
                continue
            q_type = q.get("type", "")
            fields = q.get("fields", [])
            field_name = fields[0].get("name", "") if fields else ""

            try:
                if q_type in ("boolean", "multi_value_single_select"):
                    # Radio / select
                    try:
                        # Try select
                        sel = page.locator(f'select[id="{field_name}"], select[name="{field_name}"]').first
                        sel.select_option(label=answer)
                    except Exception:
                        # Try radio label
                        _answer_label_on_page(page, label, answer)
                elif q_type == "boolean":
                    # Checkbox
                    cb = page.locator(f'input[type="checkbox"][name="{field_name}"]').first
                    if answer in ("Yes", "1", "true") and not cb.is_checked():
                        cb.click()
                else:
                    # Text input
                    try:
                        page.locator(
                            f'input[id="{field_name}"], input[name="{field_name}"], '
                            f'textarea[id="{field_name}"], textarea[name="{field_name}"]'
                        ).first.fill(answer)
                    except Exception:
                        _fill_by_label(page, label[:40], answer)
                time.sleep(0.15)
            except Exception as exc:
                logger.debug(f"    Q skip [{label[:30]}]: {exc}")

        # ── Step 6: EEO checkboxes / consent ──────────────────────────────
        print("[6] Handling consent / EEO…")
        try:
            # Check any "I agree" / privacy policy checkboxes
            for cb in page.locator(
                'input[type="checkbox"]:visible'
            ).all():
                try:
                    aria = (cb.get_attribute("aria-label") or "").lower()
                    name = (cb.get_attribute("name") or "").lower()
                    if any(k in aria + name for k in ("consent", "agree", "policy", "acknowledge")):
                        if not cb.is_checked():
                            cb.click()
                            time.sleep(0.1)
                except Exception:
                    pass
        except Exception:
            pass

        # ── Step 7: Submit ─────────────────────────────────────────────────
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        time.sleep(1)

        print("\n" + "=" * 60)
        print(f"  All fields filled. Please review and click Submit")
        print(f"  in the browser. Waiting up to 10 minutes…")
        print("=" * 60 + "\n")

        # Try auto-submit
        submitted = False
        for sel in [
            'button[type="submit"]:has-text("Submit")',
            'button:has-text("Submit Application")',
            '[data-qa="btn-submit"]',
            'input[type="submit"]',
        ]:
            try:
                btn = page.locator(sel).first
                if btn.is_visible(timeout=3000):
                    btn.click()
                    _settle(page, 5000)
                    submitted = True
                    break
            except Exception:
                pass

        # Wait for confirmation (auto-submit or manual)
        try:
            page.wait_for_url(
                re.compile(r"(confirmation|thank|submitted|success)", re.I),
                timeout=600_000,
            )
        except Exception:
            pass

        final_url = page.url
        print(f"[RESULT] {final_url}")

        if any(k in final_url.lower() for k in ("confirm", "thank", "submit", "success")):
            print("[SUCCESS] Application submitted!")
            page.close()
            return True
        # Check for success text on page
        try:
            body = page.inner_text("body")[:500]
            if any(k in body.lower() for k in ("thank you", "application received", "successfully")):
                print("[SUCCESS] Confirmation text detected.")
                page.close()
                return True
        except Exception:
            pass

        page.screenshot(path=f"/tmp/gh_apply_{board}_{job_id}.png")
        print(f"[WARN] Not confirmed — screenshot at /tmp/gh_apply_{board}_{job_id}.png")
        page.close()
        return False

    except Exception as exc:
        print(f"[ERROR] {exc}")
        try:
            page.screenshot(path=f"/tmp/gh_error_{board}_{job_id}.png")
        except Exception:
            pass
        page.close()
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

    sess = requests.Session()
    sess.headers.update(_HEADERS)
    sess.verify = False

    if args.dry_run:
        for j in jobs:
            apply_to_job(j, None, sess, dry_run=True)
        return

    # ── Launch Playwright browser with Chrome cookies ──────────────────────
    chrome_src = os.path.expanduser("~/Library/Application Support/Google/Chrome/Default")
    tmp = tempfile.mkdtemp(prefix="chrome_gh_apply_")
    dst = os.path.join(tmp, "Default")
    os.makedirs(dst, exist_ok=True)
    for f in ("Cookies", "Cookies-journal"):
        s = os.path.join(chrome_src, f)
        if os.path.isfile(s):
            shutil.copy2(s, os.path.join(dst, f))

    applied_ids = []
    skipped     = []

    with sync_playwright() as pw:
        try:
            ctx = pw.chromium.launch_persistent_context(
                tmp,
                channel="chrome",
                headless=False,
                args=["--disable-blink-features=AutomationControlled"],
                viewport={"width": 1280, "height": 900},
            )
        except Exception:
            ctx = pw.chromium.launch_persistent_context(
                tmp,
                headless=False,
                args=["--disable-blink-features=AutomationControlled"],
                viewport={"width": 1280, "height": 900},
            )

        for j in jobs:
            try:
                ok = apply_to_job(j, ctx, sess)
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

        ctx.close()

    shutil.rmtree(tmp, ignore_errors=True)

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
