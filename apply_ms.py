#!/usr/bin/env python3
"""
apply_ms.py
-----------
Automates job application on Morgan Stanley's Workday careers site
(ms.wd5.myworkdayjobs.com).

Flow (from HAR analysis):
  1. Navigate directly to login page → fill credentials → sign in
  2. Navigate to job URL → click Apply
  3. Click "Autofill with Resume" → upload resume PDF
  4. Walk through multi-step form (contact, work, education, skills, social,
     questionnaire)
  5. Review page → pause for manual Submit click

Usage:
    python3 apply_ms.py --job-url "https://ms.wd5.myworkdayjobs.com/en-US/External/job/Bengaluru-India/Lead-Engineer-Vice-President_JR038173-2"
    python3 apply_ms.py --dry-run
"""

import argparse
import os
import shutil
import sys
import tempfile

import dotenv
dotenv.load_dotenv(os.path.join(os.path.dirname(__file__), ".env"), override=False)

def _e(key: str, default: str = "") -> str:
    return os.environ.get(key, default)
import time

from playwright.sync_api import sync_playwright
from playwright_stealth import Stealth

# ── Candidate profile ────────────────────────────────────────────────────────

PROFILE = {
    "first_name":  _e("CANDIDATE_FIRST_NAME"),
    "middle_name": _e("CANDIDATE_MIDDLE_NAME"),
    "last_name":   _e("CANDIDATE_LAST_NAME"),
    "email":       _e("CANDIDATE_EMAIL"),
    "password":    _e("WORKDAY_MS_PASSWORD"),
    "phone":       _e("CANDIDATE_PHONE"),
    "linkedin":    _e("CANDIDATE_LINKEDIN"),
    "address1":    _e("CANDIDATE_ADDRESS"),
    "city":        _e("CANDIDATE_CITY"),
    "postal_code": _e("CANDIDATE_POSTAL_CODE"),
    "resume_pdf":  os.path.expanduser(_e("CANDIDATE_RESUME_PDF")),
}

DEFAULT_JOB_URL = (
    "https://ms.wd5.myworkdayjobs.com/en-US/External/job/"
    "Bengaluru-India/Lead-Engineer-Vice-President_JR038173-2"
)

LOGIN_URL = "https://ms.wd5.myworkdayjobs.com/en-US/External/login"

# ── Helpers ───────────────────────────────────────────────────────────────────

def settle(page, ms=2000):
    try:
        page.wait_for_load_state("networkidle", timeout=ms)
    except Exception:
        pass
    time.sleep(0.5)


def safe_fill(page, aid, value, timeout=8000):
    try:
        loc = page.locator(f'[data-automation-id="{aid}"]').first
        loc.wait_for(state="visible", timeout=timeout)
        loc.fill(value)
    except Exception as ex:
        print(f"  [WARN] fill {aid}: {ex}")


def try_next(page):
    for aid in ("bottom-navigation-next-btn", "next-btn", "saveAndContinueButton"):
        try:
            b = page.locator(f'[data-automation-id="{aid}"]').first
            if b.is_visible(timeout=3000):
                b.click()
                settle(page, 3000)
                return True
        except Exception:
            pass
    return False


# ── Main automation ───────────────────────────────────────────────────────────

def apply(job_url: str, dry_run: bool = False):
    resume_path = PROFILE["resume_pdf"]
    if not os.path.exists(resume_path):
        print(f"[ERROR] Resume PDF not found: {resume_path}")
        sys.exit(1)

    print(f"[INFO] Job URL: {job_url}")

    # Copy Chrome cookies for better trust score
    src = os.path.expanduser("~/Library/Application Support/Google/Chrome/Default")
    tmp = tempfile.mkdtemp(prefix="chrome_ms_")
    dst = os.path.join(tmp, "Default")
    os.makedirs(dst, exist_ok=True)
    for f in ("Cookies", "Cookies-journal"):
        s = os.path.join(src, f)
        if os.path.isfile(s):
            shutil.copy2(s, os.path.join(dst, f))
            print(f"[INFO] Copied {f}")

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

        page = ctx.new_page()
        Stealth().apply_stealth_sync(page)

        # ── Step 1: Sign in ───────────────────────────────────────────────────
        print(f"[STEP 1] Signing in at {LOGIN_URL} ...")
        page.goto(LOGIN_URL, wait_until="networkidle", timeout=30000)
        settle(page, 2000)
        print(f"  Page: {page.title()} | {page.url}")

        if dry_run:
            ctx.close()
            return

        # Workday uses data-automation-id on inputs (not name="username")
        email_sel = '[data-automation-id="email"]'
        pwd_sel   = '[data-automation-id="password"]'
        try:
            page.locator(email_sel).first.wait_for(state="visible", timeout=15000)
            page.locator(email_sel).first.fill(PROFILE["email"])
            page.locator(pwd_sel).first.fill(PROFILE["password"])
            print("  Credentials entered. Submitting...")
            # Workday has a click_filter overlay that intercepts button clicks — JS-click it
            try:
                page.locator('[data-automation-id="click_filter"]').first.evaluate("el => el.click()")
                settle(page, 1000)
            except Exception:
                pass
            # Fallback: press Enter on password field
            page.locator(pwd_sel).first.press("Enter")
            # Wait for redirect to userHome
            try:
                page.wait_for_url("**/userHome**", timeout=15000)
            except Exception:
                pass
            settle(page, 3000)
            print(f"  Signed in. URL: {page.url}")
        except Exception as ex:
            print(f"  [WARN] Login: {ex} | URL: {page.url}")

        # ── Step 2: Navigate to job page ──────────────────────────────────────
        print(f"[STEP 2] Loading job page...")
        page.goto(job_url, wait_until="domcontentloaded", timeout=30000)
        settle(page, 3000)
        print(f"  Job: {page.title()}")

        # ── Step 3: Click Apply ───────────────────────────────────────────────
        print("[STEP 3] Clicking Apply...")
        for aid in ("applyWithAccountButton", "applyButton", "apply"):
            try:
                b = page.locator(f'[data-automation-id="{aid}"]').first
                if b.is_visible(timeout=5000):
                    b.click()
                    print(f"  Clicked: {aid}")
                    settle(page, 4000)
                    break
            except Exception:
                pass

        # ── Step 4: Click "Autofill with Resume" ─────────────────────────────
        print(f"[STEP 4] Autofill with Resume... (URL: {page.url})")
        for aid in ("autofillWithResume", "fillWithResume", "resumeUpload"):
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
                page.locator('button:has-text("Autofill"), a:has-text("Autofill")').first.click()
                settle(page, 3000)
                print("  Clicked Autofill (text).")
            except Exception:
                pass

        # ── Step 5: Upload resume ─────────────────────────────────────────────
        print(f"[STEP 5] Uploading resume... (URL: {page.url})")
        try:
            fi = page.locator(
                '[data-automation-id="file-upload-input-ref"], input[type="file"]'
            ).first
            fi.wait_for(state="attached", timeout=15000)
            fi.set_input_files(resume_path)
            settle(page, 8000)
            print("  Resume uploaded.")
        except Exception as ex:
            print(f"  [WARN] Upload: {ex}")

        # Click Next/Continue after upload
        try_next(page)
        settle(page, 3000)

        # ── Step 6: Fill form steps ───────────────────────────────────────────
        print("[STEP 6] Filling form...")

        print("  [6a] Personal info...")
        safe_fill(page, "legalNameSection_firstName",  PROFILE["first_name"])
        safe_fill(page, "legalNameSection_lastName",   PROFILE["last_name"])
        safe_fill(page, "legalNameSection_middleName", PROFILE["middle_name"])
        safe_fill(page, "addressSection_addressLine1", PROFILE["address1"])
        safe_fill(page, "addressSection_city",         PROFILE["city"])
        safe_fill(page, "addressSection_postalCode",   PROFILE["postal_code"])
        safe_fill(page, "phone-number",                PROFILE["phone"])
        try_next(page)

        print("  [6b] Work experience...")
        try_next(page)

        print("  [6c] Education...")
        try_next(page)

        print("  [6d] Skills...")
        try_next(page)

        print("  [6e] Social profiles...")
        safe_fill(page, "linkedInAccount", PROFILE["linkedin"])
        try_next(page)

        print("  [6f] Questionnaire (No to all)...")
        for _ in range(4):
            answered = False
            for radio in page.locator(
                '[data-automation-id*="radioBtn"]:has-text("No"), label:has-text("No")'
            ).all():
                try:
                    radio.click()
                    answered = True
                    time.sleep(0.2)
                except Exception:
                    pass
            if answered:
                try_next(page)

        print("  [6g] Source / previous worker...")
        try:
            page.locator('label:has-text("Word of Mouth")').first.click()
        except Exception:
            pass
        try_next(page)

        # ── Step 7: Review — pause for manual Submit ──────────────────────────
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        time.sleep(1)

        print("\n" + "="*60)
        print("  ALL FIELDS FILLED. Please review and click Submit")
        print("  in the browser window. Waiting up to 10 minutes...")
        print("="*60 + "\n")

        try:
            page.wait_for_url("**thankyou**", timeout=600000)
        except Exception:
            pass

        final_url = page.url
        print(f"\n[RESULT] {final_url}")
        if any(k in final_url.lower() for k in ("thank", "confirm", "submitted")):
            print("[SUCCESS] Application submitted!")
        else:
            page.screenshot(path="/tmp/ms_apply_result.png")
            print("[WARN] Not confirmed — screenshot at /tmp/ms_apply_result.png")

        time.sleep(5)
        ctx.close()


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--job-url", default=DEFAULT_JOB_URL)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    apply(args.job_url, dry_run=args.dry_run)
