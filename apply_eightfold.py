#!/usr/bin/env python3
"""
apply_eightfold.py
------------------
Playwright-based automation for Eightfold AI career portals.

Supported companies (same platform, different domains):
  - Micron Technology     : micron.eightfold.ai
  - Applied Materials     : careers.appliedmaterials.com
  - Any other Eightfold site

Flow (from HAR analysis):
  1. Navigate to job apply page
  2. Fill personal info (name, email, phone, location)
  3. Upload resume PDF
  4. Answer screening questions (Yes/No based on label text)
  5. Pause at Submit for manual click (reCAPTCHA v3 scores the click)
  6. Detect confirmation page → mark success

Usage:
    python3 apply_eightfold.py --job-url "https://micron.eightfold.ai/careers/apply?pid=43363930"
    python3 apply_eightfold.py --job-url "https://careers.appliedmaterials.com/careers/apply?pid=790315863609"
    python3 apply_eightfold.py --dry-run --job-url "..."
"""

import argparse
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
    "first_name": "Kaushal",
    "last_name":  "Jha",
    "email":      "kaushalkumarjha727219@gmail.com",
    "phone":      "+91 9818147393",
    "country":    "India",
    "state":      "Karnataka",
    "city":       "Bengaluru",
    "address1":   "Mahaveer Ranches, Sree Sai Layout, Prapanna Agarpara",
    "postal_code": "560100",
    "linkedin":   "https://www.linkedin.com/in/kaushal-kumar-jha-93b77512a/",
    "current_ctc": "26 LPA",
    "expected_ctc": "32 LPA",
    "notice_period": "1 month",
    "resume_pdf": os.path.expanduser(
        "~/gitQW/IO/Resume/job-hunter/profiles/kaushal-kumar-jha/output/"
        "2026-07-31/Okta-Staff_Fullstack_Engineer/resume.pdf"
    ),
}

# ── Helpers ───────────────────────────────────────────────────────────────────

def settle(page, ms: int = 2000):
    try:
        page.wait_for_load_state("networkidle", timeout=ms)
    except Exception:
        pass
    time.sleep(0.4)


def safe_fill(page, selector: str, value: str, timeout: int = 5000):
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
    "legally entitled",
)
NO_KEYWORDS = (
    "bonded", "bond period", "relatives", "family member",
    "refer", "referral", "previous employment", "applied before",
    "conflict of interest", "criminal", "non-compete",
)


def answer_screening_questions(page):
    """Iterate over visible Yes/No question groups and click the right option."""
    answered = 0
    # Eightfold question containers: div with role="radiogroup" or class containing "question"
    q_containers = page.locator(
        '[role="radiogroup"], .ef-radio-group, .question-container, '
        '.css-question, [data-test="question-field"]'
    ).all()

    for container in q_containers:
        try:
            q_text = container.inner_text(timeout=500).lower()
        except Exception:
            continue

        want_yes = any(kw in q_text for kw in YES_KEYWORDS)
        want_no  = any(kw in q_text for kw in NO_KEYWORDS) and not want_yes

        if want_yes:
            # Click the "Yes" radio inside this container
            for sel in ('label:has-text("Yes")', 'input[value="yes"]',
                        'input[value="Yes"]', '[aria-label="Yes"]'):
                try:
                    el = container.locator(sel).first
                    if el.is_visible(timeout=500):
                        el.evaluate("el => el.click()")
                        answered += 1
                        time.sleep(0.15)
                        break
                except Exception:
                    pass
        elif want_no:
            for sel in ('label:has-text("No")', 'input[value="no"]',
                        'input[value="No"]', '[aria-label="No"]'):
                try:
                    el = container.locator(sel).first
                    if el.is_visible(timeout=500):
                        el.evaluate("el => el.click()")
                        answered += 1
                        time.sleep(0.15)
                        break
                except Exception:
                    pass

    # Fallback: click all visible "No" radio labels that look standalone
    if answered == 0:
        for label in page.locator('label:has-text("No"), label:has-text("no")').all():
            try:
                label.evaluate("el => el.click()")
                answered += 1
                time.sleep(0.15)
            except Exception:
                pass

    print(f"  Answered {answered} screening questions")


def fill_text_question(page, q_text_fragment: str, answer: str):
    """Fill a free-text question containing q_text_fragment."""
    try:
        # Find the label containing the fragment, then fill its associated input
        label = page.locator(f'label:has-text("{q_text_fragment}")').first
        field_id = label.get_attribute("for") or ""
        if field_id:
            page.locator(f"#{field_id}").fill(answer)
        else:
            # Find nearest sibling input/textarea
            label.evaluate(
                f"el => {{ let p = el.closest('div,section'); "
                f"if(p) {{ let i = p.querySelector('input,textarea'); "
                f"if(i) i.value = '{answer}'; }} }}"
            )
    except Exception:
        pass


# ── Main apply function ───────────────────────────────────────────────────────

def apply(job_url: str, dry_run: bool = False):
    resume_path = PROFILE["resume_pdf"]
    if not os.path.isfile(resume_path):
        print(f"[ERROR] Resume PDF not found: {resume_path}")
        sys.exit(1)

    print(f"[INFO] Job URL  : {job_url}")
    print(f"[INFO] Resume   : {resume_path}")

    # Copy Chrome cookies for better reCAPTCHA trust score
    chrome_src = os.path.expanduser("~/Library/Application Support/Google/Chrome/Default")
    tmp = tempfile.mkdtemp(prefix="chrome_ef_")
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

        # ── Step 1: Navigate to apply page ───────────────────────────────────
        print(f"\n[STEP 1] Loading job page...")
        page.goto(job_url, wait_until="networkidle", timeout=30000)
        settle(page, 3000)
        print(f"  Title: {page.title()}")

        if dry_run:
            print("[DRY RUN] Page loaded. Exiting without applying.")
            ctx.close()
            return

        # Dismiss cookie/consent banners
        for sel in ("#onetrust-accept-btn-handler", ".accept-cookies-btn",
                    '[aria-label="Accept All"]', 'button:has-text("Accept")'):
            click_if_visible(page, sel, 2000)
            time.sleep(0.3)

        # ── Step 2: Click "Apply" if on job detail page (not already on apply) ─
        if "/apply" not in page.url:
            print("[STEP 2] Clicking Apply...")
            for sel in (
                'a:has-text("Apply Now")', 'button:has-text("Apply Now")',
                'a:has-text("Apply")',     'button:has-text("Apply")',
                '[data-ph-id="ph-page-element-page-applyButton"]',
            ):
                if click_if_visible(page, sel, 4000):
                    print(f"  Clicked Apply: {sel}")
                    settle(page, 3000)
                    break
        else:
            print("[STEP 2] Already on apply page.")

        # ── Step 3: Fill personal info ────────────────────────────────────────
        print("[STEP 3] Filling personal info...")
        # Eightfold uses various input names / IDs across tenants
        for sel, val in [
            ('input[name="firstname"], input[id*="first"], input[placeholder*="First"]', PROFILE["first_name"]),
            ('input[name="lastname"],  input[id*="last"],  input[placeholder*="Last"]',  PROFILE["last_name"]),
            ('input[name="email"],     input[id*="email"], input[placeholder*="Email"]',  PROFILE["email"]),
            ('input[name="phone"],     input[id*="phone"], input[placeholder*="Phone"]',  PROFILE["phone"]),
            ('input[name="city"],      input[id*="city"],  input[placeholder*="City"]',   PROFILE["city"]),
        ]:
            for s in sel.split(","):
                s = s.strip()
                try:
                    el = page.locator(s).first
                    if el.is_visible(timeout=1500):
                        el.fill(val)
                        break
                except Exception:
                    pass

        # LinkedIn URL if present
        for sel in ('input[placeholder*="LinkedIn"]', 'input[name*="linkedin"]',
                    'input[name*="website"]'):
            try:
                el = page.locator(sel).first
                if el.is_visible(timeout=1000):
                    el.fill(PROFILE["linkedin"])
                    break
            except Exception:
                pass

        # ── Step 4: Upload resume ─────────────────────────────────────────────
        print("[STEP 4] Uploading resume...")
        try:
            fi = page.locator('input[type="file"]').first
            fi.wait_for(state="attached", timeout=15000)
            fi.set_input_files(resume_path)
            settle(page, 6000)
            print("  Resume uploaded.")
        except Exception as ex:
            print(f"  [WARN] Upload: {ex}")

        # Wait for parsing confirmation
        try:
            page.wait_for_selector(
                '.resume-parsed, .parsed-success, [data-test="resume-parsed"]',
                timeout=10000,
            )
            print("  Resume parsed.")
        except Exception:
            pass

        # Re-fill name/email/phone in case resume parse cleared them
        settle(page, 2000)
        for sel, val in [
            ('input[name="firstname"]', PROFILE["first_name"]),
            ('input[name="lastname"]',  PROFILE["last_name"]),
            ('input[name="email"]',     PROFILE["email"]),
            ('input[name="phone"]',     PROFILE["phone"]),
        ]:
            try:
                el = page.locator(sel).first
                if el.is_visible(timeout=800) and not el.input_value():
                    el.fill(val)
            except Exception:
                pass

        # ── Step 5: Answer screening questions ───────────────────────────────
        print("[STEP 5] Answering screening questions...")
        answer_screening_questions(page)

        # CTC / salary questions (text fields)
        fill_text_question(page, "current", PROFILE["current_ctc"])
        fill_text_question(page, "expected", PROFILE["expected_ctc"])
        fill_text_question(page, "notice", PROFILE["notice_period"])

        # ── Step 6: Human-like scrolling for reCAPTCHA v3 score ──────────────
        print("[STEP 6] Simulating human activity...")
        for y in range(0, 1600, 200):
            page.evaluate(f"window.scrollTo(0, {y})")
            time.sleep(0.25)
        for y in range(1600, 0, -300):
            page.evaluate(f"window.scrollTo(0, {y})")
            time.sleep(0.2)
        page.mouse.move(640, 400)
        time.sleep(0.3)
        page.mouse.move(900, 600)
        time.sleep(0.3)
        page.mouse.move(700, 500)
        time.sleep(2)

        # Scroll to Submit button
        try:
            submit_btn = page.locator(
                'button:has-text("Submit"), button[type="submit"], '
                'input[value="Submit Application"]'
            ).first
            submit_btn.evaluate("el => el.scrollIntoView({block:'center'})")
            time.sleep(1)
        except Exception:
            pass

        # ── Step 7: Pause for manual Submit ──────────────────────────────────
        print("\n" + "=" * 60)
        print("  ALL FIELDS FILLED. Please review and click Submit")
        print("  in the browser window. Waiting up to 10 minutes...")
        print("=" * 60 + "\n")

        # Success detection patterns for Eightfold
        try:
            page.wait_for_url(
                "**/apply/success**|**/thankyou**|**/thank-you**"
                "|**/profile-review**|**success=true**",
                timeout=600_000,
            )
        except Exception:
            pass

        final_url = page.url
        print(f"\n[RESULT] {final_url}")
        if any(k in final_url.lower() for k in ("success", "thank", "submitted", "profile-review")):
            print("[SUCCESS] Application submitted!")
        else:
            page.screenshot(path="/tmp/ef_apply_result.png")
            print("[WARN] Not confirmed — screenshot at /tmp/ef_apply_result.png")

        time.sleep(5)
        ctx.close()


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Apply to an Eightfold AI careers job (Micron, Applied Materials, etc.)"
    )
    parser.add_argument(
        "--job-url",
        required=True,
        help='Full apply URL, e.g. "https://micron.eightfold.ai/careers/apply?pid=43363930"',
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Load the page without filling or submitting",
    )
    args = parser.parse_args()
    apply(args.job_url, dry_run=args.dry_run)
