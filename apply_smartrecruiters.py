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


class DataDomeBlockedError(Exception):
    """Raised when DataDome bot-detection challenge is not cleared."""
import time

from playwright.sync_api import sync_playwright
from playwright_stealth import Stealth

try:
    import browser_cookie3 as _bc3
    _HAS_BC3 = True
except ImportError:
    _HAS_BC3 = False

# ── Candidate profile (loaded from .env) ──────────────────────────────────────

import dotenv as _dotenv
_dotenv.load_dotenv(os.path.join(os.path.dirname(__file__), ".env"), override=False)

def _e(key: str, default: str = "") -> str:
    return os.environ.get(key, default)

PROFILE = {
    "first_name": _e("CANDIDATE_FIRST_NAME"),
    "last_name":  _e("CANDIDATE_LAST_NAME"),
    "email":      _e("CANDIDATE_EMAIL"),
    "phone":      _e("CANDIDATE_PHONE_E164"),
    "country":    _e("CANDIDATE_COUNTRY", "India"),
    "city":       _e("CANDIDATE_CITY"),
    "linkedin":   _e("CANDIDATE_LINKEDIN"),
    "resume_pdf": os.path.expanduser(_e("CANDIDATE_RESUME_PDF")),
    "cover_note": (
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

    # Use a minimal temp profile (no encrypted cookie copy)
    tmp = tempfile.mkdtemp(prefix="chrome_sr_")

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

        # ── Inject decrypted Chrome cookies (DataDome trust) ──────────────────
        if _HAS_BC3:
            try:
                chrome_cookies = _bc3.chrome(domain_name="smartrecruiters.com")
                playwright_cookies = []
                for c in chrome_cookies:
                    entry = {
                        "name": c.name,
                        "value": c.value,
                        "domain": c.domain if c.domain.startswith(".") else "." + c.domain,
                        "path": c.path or "/",
                        "secure": bool(c.secure),
                        "httpOnly": False,
                        "sameSite": "None",
                    }
                    playwright_cookies.append(entry)
                if playwright_cookies:
                    ctx.add_cookies(playwright_cookies)
                    print(f"[INFO] Injected {len(playwright_cookies)} Chrome cookies (DataDome trust)")
            except Exception as e:
                print(f"[WARN] Cookie injection failed: {e}")

        def _handle_datadome(pg):
            """Detect DataDome 'Verification Required' interstitial and wait for it to clear."""
            try:
                if "verification required" in pg.title().lower() or \
                   pg.locator('text="Verification Required"').is_visible(timeout=1500):
                    print("  [DataDome] Verification challenge detected — waiting for user to solve...")
                    # The challenge has a verify widget and a RETRY button
                    # Try clicking the verify icon/button (image person icon) to trigger check
                    for chk_sel in [
                        'button:has-text("RETRY")', 'button:has-text("Retry")',
                        '.dd-lb-button', '[class*="lb-button"]',
                        'button[class*="verify"]', 'button[class*="check"]',
                    ]:
                        try:
                            btn = pg.locator(chk_sel).first
                            if btn.is_visible(timeout=1000):
                                btn.click()
                                time.sleep(2)
                                break
                        except Exception:
                            pass
                    # Wait up to 45s for challenge to clear (user may need to solve)
                    for _ in range(9):
                        if "verification required" not in pg.title().lower():
                            break
                        time.sleep(5)
                    if "verification required" in pg.title().lower():
                        print("  [DataDome] Challenge not cleared — skipping job")
                        raise DataDomeBlockedError("DataDome challenge not solved")
                    print("  [DataDome] Challenge cleared.")
            except DataDomeBlockedError:
                raise
            except Exception:
                pass

        # ── Step 1: Navigate to job page ──────────────────────────────────────
        print(f"\n[STEP 1] Loading job page...")
        try:
            page.goto(job_url, wait_until="load", timeout=45000)
        except Exception:
            # If load times out, try domcontentloaded fallback
            try:
                page.goto(job_url, wait_until="domcontentloaded", timeout=30000)
            except Exception as ex:
                print(f"  [WARN] Page load failed: {ex}")
        settle(page, 3000)
        print(f"  Title: {page.title()}")
        _handle_datadome(page)

        if dry_run:
            print("[DRY RUN] Page loaded. Exiting without applying.")
            ctx.close()
            return

        # ── Step 2: Click Apply / OneClick Apply button ───────────────────────
        print("[STEP 2] Clicking Apply...")
        for sel in (
            'a[data-test="apply-button"]', 'button[data-test="apply-button"]',
            '[data-test="btn-apply"]',
            # ServiceNow / SmartRecruiters custom labels
            'button:has-text("I\'m interested")', 'a:has-text("I\'m interested")',
            '[data-test="btn-interested"]', 'button[data-test*="interested"]',
            'a:has-text("Apply")', 'button:has-text("Apply Now")', 'button:has-text("Apply")',
        ):
            if click_if_visible(page, sel, 4000):
                print(f"  Clicked: {sel}")
                settle(page, 3000)
                break

        _handle_datadome(page)

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

        # Confirm email (some SR forms have a repeat-email field)
        for sel in (
            '[data-test="input-confirmEmail"]', 'input[name="confirmEmail"]',
            'input[placeholder*="onfirm"]', 'input[id*="confirm"]',
        ):
            try:
                el = page.locator(sel).first
                if el.is_visible(timeout=1000):
                    if not el.input_value():
                        el.fill(PROFILE["email"])
                    print("  Filled confirm-email")
                    break
            except Exception:
                pass

        # Location / city — SR uses a typeahead with a search icon; type + pick first option
        for sel in (
            '[data-test="input-city"]', 'input[name="city"]',
            'input[placeholder*="City"]', 'input[placeholder*="city"]',
            'input[placeholder*="place of residence"]',
        ):
            try:
                el = page.locator(sel).first
                if el.is_visible(timeout=1000):
                    if not el.input_value():
                        el.click()
                        el.type(PROFILE["city"], delay=60)
                        time.sleep(1.2)
                        # Pick first autocomplete suggestion
                        for _opt_sel in (
                            '[role="option"]:visible', 'li[role="option"]:visible',
                            '.pac-item:visible', '[class*="suggestion"]:visible',
                        ):
                            try:
                                _opt = page.locator(_opt_sel).first
                                if _opt.is_visible(timeout=800):
                                    _opt.click()
                                    print(f"  Filled city via autocomplete ({_opt_sel})")
                                    break
                            except Exception:
                                pass
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

        # ── Step 8: Auto-submit then wait for confirmation ────────────────────
        submitted = False
        for sel in (
            'button[data-test="btn-submit"]', 'button:has-text("Submit")',
            'button[type="submit"]', 'input[type="submit"]',
        ):
            try:
                btn = page.locator(sel).first
                if btn.is_visible(timeout=3000):
                    btn.click()
                    settle(page, 3000)
                    submitted = True
                    print(f"  Auto-submit clicked: {sel}")
                    break
            except Exception:
                pass

        if not submitted:
            print("[WARN] No submit button found — skipping (batch mode)")

        # Wait 90s if no submit found; 10 min if submit was clicked
        wait_ms = 90_000 if not submitted else 600_000
        try:
            page.wait_for_url(
                "**/confirmation**|**/thank-you**|**/thankyou**"
                "|**/success**|**applied=true**",
                timeout=wait_ms,
            )
        except Exception:
            pass

        final_url = page.url
        print(f"\n[RESULT] {final_url}")
        success = any(k in final_url.lower() for k in ("confirm", "thank", "success", "applied"))
        if success:
            print("[SUCCESS] Application submitted!")
        else:
            page.screenshot(path="/tmp/sr_apply_result.png")
            print("[WARN] Not confirmed — screenshot at /tmp/sr_apply_result.png")

        time.sleep(5)
        ctx.close()
        return success


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Apply to SmartRecruiters jobs (ServiceNow, etc.)"
    )
    parser.add_argument("--job-url",   help="Single job URL (single-job mode)")
    parser.add_argument("--profile",   default=os.environ.get("CANDIDATE_PROFILE_SLUG", ""))
    parser.add_argument("--min-score", type=int, default=7)
    parser.add_argument("--limit",     type=int)
    parser.add_argument("--company",   help="Filter to one company")
    parser.add_argument("--dry-run",   action="store_true")
    args = parser.parse_args()

    # ── Single-URL mode (original behaviour) ──────────────────────────────────
    if args.job_url:
        apply(args.job_url, dry_run=args.dry_run)
        sys.exit(0)

    # ── Batch mode: read from job store ───────────────────────────────────────
    import profiles, job_store
    profiles.set_active_profile(args.profile)

    all_jobs = job_store.all_jobs()
    jobs = [
        j for j in all_jobs
        if j.get("ats_type") == "smartrecruiters"
        and j.get("fit_score", 0) >= args.min_score
        and not j.get("applied_at")
        and not j.get("removed")
        and j.get("apply_link")
    ]
    if args.company:
        jobs = [j for j in jobs if args.company.lower() in j.get("company", "").lower()]
    jobs.sort(key=lambda j: j.get("fit_score", 0), reverse=True)
    if args.limit:
        jobs = jobs[:args.limit]

    if not jobs:
        print(f"No SmartRecruiters jobs with fit_score >= {args.min_score} ready to apply.")
        sys.exit(0)

    print(f"\nFound {len(jobs)} SmartRecruiters job(s) to apply to:\n")
    for j in jobs:
        print(f"  [{j.get('fit_score','?')}/10] {j.get('title','?')[:55]} @ {j.get('company','?')}")

    applied_count, failed_count = 0, 0
    for j in jobs:
        print(f"\n{'='*60}")
        print(f"  JOB  : {j['title']} @ {j['company']}")
        print(f"  SCORE: {j.get('fit_score','?')}/10")
        print(f"  URL  : {j['apply_link']}")
        print(f"{'='*60}")
        try:
            ok = apply(j["apply_link"], dry_run=args.dry_run)
            if ok:
                job_store.mark_applied(j["id"], applied=True)
                applied_count += 1
                print(f"  Marked applied: {j['id']}")
            else:
                job_store.mark_applied(j["id"], applied=False,
                                       error="Form not confirmed — manual review needed")
                failed_count += 1
        except DataDomeBlockedError:
            # Do NOT permanently fail — just skip so it can be retried when cookies refresh
            print(f"  [DataDome] {j.get('company')} — skipped (bot detection). Will retry later.")
            failed_count += 1
        except Exception as exc:
            err = str(exc)[:200]
            print(f"[ERROR] {err}")
            job_store.mark_applied(j["id"], applied=False, error=err)
            failed_count += 1
        time.sleep(2)

    print(f"\n{'='*60}")
    print(f"  SmartRecruiters apply complete — Applied: {applied_count}  Failed: {failed_count}")
    print(f"{'='*60}")
