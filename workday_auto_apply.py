#!/usr/bin/env python3
"""
workday_auto_apply.py
---------------------
Reads high-scoring Workday jobs from the job store and automates applying
to each one using Playwright.

Flow per job:
  1. Navigate to job page
  2. Click Apply (with or without account)
  3. If login page detected, sign in with stored credentials
  4. Click "Autofill with Resume" → upload resume PDF
  5. Fill contact info (name, phone, address, LinkedIn)
  6. Walk through all form steps (Next / Save & Continue)
  7. Questionnaire: Yes for work-auth questions, No for everything else
  8. Pause at Review page — user manually clicks Submit

Usage:
    python workday_auto_apply.py                          # apply to all 7+ score jobs
    python workday_auto_apply.py --min-score 8            # higher threshold
    python workday_auto_apply.py --job-url "https://..."  # single specific job
    python workday_auto_apply.py --company "Morgan Stanley"  # filter by company
    python workday_auto_apply.py --dry-run                # list candidates only
    python workday_auto_apply.py --limit 5                # max N jobs per run
"""

import argparse
import os
import shutil
import sys
import tempfile
import time

from playwright.sync_api import sync_playwright
from playwright_stealth import Stealth

# ── Candidate profile ─────────────────────────────────────────────────────────

PROFILE = {
    "first_name":  "Kaushal",
    "middle_name": "Kumar",
    "last_name":   "Jha",
    "email":       "kaushalkumarjha727219@gmail.com",
    "password":    "Abhiram3$@!",    # Morgan Stanley Workday password
    "phone":       "9818147393",
    "linkedin":    "https://www.linkedin.com/in/kaushal-kumar-jha-93b77512a/",
    "address1":    "Mahaveer Ranches , Sree sai layout , Prapanna Agarpara",
    "city":        "Bengaluru",
    "postal_code": "560100",
    # Default resume PDF — overridden per-job if a tailored PDF exists
    "resume_pdf":  os.path.expanduser(
        "~/gitQW/IO/Resume/job-hunter/profiles/kaushal-kumar-jha/output/"
        "2026-07-31/Okta-Staff_Fullstack_Engineer/resume.pdf"
    ),
}

# Tenants where we have a registered account (email + password)
# Map: subdomain → password  (email is always PROFILE["email"])
TENANT_CREDENTIALS = {
    "ms": PROFILE["password"],   # Morgan Stanley
    # Add more tenants as accounts are created:
    # "nvidia": "...",
    # "paypal": "...",
}

# ── Helpers ───────────────────────────────────────────────────────────────────

def settle(page, ms: int = 2000):
    try:
        page.wait_for_load_state("networkidle", timeout=ms)
    except Exception:
        pass
    time.sleep(0.5)


def safe_fill(page, aid: str, value: str, timeout: int = 8000):
    try:
        loc = page.locator(f'[data-automation-id="{aid}"]').first
        loc.wait_for(state="visible", timeout=timeout)
        loc.fill(value)
    except Exception as ex:
        print(f"  [WARN] fill {aid}: {ex}")


def try_next(page) -> bool:
    for aid in ("bottom-navigation-next-btn", "next-btn", "saveAndContinueButton",
                "bottom-navigation-next-button"):
        try:
            b = page.locator(f'[data-automation-id="{aid}"]').first
            if b.is_visible(timeout=3000):
                b.click()
                settle(page, 3000)
                return True
        except Exception:
            pass
    return False


def dismiss_overlays(page):
    """Dismiss cookie banners, modals, etc."""
    for sel in [
        '[data-automation-id="skipToContent"]',
        'button[aria-label="Close"]',
        '.cookie-accept', '#onetrust-accept-btn-handler',
    ]:
        try:
            el = page.locator(sel).first
            if el.is_visible(timeout=1500):
                el.click()
                time.sleep(0.3)
        except Exception:
            pass


def _resume_for_job(job: dict) -> str:
    """Return the best available tailored resume PDF for this job."""
    # Check for job's own tailored PDF first
    pdf = job.get("pdf_path")
    if pdf and os.path.isfile(pdf):
        return pdf
    # Fall back to profile default
    return PROFILE["resume_pdf"]


def _tenant_from_url(job_url: str) -> str | None:
    """Extract Workday tenant subdomain from a job URL."""
    import re
    m = re.match(r"https?://([^.]+)\.wd\d+\.myworkdayjobs\.com", job_url, re.I)
    return m.group(1) if m else None


# ── Login ────────────────────────────────────────────────────────────────────

def _try_login(page, tenant: str):
    """
    Attempt to sign in at the Workday tenant's login page.
    Only runs if we have stored credentials for the tenant.
    """
    password = TENANT_CREDENTIALS.get(tenant)
    if not password:
        return  # No credentials — continue as guest

    # Check if we're already at a login page
    url = page.url
    if "/login" not in url:
        # Navigate to tenant login
        base = re.match(r"(https?://[^/]+)", url)
        if base:
            login_url = base.group(1) + "/en-US/External/login"
            print(f"  [login] Navigating to {login_url}")
            page.goto(login_url, wait_until="networkidle", timeout=30000)
            settle(page, 2000)

    email_sel = '[data-automation-id="email"]'
    pwd_sel   = '[data-automation-id="password"]'
    try:
        page.locator(email_sel).first.wait_for(state="visible", timeout=10000)
        page.locator(email_sel).first.fill(PROFILE["email"])
        page.locator(pwd_sel).first.fill(password)
        # JS-click the sign-in button (bypasses Workday's click_filter overlay)
        try:
            page.locator('[data-automation-id="click_filter"]').first.evaluate(
                "el => el.click()"
            )
            settle(page, 1000)
        except Exception:
            pass
        page.locator(pwd_sel).first.press("Enter")
        try:
            page.wait_for_url("**/userHome**", timeout=15000)
        except Exception:
            pass
        settle(page, 3000)
        print(f"  [login] Signed in. URL: {page.url}")
    except Exception as exc:
        print(f"  [login] WARN: {exc}")


# ── Apply to one job ──────────────────────────────────────────────────────────

def apply_to_job(job: dict, ctx, dry_run: bool = False):
    """
    Apply to one Workday job using an existing browser context.
    Returns True if we reached the review / thank-you page.
    """
    job_url    = job["apply_link"]
    resume_pdf = _resume_for_job(job)
    tenant     = _tenant_from_url(job_url) or ""

    print(f"\n{'='*60}")
    print(f"  JOB  : {job['title']} @ {job['company']}")
    print(f"  SCORE: {job.get('fit_score', '?')}/10  — {job.get('fit_reason', '')}")
    print(f"  URL  : {job_url}")
    print(f"  PDF  : {resume_pdf}")
    print(f"{'='*60}\n")

    if not os.path.isfile(resume_pdf):
        print(f"  [SKIP] Resume PDF not found: {resume_pdf}")
        return False

    if dry_run:
        return True

    page = ctx.new_page()
    Stealth().apply_stealth_sync(page)

    try:
        # ── Step 1: Login (if credentials available) ─────────────────────────
        if tenant in TENANT_CREDENTIALS:
            login_base = re.match(r"(https?://[^/]+)", job_url).group(1)
            login_url  = login_base + "/en-US/External/login"
            print(f"[STEP 1] Logging in at {login_url}")
            page.goto(login_url, wait_until="networkidle", timeout=30000)
            settle(page, 2000)
            _try_login(page, tenant)
        else:
            print("[STEP 1] No stored credentials — applying as guest")

        # ── Step 2: Navigate to job page ──────────────────────────────────────
        print(f"[STEP 2] Loading job page: {job_url}")
        page.goto(job_url, wait_until="domcontentloaded", timeout=30000)
        settle(page, 3000)
        dismiss_overlays(page)
        print(f"  Title: {page.title()}")

        # ── Step 3: Click Apply ───────────────────────────────────────────────
        print("[STEP 3] Clicking Apply…")
        clicked_apply = False
        for aid in ("applyWithAccountButton", "applyButton", "apply",
                    "applyNowBtn", "submitInterestButton"):
            try:
                b = page.locator(f'[data-automation-id="{aid}"]').first
                if b.is_visible(timeout=5000):
                    b.click()
                    print(f"  Clicked: {aid}")
                    settle(page, 4000)
                    clicked_apply = True
                    break
            except Exception:
                pass
        if not clicked_apply:
            # Text-based fallback
            try:
                page.locator('a:has-text("Apply"), button:has-text("Apply Now")').first.click()
                settle(page, 4000)
                clicked_apply = True
            except Exception:
                pass

        if not clicked_apply:
            print("  [WARN] Could not find Apply button — may already be on apply flow")

        # ── Step 4: Handle "Sign In" prompt if it appears ─────────────────────
        # Some tenants redirect to account creation after clicking Apply
        if "signIn" in page.url or "login" in page.url.lower():
            print("[STEP 4] Login prompt detected — signing in")
            _try_login(page, tenant)
            # Re-navigate to job after login
            page.goto(job_url, wait_until="domcontentloaded", timeout=30000)
            settle(page, 3000)
            for aid in ("applyWithAccountButton", "applyButton"):
                try:
                    b = page.locator(f'[data-automation-id="{aid}"]').first
                    if b.is_visible(timeout=5000):
                        b.click()
                        settle(page, 4000)
                        break
                except Exception:
                    pass

        # ── Step 5: Autofill with Resume ──────────────────────────────────────
        print("[STEP 5] Autofill with Resume…")
        for aid in ("autofillWithResume", "fillWithResume", "resumeUpload",
                    "fillWithResumeBtn"):
            try:
                b = page.locator(f'[data-automation-id="{aid}"]').first
                if b.is_visible(timeout=6000):
                    b.click()
                    print(f"  Clicked: {aid}")
                    settle(page, 4000)
                    break
            except Exception:
                pass
        else:
            try:
                page.locator(
                    'button:has-text("Autofill"), a:has-text("Autofill")'
                ).first.click()
                settle(page, 3000)
            except Exception:
                pass

        # ── Step 6: Upload resume ─────────────────────────────────────────────
        print("[STEP 6] Uploading resume…")
        try:
            fi = page.locator(
                '[data-automation-id="file-upload-input-ref"], input[type="file"]'
            ).first
            fi.wait_for(state="attached", timeout=15000)
            fi.set_input_files(resume_pdf)
            settle(page, 8000)
            print("  Resume uploaded.")
        except Exception as exc:
            print(f"  [WARN] Upload: {exc}")

        try_next(page)
        settle(page, 3000)

        # ── Step 7: Fill contact info ─────────────────────────────────────────
        print("[STEP 7] Filling contact info…")
        safe_fill(page, "legalNameSection_firstName",  PROFILE["first_name"])
        safe_fill(page, "legalNameSection_lastName",   PROFILE["last_name"])
        safe_fill(page, "legalNameSection_middleName", PROFILE["middle_name"])
        safe_fill(page, "addressSection_addressLine1", PROFILE["address1"])
        safe_fill(page, "addressSection_city",         PROFILE["city"])
        safe_fill(page, "addressSection_postalCode",   PROFILE["postal_code"])
        safe_fill(page, "phone-number",                PROFILE["phone"])
        # Email fields (guest flow)
        safe_fill(page, "email",                       PROFILE["email"])
        safe_fill(page, "emailAddress",                PROFILE["email"])
        try_next(page)

        # ── Step 8: Walk through remaining steps ──────────────────────────────
        print("[STEP 8] Walking through form steps…")

        # Work experience
        try_next(page)
        settle(page, 2000)

        # Education
        try_next(page)
        settle(page, 2000)

        # Skills
        try_next(page)
        settle(page, 2000)

        # Social / LinkedIn
        safe_fill(page, "linkedInAccount", PROFILE["linkedin"])
        try_next(page)
        settle(page, 2000)

        # ── Step 9: Questionnaire ─────────────────────────────────────────────
        print("[STEP 9] Answering questionnaire…")
        yes_keywords = (
            "legally authorized", "authorized to work", "eligible to work",
            "right to work", "citizen or permanent", "sponsorship not required",
        )
        for _ in range(6):   # up to 6 questionnaire pages
            answered = False
            # Handle radio buttons
            for radio in page.locator(
                '[data-automation-id*="radioBtn"]:visible'
            ).all():
                try:
                    label_text = ""
                    try:
                        label_text = radio.inner_text(timeout=500).lower()
                    except Exception:
                        pass
                    # Look at surrounding label text for context
                    question_text = ""
                    try:
                        question_text = radio.evaluate(
                            "el => { let p = el.closest('[data-automation-id*=\"formField\"],"
                            ".css-1o47f6n'); if(p) return p.innerText; return ''; }"
                        ).lower()
                    except Exception:
                        pass
                    full_text = label_text + " " + question_text
                    is_yes_answer = any(kw in full_text for kw in yes_keywords)
                    if is_yes_answer and "yes" in label_text:
                        radio.evaluate("el => el.click()")
                        answered = True
                        time.sleep(0.2)
                    elif not is_yes_answer and "no" in label_text:
                        radio.evaluate("el => el.click()")
                        answered = True
                        time.sleep(0.2)
                except Exception:
                    pass
            # Handle Yes/No labels as text
            for label in page.locator('label:has-text("No")').all():
                try:
                    label.click()
                    answered = True
                    time.sleep(0.15)
                except Exception:
                    pass
            # Source question
            try:
                page.locator('label:has-text("Word of Mouth")').first.click()
            except Exception:
                pass
            if answered or try_next(page):
                settle(page, 2000)
            else:
                break

        # ── Step 10: Review — pause for manual Submit ─────────────────────────
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        time.sleep(1)

        print("\n" + "=" * 60)
        print(f"  JOB : {job['title']} @ {job['company']}")
        print("  ALL FIELDS FILLED. Please review and click Submit")
        print("  in the browser window. Waiting up to 10 minutes…")
        print("=" * 60 + "\n")

        try:
            page.wait_for_url("**thankyou**", timeout=600_000)
        except Exception:
            pass

        final_url = page.url
        print(f"\n[RESULT] {final_url}")
        if any(k in final_url.lower() for k in ("thank", "confirm", "submitted")):
            print("[SUCCESS] Application submitted!")
            page.close()
            return True
        else:
            page.screenshot(path=f"/tmp/wd_apply_{tenant}.png")
            print(f"[WARN] Not confirmed — screenshot at /tmp/wd_apply_{tenant}.png")
            page.close()
            return False

    except Exception as exc:
        print(f"[ERROR] Unexpected error: {exc}")
        try:
            page.screenshot(path=f"/tmp/wd_error_{tenant}.png")
        except Exception:
            pass
        page.close()
        return False


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Auto-apply to Workday jobs from job store")
    parser.add_argument("--min-score", type=int,  default=7,
                        help="Minimum fit_score to consider (default: 7)")
    parser.add_argument("--job-url",               help="Apply to a single specific Workday job URL")
    parser.add_argument("--company",               help="Filter to jobs from this company")
    parser.add_argument("--limit",   type=int,    help="Max number of jobs to apply to per run")
    parser.add_argument("--dry-run", action="store_true", help="List candidates only, no browser")
    parser.add_argument("--profile", default="kaushal-kumar-jha", help="Profile slug")
    args = parser.parse_args()

    import profiles
    profiles.set_active_profile(args.profile)
    import job_store

    # ── Build candidate list ──────────────────────────────────────────────────
    if args.job_url:
        # Single job from URL
        jobs = [{
            "id": "manual",
            "title": "Job from URL",
            "company": "",
            "location": "",
            "apply_link": args.job_url,
            "fit_score": 10,
            "fit_reason": "Manually specified",
            "pdf_path": None,
            "ats_type": "workday",
        }]
    else:
        all_jobs = job_store.all_jobs()
        jobs = [
            j for j in all_jobs
            if j.get("ats_type") == "workday"
            and j.get("fit_score", 0) >= args.min_score
            and not j.get("applied_at")
            and not j.get("removed")
        ]
        if args.company:
            needle = args.company.lower()
            jobs = [j for j in jobs if needle in j.get("company", "").lower()]
        # Sort by score descending
        jobs.sort(key=lambda j: j.get("fit_score", 0), reverse=True)
        if args.limit:
            jobs = jobs[:args.limit]

    if not jobs:
        print(f"No Workday jobs found with fit_score >= {args.min_score} and not yet applied.")
        print("Run:  python fetch_workday.py  to fetch jobs first.")
        sys.exit(0)

    print(f"\nFound {len(jobs)} Workday job(s) to apply to:\n")
    for j in jobs:
        print(f"  [{j.get('fit_score','?')}/10] {j.get('title','?')} @ {j.get('company','?')}")
        print(f"    {j.get('apply_link','')}")
    print()

    if args.dry_run:
        return

    # ── Launch browser ────────────────────────────────────────────────────────
    # Copy Chrome cookies for better trust score
    chrome_src = os.path.expanduser("~/Library/Application Support/Google/Chrome/Default")
    tmp = tempfile.mkdtemp(prefix="chrome_wd_apply_")
    dst = os.path.join(tmp, "Default")
    os.makedirs(dst, exist_ok=True)
    for f in ("Cookies", "Cookies-journal"):
        s = os.path.join(chrome_src, f)
        if os.path.isfile(s):
            shutil.copy2(s, os.path.join(dst, f))

    applied_ids = []
    failed_ids  = []

    with sync_playwright() as pw:
        try:
            ctx = pw.chromium.launch_persistent_context(
                user_data_dir=tmp, headless=False, channel="chrome",
                viewport={"width": 1280, "height": 900},
                args=["--disable-blink-features=AutomationControlled"],
            )
        except Exception:
            ctx = pw.chromium.launch_persistent_context(
                user_data_dir=tmp, headless=False,
                viewport={"width": 1280, "height": 900},
                args=["--disable-blink-features=AutomationControlled"],
            )

        for job in jobs:
            success = apply_to_job(job, ctx, dry_run=False)
            if success and job["id"] != "manual":
                job_store.mark_applied(job["id"])
                applied_ids.append(job["id"])
                print(f"  Marked as applied: {job['id']}")
            elif not success:
                failed_ids.append(job["id"])

            # Small pause between applications
            if len(jobs) > 1:
                time.sleep(3)

        ctx.close()

    # ── Summary ───────────────────────────────────────────────────────────────
    print("\n" + "=" * 60)
    print(f"  Applied:  {len(applied_ids)}")
    print(f"  Failed:   {len(failed_ids)}")
    print("=" * 60)


if __name__ == "__main__":
    import re
    main()
