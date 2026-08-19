#!/usr/bin/env python3
"""
apply_smartrecruiters.py
------------------------
Playwright-based automation for SmartRecruiters ATS career portals.

Supported companies (same platform, different company slugs):
  - ServiceNow  : jobs.smartrecruiters.com/ServiceNow

Flow (from HAR analysis):
  1. Navigate to job apply page (oneclick-ui)
  2. Upload resume PDF → parsed via /resume/parse API
  3. Fill contact info (name, email, phone, location)
  4. Answer screening questions (Yes/No + multiple choice)
  5. Add message to employer (cover note)
  6. Pause at Submit for manual click (DataDome bot protection)
  7. Detect confirmation page → mark success

Usage:
    python3 apply_smartrecruiters.py --job-url "https://jobs.smartrecruiters.com/ServiceNow/..."
    python3 apply_smartrecruiters.py --dry-run --job-url "..."
"""

import argparse
import json
import os
import re
import shutil
import sys
import tempfile
import time

from playwright.sync_api import sync_playwright
from playwright_stealth import Stealth

# ── Candidate profile ─────────────────────────────────────────────────────────

PROFILE = {
    "first_name":    "Kaushal",
    "last_name":     "Jha",
    "email":         "kaushalkumarjha727219@gmail.com",
    "phone":         "+919818147393",
    "country":       "India",
    "city":          "Bengaluru",
    "linkedin":      "https://www.linkedin.com/in/kaushal-kumar-jha-93b77512a/",
    "resume_pdf":    os.path.expanduser(
        "~/gitQW/IO/Resume/job-hunter/profiles/kaushal-kumar-jha/output/"
        "2026-07-31/Okta-Staff_Fullstack_Engineer/resume.pdf"
    ),
    "cover_note":    (
        "I am a Staff Software Engineer with 8+ years of experience in full-stack "
        "development, distributed systems, and cloud-native architecture. "
        "I am excited about this opportunity and believe my skills in Java, "
        "React, Kubernetes, and microservices align well with this role."
    ),
}

# ── Helpers ───────────────────────────────────────────────────────────────────

def settle(page, ms: int = 2000):
    try:
        page.wait_for_load_state("networkidle", timeout=ms)
    except Exception:
        pass
    time.sleep(0.4)


def safe_fill(page, selector: str, value: str, timeout: int = 5000) -> bool:
    try:
        loc = page.locator(selector).first
        loc.wait_for(state="visible", timeout=timeout)
        loc.fill(value)
        return True
    except Exception as ex:
        print(f"  [WARN] fill '{selector}': {ex}")
        return False


def click_if_visible(page, selector: str, timeout: int = 3000) -> bool:
    try:
        loc = page.locator(selector).first
        if loc.is_visible(timeout=timeout):
            loc.click()
            return True
    except Exception:
        pass
    return False


YES_KEYWORDS = (
    "18 years", "legally authorized", "authorized to work",
    "eligible to work", "right to work", "citizen", "permanent resident",
    "legally entitled", "legal age", "age requirement",
)
NO_KEYWORDS = (
    "bonded", "bond period", "relatives", "family member",
    "refer", "referral", "previous employment", "applied before",
    "conflict of interest", "criminal", "non-compete", "sponsorship",
    "require.*sponsor", "visa sponsor",
)


def answer_screening_questions(page):
    """Answer Yes/No and other screening questions in SmartRecruiters form."""
    answered = 0

    # SmartRecruiters question containers
    containers = page.locator(
        '[data-test="question"], .application-question, '
        '[class*="question"], fieldset'
    ).all()

    for container in containers:
        try:
            q_text = container.inner_text(timeout=500).lower()
        except Exception:
            continue

        want_yes = any(kw in q_text for kw in YES_KEYWORDS)
        want_no  = any(kw in q_text for kw in NO_KEYWORDS) and not want_yes

        if want_yes:
            for sel in (
                'label:has-text("Yes")', 'input[value="yes"]',
                'input[value="Yes"]', 'input[value="1"]',
                '[aria-label="Yes"]',
            ):
                try:
                    el = container.locator(sel).first
                    if el.is_visible(timeout=400):
                        el.evaluate("el => el.click()")
                        answered += 1
                        time.sleep(0.15)
                        break
                except Exception:
                    pass
        elif want_no:
            for sel in (
                'label:has-text("No")', 'input[value="no"]',
                'input[value="No"]', 'input[value="0"]',
                '[aria-label="No"]',
            ):
                try:
                    el = container.locator(sel).first
                    if el.is_visible(timeout=400):
                        el.evaluate("el => el.click()")
                        answered += 1
                        time.sleep(0.15)
                        break
                except Exception:
                    pass

    print(f"  Answered {answered} screening questions")


# ── Main apply function ───────────────────────────────────────────────────────

def apply(job_url: str, dry_run: bool = False):
    resume_path = PROFILE["resume_pdf"]
    if not os.path.isfile(resume_path):
        print(f"[ERROR] Resume PDF not found: {resume_path}")
        sys.exit(1)

    print(f"[INFO] Job URL  : {job_url}")
    print(f"[INFO] Resume   : {resume_path}")

    # Copy Chrome cookies for DataDome trust score
    chrome_src = os.path.expanduser("~/Library/Application Support/Google/Chrome/Default")
    tmp = tempfile.mkdtemp(prefix="chrome_sr_")
    dst = os.path.join(tmp, "Default")
    os.makedirs(dst, exist_ok=True)
    for f in ("Cookies", "Cookies-journal"):
        s = os.path.join(chrome_src, f)
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

        # ── Step 1: Navigate to job page ──────────────────────────────────────
        print(f"\n[STEP 1] Loading job page...")
        page.goto(job_url, wait_until="networkidle", timeout=30000)
        settle(page, 3000)
        print(f"  Title: {page.title()}")

        if dry_run:
            print("[DRY RUN] Page loaded. Exiting without applying.")
            ctx.close()
            return

        # ── Step 2: Click Apply / OneClick Apply button ───────────────────────
        print("[STEP 2] Clicking Apply...")
        for sel in (
            'a[data-test="apply-button"]', 'button[data-test="apply-button"]',
            '[data-test="btn-apply"]', 'a:has-text("Apply")',
            'button:has-text("Apply Now")', 'button:has-text("Apply")',
        ):
            if click_if_visible(page, sel, 4000):
                print(f"  Clicked: {sel}")
                settle(page, 3000)
                break

        # ── Step 3: Upload resume ─────────────────────────────────────────────
        print("[STEP 3] Uploading resume...")
        try:
            # SmartRecruiters has a file input inside the resume section
            fi = page.locator('input[type="file"]').first
            fi.wait_for(state="attached", timeout=15000)
            fi.set_input_files(resume_path)
            settle(page, 5000)
            print("  Resume uploaded.")
        except Exception as ex:
            print(f"  [WARN] Upload: {ex}")

        # ── Step 4: Fill contact info ─────────────────────────────────────────
        print("[STEP 4] Filling contact info...")

        # SmartRecruiters uses data-test attributes for form fields
        field_map = [
            ('[data-test="input-firstName"], input[name="firstName"], input[placeholder*="First"]', PROFILE["first_name"]),
            ('[data-test="input-lastName"],  input[name="lastName"],  input[placeholder*="Last"]',  PROFILE["last_name"]),
            ('[data-test="input-email"],     input[name="email"],     input[type="email"]',          PROFILE["email"]),
            ('[data-test="input-phone"],     input[name="phone"],     input[type="tel"]',            PROFILE["phone"]),
        ]
        for selectors, val in field_map:
            for s in selectors.split(","):
                s = s.strip()
                try:
                    el = page.locator(s).first
                    if el.is_visible(timeout=1500):
                        current = el.input_value()
                        if not current:
                            el.fill(val)
                        break
                except Exception:
                    pass

        # Location / city
        for sel in (
            '[data-test="input-city"]', 'input[name="city"]',
            'input[placeholder*="City"]',
        ):
            try:
                el = page.locator(sel).first
                if el.is_visible(timeout=1000):
                    if not el.input_value():
                        el.fill(PROFILE["city"])
                    break
            except Exception:
                pass

        # LinkedIn URL
        for sel in (
            'input[placeholder*="LinkedIn"]', 'input[name*="linkedin"]',
            'input[placeholder*="Website"]',
        ):
            try:
                el = page.locator(sel).first
                if el.is_visible(timeout=1000):
                    el.fill(PROFILE["linkedin"])
                    break
            except Exception:
                pass

        # ── Step 5: Answer screening questions ────────────────────────────────
        print("[STEP 5] Answering screening questions...")
        answer_screening_questions(page)

        # ── Step 6: Message to employer ───────────────────────────────────────
        print("[STEP 6] Adding cover note...")
        for sel in (
            'textarea[name="message"]', 'textarea[data-test*="message"]',
            'textarea[placeholder*="message"]', 'textarea[placeholder*="cover"]',
            'textarea',
        ):
            try:
                el = page.locator(sel).first
                if el.is_visible(timeout=1000):
                    if not el.input_value():
                        el.fill(PROFILE["cover_note"])
                    break
            except Exception:
                pass

        # ── Step 7: Human-like activity ───────────────────────────────────────
        print("[STEP 7] Simulating human activity...")
        for y in range(0, 1600, 200):
            page.evaluate(f"window.scrollTo(0, {y})")
            time.sleep(0.2)
        for y in range(1600, 0, -300):
            page.evaluate(f"window.scrollTo(0, {y})")
            time.sleep(0.2)
        page.mouse.move(640, 400)
        time.sleep(0.3)
        page.mouse.move(900, 600)
        time.sleep(2)

        # Scroll to Submit
        try:
            submit_btn = page.locator(
                'button[data-test="btn-submit"], button:has-text("Submit"), '
                'button[type="submit"]'
            ).first
            submit_btn.evaluate("el => el.scrollIntoView({block:'center'})")
            time.sleep(1)
        except Exception:
            pass

        # ── Step 8: Pause for manual Submit ──────────────────────────────────
        print("\n" + "=" * 60)
        print("  ALL FIELDS FILLED. Please review and click Submit")
        print("  in the browser window. Waiting up to 10 minutes...")
        print("=" * 60 + "\n")

        try:
            page.wait_for_url(
                "**/confirmation**|**/thank-you**|**/thankyou**"
                "|**/success**|**applied=true**",
                timeout=600_000,
            )
        except Exception:
            pass

        final_url = page.url
        print(f"\n[RESULT] {final_url}")
        if any(k in final_url.lower() for k in ("confirm", "thank", "success", "applied")):
            print("[SUCCESS] Application submitted!")
        else:
            page.screenshot(path="/tmp/sr_apply_result.png")
            print("[WARN] Not confirmed — screenshot at /tmp/sr_apply_result.png")

        time.sleep(5)
        ctx.close()


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Apply to a SmartRecruiters job (ServiceNow, etc.)"
    )
    parser.add_argument(
        "--job-url",
        required=True,
        help='Full job URL, e.g. "https://jobs.smartrecruiters.com/ServiceNow/..."',
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Load the page without filling or submitting",
    )
    args = parser.parse_args()
    apply(args.job_url, dry_run=args.dry_run)
