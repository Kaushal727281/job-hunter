#!/usr/bin/env python3
"""
apply_oracle.py
---------------
Playwright-based automation for Oracle HCM Cloud career portals.

Supported companies (same Oracle HCM platform, different tenants):
  - Oracle Corp    : careers.oracle.com  (tenant: eeho.fa.us2.oraclecloud.com, site: CX_45001)
  - Texas Instruments: (tenant: edbz.fa.us2.oraclecloud.com, site: CX)

Flow (from HAR analysis):
  1. Navigate to job listing page
  2. Click "Apply" on the job
  3. Accept terms / cookie banners
  4. Fill personal info (name, email, phone, address)
  5. Upload resume PDF
  6. Answer screening/questionnaire questions
  7. Pause at Submit for manual click (and email OTP if required)
  8. Detect confirmation → mark success

Note: Oracle HCM may require email OTP verification for the final
      submission step. The script pauses to allow manual completion.

Usage:
    python3 apply_oracle.py --job-url "https://careers.oracle.com/jobs/#en/sites/jobsearch/job/..."
    python3 apply_oracle.py --dry-run --job-url "..."
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
    "first_name":    "Kaushal",
    "last_name":     "Jha",
    "email":         "kaushalkumarjha727219@gmail.com",
    "phone":         "+91 9818147393",
    "country":       "India",
    "state":         "Karnataka",
    "city":          "Bengaluru",
    "postal_code":   "560100",
    "address1":      "Mahaveer Ranches, Sree Sai Layout, Prapanna Agarpara",
    "linkedin":      "https://www.linkedin.com/in/kaushal-kumar-jha-93b77512a/",
    "current_ctc":   "26 LPA",
    "expected_ctc":  "32 LPA",
    "notice_period": "1 month",
    "resume_pdf":    os.path.expanduser(
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
    "conflict of interest", "criminal", "non-compete", "sponsorship",
)


def answer_screening_questions(page):
    """Answer Yes/No questions in Oracle HCM questionnaire."""
    answered = 0
    containers = page.locator(
        '[role="radiogroup"], .questionBlock, .question-row, '
        '[data-question], fieldset, .oj-form-layout-item'
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
                'label:has-text("Yes")', 'input[value="Y"]',
                'input[value="yes"]', 'input[value="Yes"]',
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
                'label:has-text("No")', 'input[value="N"]',
                'input[value="no"]', 'input[value="No"]',
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


# ── Multi-step form navigator ─────────────────────────────────────────────────

def click_next(page) -> bool:
    """Click Next / Continue button to advance form steps."""
    for sel in (
        'button:has-text("Next")', 'button:has-text("Continue")',
        'button:has-text("Save and Continue")', 'button:has-text("Proceed")',
        '[data-name="Next"]', '[aria-label="Next"]',
        '.nextButton', '#next-button',
    ):
        try:
            btn = page.locator(sel).first
            if btn.is_visible(timeout=2000):
                btn.click()
                settle(page, 3000)
                return True
        except Exception:
            pass
    return False


# ── Main apply function ───────────────────────────────────────────────────────

def apply(job_url: str, dry_run: bool = False):
    resume_path = PROFILE["resume_pdf"]
    if not os.path.isfile(resume_path):
        print(f"[ERROR] Resume PDF not found: {resume_path}")
        sys.exit(1)

    print(f"[INFO] Job URL  : {job_url}")
    print(f"[INFO] Resume   : {resume_path}")

    # Copy Chrome cookies for better trust score
    chrome_src = os.path.expanduser("~/Library/Application Support/Google/Chrome/Default")
    tmp = tempfile.mkdtemp(prefix="chrome_ora_")
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

        # ── Step 1: Navigate ──────────────────────────────────────────────────
        print(f"\n[STEP 1] Loading job page...")
        page.goto(job_url, wait_until="networkidle", timeout=30000)
        settle(page, 3000)
        print(f"  Title: {page.title()}")

        if dry_run:
            print("[DRY RUN] Page loaded. Exiting without applying.")
            ctx.close()
            return

        # Dismiss cookie banners
        for sel in (
            '#onetrust-accept-btn-handler', '[aria-label="Accept All"]',
            'button:has-text("Accept All")', 'button:has-text("Accept")',
            '.accept-cookies',
        ):
            click_if_visible(page, sel, 2000)
            time.sleep(0.3)

        # ── Step 2: Click Apply ───────────────────────────────────────────────
        print("[STEP 2] Clicking Apply...")
        for sel in (
            'a:has-text("Apply Now")', 'button:has-text("Apply Now")',
            'a:has-text("Apply")',     'button:has-text("Apply")',
            '[data-testid*="apply"]',  '.apply-button',
        ):
            if click_if_visible(page, sel, 4000):
                print(f"  Clicked: {sel}")
                settle(page, 3000)
                break

        # Handle new tab if Oracle opens apply in a new page
        if len(ctx.pages) > 1:
            page = ctx.pages[-1]
            settle(page, 3000)
            print(f"  Switched to new tab: {page.url}")

        # ── Step 3: Resume upload (Oracle HCM "Upload CV" step) ───────────────
        print("[STEP 3] Looking for resume upload...")
        try:
            fi = page.locator('input[type="file"]').first
            fi.wait_for(state="attached", timeout=10000)
            fi.set_input_files(resume_path)
            settle(page, 5000)
            print("  Resume uploaded.")
        except Exception as ex:
            print(f"  [WARN] Upload: {ex}")

        # ── Step 4: Fill personal info ────────────────────────────────────────
        print("[STEP 4] Filling personal info...")
        settle(page, 2000)

        field_map = [
            # Oracle HCM uses various selector patterns
            ('input[id*="firstName"], input[name*="firstName"], input[placeholder*="First Name"]', PROFILE["first_name"]),
            ('input[id*="lastName"],  input[name*="lastName"],  input[placeholder*="Last Name"]',  PROFILE["last_name"]),
            ('input[id*="email"],     input[name*="email"],     input[type="email"]',              PROFILE["email"]),
            ('input[id*="phone"],     input[name*="phone"],     input[type="tel"]',                PROFILE["phone"]),
            ('input[id*="city"],      input[name*="city"],      input[placeholder*="City"]',       PROFILE["city"]),
            ('input[id*="zip"],       input[name*="postal"],    input[placeholder*="Postal"]',     PROFILE["postal_code"]),
        ]

        for selectors, val in field_map:
            for s in selectors.split(","):
                s = s.strip()
                try:
                    el = page.locator(s).first
                    if el.is_visible(timeout=1500):
                        if not el.input_value():
                            el.fill(val)
                        break
                except Exception:
                    pass

        # LinkedIn
        for sel in ('input[placeholder*="LinkedIn"]', 'input[name*="linkedin"]'):
            try:
                el = page.locator(sel).first
                if el.is_visible(timeout=1000):
                    el.fill(PROFILE["linkedin"])
                    break
            except Exception:
                pass

        # ── Step 5: Walk multi-step form ──────────────────────────────────────
        print("[STEP 5] Walking multi-step form...")
        for step in range(1, 15):
            # Answer any questions on the current step
            answer_screening_questions(page)

            # Fill CTC/notice fields if present
            for q_frag, val in [
                ("current", PROFILE["current_ctc"]),
                ("expected", PROFILE["expected_ctc"]),
                ("notice", PROFILE["notice_period"]),
                ("salary", PROFILE["expected_ctc"]),
            ]:
                try:
                    label = page.locator(
                        f'label:has-text("{q_frag}"), '
                        f'label:has-text("{q_frag.title()}")'
                    ).first
                    field_id = label.get_attribute("for") or ""
                    if field_id:
                        el = page.locator(f"#{field_id}").first
                        if el.is_visible(timeout=500) and not el.input_value():
                            el.fill(val)
                except Exception:
                    pass

            # Check if Submit button is visible → stop advancing
            try:
                submit_btn = page.locator(
                    'button:has-text("Submit"), button[type="submit"], '
                    'button:has-text("Complete Application")'
                ).first
                if submit_btn.is_visible(timeout=2000):
                    print(f"  Submit button visible at step {step}")
                    break
            except Exception:
                pass

            # Try to advance to next step
            if not click_next(page):
                print(f"  No Next button at step {step}, stopping.")
                break

        # ── Step 6: Human-like activity ───────────────────────────────────────
        print("[STEP 6] Simulating human activity...")
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
                'button:has-text("Submit"), button[type="submit"]'
            ).first
            submit_btn.evaluate("el => el.scrollIntoView({block:'center'})")
            time.sleep(1)
        except Exception:
            pass

        # ── Step 7: Pause for manual Submit (+ email OTP if needed) ──────────
        print("\n" + "=" * 60)
        print("  ALL FIELDS FILLED. Please review and click Submit.")
        print("  NOTE: Oracle HCM may require EMAIL OTP verification.")
        print("  Check your email and complete the OTP step if prompted.")
        print("  Waiting up to 15 minutes...")
        print("=" * 60 + "\n")

        try:
            page.wait_for_url(
                "**/confirmation**|**/thank-you**|**/thankyou**"
                "|**/success**|**/submitted**|**/apply/success**",
                timeout=900_000,
            )
        except Exception:
            # Also check for "Application Submitted" text on page
            try:
                page.wait_for_selector(
                    ':has-text("Application Submitted"), '
                    ':has-text("Thank you"), :has-text("successfully submitted")',
                    timeout=900_000,
                )
            except Exception:
                pass

        final_url = page.url
        print(f"\n[RESULT] {final_url}")
        if any(k in final_url.lower() for k in ("confirm", "thank", "success", "submitted")):
            print("[SUCCESS] Application submitted!")
        else:
            # Check page text
            try:
                body = page.inner_text("body")[:500]
                if any(k in body.lower() for k in ("thank you", "successfully", "submitted")):
                    print("[SUCCESS] Application submitted! (detected from page text)")
                else:
                    page.screenshot(path="/tmp/oracle_apply_result.png")
                    print("[WARN] Not confirmed — screenshot at /tmp/oracle_apply_result.png")
            except Exception:
                page.screenshot(path="/tmp/oracle_apply_result.png")
                print("[WARN] Not confirmed — screenshot at /tmp/oracle_apply_result.png")

        time.sleep(5)
        ctx.close()


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Apply to an Oracle HCM Cloud job (Oracle, Texas Instruments, etc.)"
    )
    parser.add_argument(
        "--job-url",
        required=True,
        help='Full job URL, e.g. "https://careers.oracle.com/jobs/#en/sites/jobsearch/job/..."',
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Load the page without filling or submitting",
    )
    args = parser.parse_args()
    apply(args.job_url, dry_run=args.dry_run)
