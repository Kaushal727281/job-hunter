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
import re
import shutil
import sys
import tempfile
import time

from playwright.sync_api import sync_playwright
from playwright_stealth import Stealth

# ── Candidate profile (loaded from .env) ──────────────────────────────────────

import dotenv as _dotenv
_dotenv.load_dotenv(os.path.join(os.path.dirname(__file__), ".env"), override=False)

def _e(key: str, default: str = "") -> str:
    return os.environ.get(key, default)

PROFILE = {
    "first_name":  _e("CANDIDATE_FIRST_NAME"),
    "middle_name": _e("CANDIDATE_MIDDLE_NAME"),
    "last_name":   _e("CANDIDATE_LAST_NAME"),
    "email":       _e("CANDIDATE_EMAIL"),
    "password":    _e("WORKDAY_DEFAULT_PASSWORD"),  # Workday Create Account password
    "phone":       _e("CANDIDATE_PHONE"),
    "linkedin":    _e("CANDIDATE_LINKEDIN"),
    "address1":    _e("CANDIDATE_ADDRESS"),
    "city":        _e("CANDIDATE_CITY"),
    "postal_code": _e("CANDIDATE_POSTAL_CODE"),
    # Default resume PDF — overridden per-job if a tailored PDF exists
    "resume_pdf":  os.path.expanduser(_e("CANDIDATE_RESUME_PDF")),
}

# Tenants where we have a registered account (email + password)
# Map: subdomain → password  (email is always PROFILE["email"])
_WD_PWD = _e("WORKDAY_DEFAULT_PASSWORD")
_RH_PWD = _e("WORKDAY_RH_PASSWORD")   # Red Hat Workday account (HAR-verified)
TENANT_CREDENTIALS = {
    "ms":          _e("WORKDAY_MS_PASSWORD"),  # Morgan Stanley (existing account, 11-char password)
    "redhat":      _RH_PWD,                    # Red Hat (redhat.wd5)
    "visa":        _WD_PWD,                    # Visa (existing account)
    "cisco":       _WD_PWD,                    # Cisco (created in earlier runs)
    "adobe":       _WD_PWD,                    # Adobe (created in earlier runs)
    "workday":     _WD_PWD,                    # Workday Inc (created in earlier runs)
    "autodesk":    _WD_PWD,                    # Autodesk (created in earlier runs)
    "mastercard":  _WD_PWD,                    # Mastercard (created in earlier runs)
    "hp":          _WD_PWD,                    # HP Inc (created in earlier runs)
    # ── Add after manually creating account on each portal ──────────────────────
    "broadcom":    _WD_PWD,                    # TODO: create account at broadcom.wd1.myworkdayjobs.com
    "cadence":     _WD_PWD,                    # TODO: create account at cadence.wd1.myworkdayjobs.com
    "cloudera":    _WD_PWD,                    # TODO: create account at cloudera.wd5.myworkdayjobs.com
    "warnerbros":  _WD_PWD,                    # TODO: create account at warnerbros.wd5.myworkdayjobs.com
    "salesforce":  _WD_PWD,                    # TODO: create account at salesforce.wd12.myworkdayjobs.com
    "intel":       _WD_PWD,                    # TODO: create account at intel.wd1.myworkdayjobs.com
    "crowdstrike": _WD_PWD,                    # TODO: create account at crowdstrike.wd5.myworkdayjobs.com
    "cohesity":    _WD_PWD,                    # TODO: create account at cohesity.wd5.myworkdayjobs.com
    "sprinklr":    _WD_PWD,                    # TODO: create account at sprinklr.wd1.myworkdayjobs.com
    "ptc":         _WD_PWD,                    # TODO: create account at ptc.wd1.myworkdayjobs.com
}

# Tenants that cannot be automated (OAuth-only or require email verification)
_OAUTH_ONLY_TENANTS = {
    "nvidia",     # uses community.workday.com SSO (Google/LinkedIn only)
    "autodesk",   # account requires email verification — manually verify at careers.autodesk.com
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
    for aid in ("pageFooterNextButton", "bottom-navigation-next-btn", "next-btn",
                "saveAndContinueButton", "bottom-navigation-next-button",
                "continueButton", "nextButton"):
        try:
            b = page.locator(f'[data-automation-id="{aid}"]').first
            b.wait_for(state="visible", timeout=2000)
            b.scroll_into_view_if_needed()
            b.evaluate("el => el.click()")   # JS click bypasses overlay issues
            print(f"  [next] Clicked: {aid}")
            settle(page, 4000)
            return True
        except Exception:
            pass
    # Text-based fallbacks
    for sel in ('button:has-text("Continue")', 'button:has-text("Next")',
                'button:has-text("Save and Continue")'):
        try:
            b = page.locator(sel).first
            b.wait_for(state="visible", timeout=2000)
            b.scroll_into_view_if_needed()
            b.evaluate("el => el.click()")
            print(f"  [next] Clicked text: {sel}")
            settle(page, 4000)
            return True
        except Exception:
            pass
    print("  [next] No Next/Continue button found")
    return False


def dismiss_overlays(page):
    """Dismiss cookie banners, modals, etc."""
    for sel in [
        '[data-automation-id="legalNoticeAcceptButton"]',
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

def _try_login(page, tenant: str) -> bool:
    """
    Attempt to sign in at the Workday tenant's login page.
    Only runs if we have stored credentials for the tenant.
    Returns True if sign-in succeeded (URL changed away from login).
    """
    password = TENANT_CREDENTIALS.get(tenant)
    if not password:
        return False  # No credentials — continue as guest

    # Check if we're already at a login / SSO page
    url = page.url
    at_community = "community.workday.com" in url
    at_login     = "/login" in url or "signin" in url.lower() or "oauth" in url.lower()

    if at_community:
        # Workday redirected to community.workday.com SSO — can't use email/password here
        print(f"  [login] At community.workday.com SSO — skipping pre-login (no email form)")
        return
    elif not at_login:
        # Navigate to the proper tenant login URL (use tenant, not current URL base)
        try:
            login_url = f"https://{tenant}.wd5.myworkdayjobs.com/en-US/External/login"
            print(f"  [login] Navigating to {login_url}")
            page.goto(login_url, wait_until="networkidle", timeout=30000)
            settle(page, 2000)
            dismiss_overlays(page)
            # If redirected to community SSO after navigation, note it
            if "community.workday.com" in page.url:
                print(f"  [login] Redirected to community SSO: {page.url}")
        except Exception as _nav_err:
            print(f"  [login] Nav warn: {_nav_err}")

    # Try both Workday tenant selectors and generic HTML selectors (community.workday.com)
    email_sel = '[data-automation-id="email"], input[name="email"], input[type="email"]'
    pwd_sel   = '[data-automation-id="password"], input[name="password"], input[type="password"]'
    try:
        # Step 1: Fill email
        page.locator(email_sel).first.wait_for(state="visible", timeout=10000)
        page.locator(email_sel).first.fill(PROFILE["email"])
        settle(page, 500)

        # Step 2: Click "Continue" if shown (two-step login: email → Continue → password)
        for _cont_sel in (
            '[data-automation-id="continue"]',
            '[data-automation-id="continueButton"]',
            'button:has-text("Continue")',
            'button:has-text("Next")',
        ):
            try:
                btn = page.locator(_cont_sel).first
                if btn.is_visible(timeout=2000):
                    btn.click()
                    settle(page, 2000)
                    print(f"  [login] Clicked Continue: {_cont_sel}")
                    break
            except Exception:
                pass

        # Step 3: Fill password (may now be visible after Continue)
        try:
            page.locator(pwd_sel).first.wait_for(state="visible", timeout=8000)
            page.locator(pwd_sel).first.fill(password)
        except Exception:
            pass

        # Step 4: Click sign-in / submit
        # JS-click the sign-in button (bypasses Workday's click_filter overlay)
        _signed_in = False
        try:
            page.locator('[data-automation-id="click_filter"]').first.evaluate(
                "el => el.click()"
            )
            settle(page, 1000)
        except Exception:
            pass
        for _sign_sel in (
            '[data-automation-id="signIn"]',
            '[data-automation-id="submitButton"]',
            'button:has-text("Sign In")',
            'button:has-text("Log In")',
        ):
            try:
                btn = page.locator(_sign_sel).first
                if btn.is_visible(timeout=2000):
                    btn.click()
                    _signed_in = True
                    settle(page, 1000)
                    break
            except Exception:
                pass
        if not _signed_in:
            try:
                page.locator('[data-automation-id="password"]').first.press("Enter")
            except Exception:
                try:
                    page.locator('input[name="password"]').first.press("Enter")
                except Exception:
                    pass

        try:
            # Wait for URL to change away from login page (userHome or apply flow)
            page.wait_for_url(re.compile(r"userHome|/apply/"), timeout=15000)
        except Exception:
            pass
        settle(page, 3000)
        _post_url = page.url
        _login_success = "login" not in _post_url.lower() and "signin" not in _post_url.lower()
        print(f"  [login] {'Signed in' if _login_success else 'Login FAILED'}. URL: {_post_url}")
        return _login_success
    except Exception as exc:
        print(f"  [login] WARN: {exc}")
    return False


def _try_create_account(page, tenant: str = "") -> bool:
    """
    Detect and fill a Workday 'Create Account' / 'Sign In' form.
    Handles two patterns:
      A) Direct Create Account form (verifyPassword visible)
      B) OAuth page with 'Sign in with email' → leads to Create Account
    Tries Sign In ONLY for known tenants in TENANT_CREDENTIALS.
    For unknown tenants, goes straight to Create Account form.
    Returns True if auth was attempted.
    """
    email = PROFILE["email"]
    pwd   = PROFILE["password"]

    # Pattern C: Try Sign In first (account may already exist from a previous run)
    # ONLY for tenants we have stored credentials for.
    # SKIP if we're already on autofillWithResume — any Sign In link there would navigate
    # away from the apply flow. Sign In for autofillWithResume is handled via direct login URL.
    _before_url = page.url
    _on_autofill = "autofillWithResume" in _before_url
    try:
        # Check if there's a Sign In TAB within the form (NOT the header nav link).
        # Use only automation-id or role=tab selectors to avoid matching page header "Sign In".
        # Never use plain a:has-text("Sign In") — it can match the header link or footer links.
        signin_tab = page.locator(
            '[data-automation-id="signInLink"], '
            '[data-automation-id="signInTab"]'
        ).first
        _tab_found = (not _on_autofill) and signin_tab.is_visible(timeout=1500)
        if _tab_found and tenant in TENANT_CREDENTIALS:
            # There is an in-form Sign In tab — click it and try to sign in
            signin_tab.click()
            settle(page, 2000)
            print("  [create-account] Clicked Sign In tab/link")
            stored_pwd = TENANT_CREDENTIALS.get(tenant, PROFILE["password"])
            for _esel in ('[data-automation-id="email"]', 'input[type="email"]'):
                try:
                    el = page.locator(_esel).first
                    if el.is_visible(timeout=1500):
                        el.fill(email); time.sleep(0.2); break
                except Exception: pass
            for _psel in ('[data-automation-id="password"]', 'input[type="password"]'):
                try:
                    el = page.locator(_psel).first
                    if el.is_visible(timeout=1500):
                        el.fill(stored_pwd); time.sleep(0.2); break
                except Exception: pass
            # Click Sign In submit
            _signin_submitted = False
            for sel in ('[data-automation-id="signIn"]',
                        '[data-automation-id="signInSubmitButton"]',
                        '[data-automation-id="submitButton"]',
                        'button[type="submit"]:has-text("Sign In")',
                        'button:has-text("Sign In")'):
                try:
                    btn = page.locator(sel).first
                    btn.wait_for(state="visible", timeout=2000)
                    btn.evaluate("el => el.click()")
                    settle(page, 5000)
                    _signin_submitted = True
                    break
                except Exception: pass
            if not _signin_submitted:
                try:
                    page.locator('input[type="password"]').last.press("Enter")
                    settle(page, 5000)
                    _signin_submitted = True
                except Exception: pass
            # Check if Sign In succeeded
            _auth_visible = False
            for _achk in ('[data-automation-id="verifyPassword"]', '[data-automation-id="signInContent"]'):
                try:
                    if page.locator(_achk).first.is_visible(timeout=1000):
                        _auth_visible = True
                        break
                except Exception: pass
            if _signin_submitted and not _auth_visible:
                print(f"  [create-account] Sign In succeeded. URL: {page.url}")
                return True
            else:
                print(f"  [create-account] Sign In failed (still on auth page) — trying Create Account")
    except Exception:
        pass

    # Pattern B: OAuth/Google sign-in page — click "Sign in with email" to get to password form
    try:
        email_btn = page.locator(
            'button:has-text("Sign in with email"), '
            'a:has-text("Sign in with email"), '
            '[data-automation-id="signInWithEmailButton"]'
        ).first
        if email_btn.is_visible(timeout=2000):
            email_btn.click()
            settle(page, 3000)
            print("  [create-account] Clicked 'Sign in with email'")
            # Fill email if prompted
            for sel in ('[data-automation-id="email"]', 'input[type="email"]', 'input[name="email"]'):
                try:
                    el = page.locator(sel).first
                    if el.is_visible(timeout=2000):
                        el.fill(email)
                        el.press("Tab")
                        settle(page, 1000)
                        break
                except Exception:
                    pass
            # Click Continue/Next if present
            for sel in (
                'button:has-text("Continue")', 'button:has-text("Next")',
                '[data-automation-id="continueButton"]',
            ):
                try:
                    btn = page.locator(sel).first
                    if btn.is_visible(timeout=2000):
                        btn.click()
                        settle(page, 3000)
                        break
                except Exception:
                    pass
    except Exception:
        pass

    # Pattern A: Direct Create Account form
    # If Sign In tab was clicked in Pattern C, we may now be on the Sign In form.
    # Only try switching to Create Account if verifyPassword is NOT yet visible.
    _already_on_create = False
    try:
        _already_on_create = page.locator('[data-automation-id="verifyPassword"]').first.is_visible(timeout=800)
    except Exception:
        pass

    if not _already_on_create:
        # Try clicking "Create Account" tab/link to switch to Create Account form.
        # Use only automation-id selectors + role=tab to avoid matching the submit button.
        for _ca_tab_sel in (
            '[data-automation-id="createAccount"]',
            '[data-automation-id="createAccountTab"]',
            '[role="tab"]:has-text("Create Account")',
            'a:has-text("Create Account")',
        ):
            try:
                _ca_btn = page.locator(_ca_tab_sel).first
                if _ca_btn.is_visible(timeout=1000):
                    _ca_btn.click()
                    settle(page, 1500)
                    print("  [create-account] Clicked 'Create Account' tab to switch back")
                    break
            except Exception:
                pass

    _pwd_visible = False
    for _pchk in (
        '[data-automation-id="verifyPassword"]',
        '[data-automation-id="password"]',
        'input[type="password"]',
        'input[name="verifyPassword"]',
        'input[name*="password" i]',
    ):
        try:
            if page.locator(_pchk).first.is_visible(timeout=2000):
                _pwd_visible = True
                break
        except Exception:
            pass
    if not _pwd_visible:
        return False

    print("  [create-account] Detected Create Account form — filling...")
    try:
        pwd = PROFILE["password"]
        # Fill email field first (required for Create Account)
        for _esel in ('[data-automation-id="email"]', 'input[type="email"]', 'input[name="email"]'):
            try:
                _ef = page.locator(_esel).first
                if _ef.is_visible(timeout=1000):
                    _ef.fill(email)
                    time.sleep(0.2)
                    break
            except Exception:
                pass
        # Fill all visible password fields (password + confirm password)
        _pwd_fields = page.locator('input[type="password"]').all()
        for _pf in _pwd_fields:
            try:
                if _pf.is_visible(timeout=500):
                    _pf.fill(pwd)
                    time.sleep(0.2)
            except Exception:
                pass
        # Also try automation-id selectors explicitly
        for sel in ('[data-automation-id="password"]', 'input[name="password"]'):
            try:
                el = page.locator(sel).first
                if el.is_visible(timeout=1000):
                    el.fill(pwd)
            except Exception:
                pass
        for sel in ('[data-automation-id="verifyPassword"]', 'input[name="verifyPassword"]',
                    'input[name*="confirm" i]', 'input[name*="verify" i]'):
            try:
                el = page.locator(sel).first
                if el.is_visible(timeout=1000):
                    el.fill(pwd)
            except Exception:
                pass
        # Tick consent / acknowledgment checkboxes
        # First scroll to bottom of form so checkboxes are in viewport
        try:
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            time.sleep(0.5)
        except Exception:
            pass
        # Try Playwright's check() first (most reliable for styled checkboxes)
        for _cb_sel in (
            '[data-automation-id="createAccountCheckbox"]',
            '[data-automation-id="consent"]',
            'label:has-text("Candidate acknowledgment") input',
            'label:has-text("acknowledgment") input',
        ):
            try:
                cb = page.locator(_cb_sel).first
                if cb.is_visible(timeout=1000) and not cb.is_checked():
                    cb.check()
                    time.sleep(0.3)
                    print(f"  [create-account] Checked consent via {_cb_sel!r}")
                    break
            except Exception:
                pass
        # JS fallback: click all unchecked checkboxes
        try:
            _checked = page.evaluate('''() => {
                var cbs = Array.from(document.querySelectorAll('input[type="checkbox"]'));
                var ticked = 0;
                cbs.forEach(function(cb) {
                    if (!cb.checked) {
                        cb.click();
                        cb.dispatchEvent(new Event("change", {bubbles: true}));
                        ticked++;
                    }
                });
                return ticked;
            }''')
            if _checked:
                time.sleep(0.5)
                print(f"  [create-account] JS-ticked {_checked} unchecked checkbox(es)")
        except Exception:
            pass
        # Debug screenshot before submission to diagnose state
        try:
            page.screenshot(path="/tmp/wd_before_create_account.png")
            # Log checkbox state for debugging
            _cb_state = page.evaluate('''() => {
                var cbs = Array.from(document.querySelectorAll('input[type="checkbox"]'));
                return cbs.map(function(cb) {
                    return {checked: cb.checked, id: cb.id, name: cb.name, visible: cb.getBoundingClientRect().width > 0};
                });
            }''')
            print(f"  [create-account] Checkbox states before submit: {_cb_state}")
        except Exception:
            pass
        # Click Create Account button via JS evaluate to bypass any overlay issues
        _submitted_sel = None
        for sel in (
            '[data-automation-id="createAccountSubmitButton"]',
            'button:has-text("Create Account")',
            'button:has-text("Continue")',
            'input[type="submit"]',
        ):
            try:
                btn = page.locator(sel).first
                btn.wait_for(state="visible", timeout=3000)
                btn.scroll_into_view_if_needed()
                btn.evaluate("el => el.click()")  # JS click bypasses overlay issues
                settle(page, 5000)
                print(f"  [create-account] Submitted via {sel!r}. URL: {page.url}")
                _submitted_sel = sel
                break
            except Exception:
                pass
        if not _submitted_sel:
            # Last-resort: press Enter on password field
            try:
                page.locator('input[type="password"]').last.press("Enter")
                settle(page, 5000)
                print(f"  [create-account] Submitted via Enter. URL: {page.url}")
                _submitted_sel = "enter"
            except Exception:
                pass
        if _submitted_sel:
            # Verify auth form is gone (submission succeeded) or still showing (failed)
            _still_auth = False
            for _ck in ('[data-automation-id="verifyPassword"]',
                        '[data-automation-id="createAccountSubmitButton"]'):
                try:
                    if page.locator(_ck).first.is_visible(timeout=2000):
                        _still_auth = True
                        break
                except Exception:
                    pass
            if _still_auth:
                # Scroll to top and capture page text to find error
                try:
                    page.evaluate("window.scrollTo(0, 0)")
                    time.sleep(0.5)
                    page.screenshot(path="/tmp/wd_after_create_account.png")
                    _page_text = page.locator("body").inner_text(timeout=2000)
                    # Look for known error patterns
                    _lower_text = _page_text.lower()
                    if "already" in _lower_text or "exist" in _lower_text:
                        print("  [create-account] Email already registered — returning False for direct-login fallback")
                        # Let Step 5b handle via direct login URL navigation
                    else:
                        print(f"  [create-account] Page text snippet: {_page_text[:300]!r}")
                except Exception as _ss_err:
                    print(f"  [create-account] Screenshot/text error: {_ss_err}")
                print("  [create-account] Auth form still visible after submission — email may exist or checkbox missed")
                return False
            return True
    except Exception as exc:
        print(f"  [create-account] WARN: {exc}")
    return False


# ── Apply to one job ──────────────────────────────────────────────────────────

def apply_to_job(job: dict, ctx, dry_run: bool = False, _out: dict = None):
    """
    Apply to one Workday job using an existing browser context.
    Returns True if we reached the review / thank-you page.
    _out: optional dict; on failure, _out["error"] is set to the reason string.
    """
    def _fail(reason: str):
        """Mark failure reason in _out and return False."""
        if _out is not None:
            _out["error"] = reason
        return False
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
        return _fail("resume_pdf_not_found")

    if tenant in _OAUTH_ONLY_TENANTS:
        print(f"  [SKIP] {tenant} requires Google/LinkedIn OAuth — cannot automate")
        return _fail("oauth_only_tenant")

    if dry_run:
        return True

    page = ctx.new_page()
    Stealth().apply_stealth_sync(page)

    try:
        # ── Step 1: Login (if credentials available) ─────────────────────────
        if tenant in TENANT_CREDENTIALS:
            login_base = re.match(r"(https?://[^/]+)", job_url).group(1)
            # Extract site path from job URL (e.g. "Ext" from .../en-US/Ext/job/...)
            _site_m = re.search(r"/en-US/([^/]+)/", job_url)
            _site_path = _site_m.group(1) if _site_m else "External"
            login_url  = login_base + f"/en-US/{_site_path}/login"
            print(f"[STEP 1] Logging in at {login_url}")
            page.goto(login_url, wait_until="networkidle", timeout=30000)
            settle(page, 2000)
            dismiss_overlays(page)
            # Debug: take screenshot before login attempt
            try:
                page.screenshot(path="/tmp/wd_login_before.png")
                print(f"  [debug] Pre-login screenshot saved, URL: {page.url}")
            except Exception:
                pass
            _try_login(page, tenant)
            # Debug: take screenshot after login attempt
            try:
                page.screenshot(path="/tmp/wd_login_after.png")
                print(f"  [debug] Post-login screenshot saved, URL: {page.url}")
            except Exception:
                pass
        else:
            print("[STEP 1] No stored credentials — applying as guest")

        # ── Step 2: Navigate to job page ──────────────────────────────────────
        print(f"[STEP 2] Loading job page: {job_url}")
        page.goto(job_url, wait_until="domcontentloaded", timeout=30000)
        settle(page, 3000)
        dismiss_overlays(page)
        # Workday is a SPA — title rendered by JS; wait up to 10s for it to appear
        _title = ""
        for _tw in range(10):
            _title = page.title()
            if _title:
                break
            time.sleep(1)
        _body_text = ""
        try:
            _body_text = page.locator("body").inner_text(timeout=3000).lower()
        except Exception:
            pass
        print(f"  Title: {_title}")
        # Detect dead / expired job pages (only if page has content indicating so)
        _dead_signals = (
            "page you are looking for doesn't exist",
            "job is no longer available",
            "this job posting is no longer active",
            "position has been filled",
            "no longer accepting applications",
            "page not found",
        )
        if any(s in _body_text for s in _dead_signals):
            print(f"  [SKIP] Job page is dead/404 — marking removed and skipping")
            page.close()
            return "dead"

        # Detect "already applied" message → mark as applied and skip
        _already_applied_signals = (
            "you've already applied", "you have already applied",
            "already applied for this job", "already submitted an application",
        )
        if any(s in _body_text for s in _already_applied_signals):
            print(f"  [ALREADY APPLIED] Portal shows 'already applied' — marking in store")
            page.close()
            return "already_applied"

        # ── Step 3: Click Apply ───────────────────────────────────────────────
        print("[STEP 3] Clicking Apply…")
        clicked_apply = False
        for aid in ("applyWithAccountButton", "applyButton", "adventureButton",
                    "apply", "applyNowBtn", "submitInterestButton"):
            try:
                b = page.locator(f'[data-automation-id="{aid}"]').first
                b.wait_for(state="visible", timeout=5000)
                b.click()
                print(f"  Clicked: {aid}")
                settle(page, 1500)   # short settle - popup appears within 1-2s
                clicked_apply = True
                break
            except Exception:
                pass
        if not clicked_apply:
            # Text-based fallback
            try:
                page.locator('a:has-text("Apply"), button:has-text("Apply Now")').first.click()
                settle(page, 1500)
                clicked_apply = True
            except Exception:
                pass

        if not clicked_apply:
            print("  [WARN] Could not find Apply button — may already be on apply flow")

        # ── New-tab detection: some portals open apply form in a new tab ─────
        # Only switch if a new tab is already at an /apply URL (no waiting)
        time.sleep(0.5)
        for _p in ctx.pages:
            if _p != page and "/apply" in _p.url:
                settle(_p, 2000)
                print(f"  [new-tab] Switched to apply tab: {_p.url}")
                page = _p
                break

        # ── Step 3b: Navigate directly to apply/autofillWithResume ─────────
        # Bypass the unreliable popup (auto-closes ~7s) by constructing the URL directly.
        # This is equivalent to clicking autofillWithResume in the popup.
        print(f"  [3b] URL after apply click: {page.url}")
        _step5_clicked = False
        _autofill_url = job_url.rstrip('/') + '/apply/autofillWithResume'
        try:
            page.goto(_autofill_url, wait_until="domcontentloaded", timeout=30000)
            settle(page, 3000)
            # Wait for autofill page content to fully render (SPA may take >3s)
            _autofill_ready = False
            for _aw_sel in (
                '[data-automation-id="file-upload-input-ref"]',
                'input[type="file"]',
                '[data-automation-id="pageFooterNextButton"]',
                'button:has-text("Continue")',
                '[data-automation-id="formField-verifyPassword"]',  # Create Account
                '[data-automation-id="formField-legalName--firstName"]',  # My Info
            ):
                try:
                    page.locator(_aw_sel).first.wait_for(state="attached", timeout=8000)
                    print(f"  [3b] Autofill page ready via: {_aw_sel}")
                    _autofill_ready = True
                    break
                except Exception:
                    pass
            if not _autofill_ready:
                settle(page, 3000)  # extra settle if nothing found
            print(f"  [3b] Navigated to apply URL: {page.url}")
            try:
                page.screenshot(path=f"/tmp/wd_after3b_{tenant}.png")
                print(f"  [3b] Screenshot saved: /tmp/wd_after3b_{tenant}.png")
            except Exception:
                pass
            # Check "already applied" right after navigating to apply URL
            try:
                _body_at_3b = page.locator("body").inner_text(timeout=2000).lower()
                if any(s in _body_at_3b for s in (
                    "you've already applied", "you have already applied",
                    "already applied for this job", "already submitted an application",
                )):
                    print("  [3b] Already applied — marking in store and skipping")
                    page.close()
                    return "already_applied"
            except Exception:
                pass
            _step5_clicked = True
        except Exception as _e3b:
            print(f"  [3b] Direct navigation failed: {_e3b}")
            # Fall back: try popup buttons
            for _popup_aid in ("autofillWithResume", "useMyLastApplication", "applyManually"):
                try:
                    b = page.locator(f'[data-automation-id="{_popup_aid}"]').first
                    b.wait_for(state="visible", timeout=3000)
                    b.click()
                    print(f"  [popup] Clicked: {_popup_aid}")
                    settle(page, 3000)
                    _step5_clicked = True
                    break
                except Exception:
                    pass

        # ── Step 4: Handle "Sign In" / "Create Account" prompt ───────────────
        # Skip auth detection if popup was already clicked in Step 3b — the
        # post-popup auth wall (if any) is handled in Step 5b instead.
        cur_url = page.url
        needs_auth = ("signIn" in cur_url or "login" in cur_url.lower()
                      or "register" in cur_url.lower())
        # Wait up to 12s for Create Account / Sign In form to render (Workday SPA is slow)
        if not needs_auth and not _step5_clicked:
            _AUTH_CHKS = (
                '[data-automation-id="verifyPassword"]',
                '[data-automation-id="createAccountSubmitButton"]',
                'button:has-text("Create Account")',
                'h2:has-text("Create Account")',
                'input[type="password"]',
                # OAuth/Google sign-in page (NVIDIA, Adobe, etc.)
                'button:has-text("Sign in with email")',
                'button:has-text("Sign in with Google")',
                '[data-automation-id="signInWithEmailButton"]',
                'h2:has-text("Sign In")',
                'h1:has-text("Sign In")',
            )
            _FORM_READY_SEL = (
                '[data-automation-id="file-upload-input-ref"], '
                '[data-automation-id="legalNameSection_firstName"], '
                '[data-automation-id="autofillWithResume"], '
                '[data-automation-id="applyManually"], '
                '[data-automation-id="useMyLastApplication"]'
            )
            for _wait_round in range(3):  # up to 3 rounds
                # Check form/popup FIRST — popup can auto-close after ~30s
                _form_visible = False
                try:
                    page.locator(_FORM_READY_SEL).first.wait_for(state="visible", timeout=3000)
                    _form_visible = True
                except Exception:
                    pass
                if _form_visible:
                    break  # Form/popup visible — no auth wall, proceed
                # Then check for auth selectors
                for _auth_chk in _AUTH_CHKS:
                    try:
                        page.locator(_auth_chk).first.wait_for(state="visible", timeout=2000)
                        needs_auth = True
                        print(f"  [auth-detect] Create Account form via: {_auth_chk!r}")
                        break
                    except Exception:
                        pass
                if needs_auth:
                    break
                time.sleep(1)
        if needs_auth:
            print("[STEP 4] Auth wall detected")
            if _try_create_account(page, tenant):
                print("  [auth] Account created — proceeding")
            else:
                _try_login(page, tenant)
            # Re-navigate to job after auth
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

        # ── Step 5: Autofill with Resume (fallback if popup not caught above) ──
        print("[STEP 5] Autofill with Resume…")
        if not _step5_clicked:
            for aid in ("autofillWithResume", "useMyLastApplication",
                        "fillWithResume", "resumeUpload", "fillWithResumeBtn",
                        "applyManually"):
                try:
                    b = page.locator(f'[data-automation-id="{aid}"]').first
                    b.wait_for(state="visible", timeout=6000)
                    b.click()
                    print(f"  Clicked: {aid}")
                    settle(page, 4000)
                    _step5_clicked = True
                    break
                except Exception:
                    pass
            if not _step5_clicked:
                try:
                    page.locator(
                        'button:has-text("Autofill"), a:has-text("Autofill")'
                    ).first.click()
                    settle(page, 3000)
                except Exception:
                    pass
        else:
            print("  Already handled in Step 3b (popup click)")

        # ── Step 5b: Handle auth wall that appears AFTER popup click ──────────
        _post_popup_auth = False
        for _pp_sel in (
            '[data-automation-id="verifyPassword"]',
            '[data-automation-id="formField-verifyPassword"]',
            '[data-automation-id="createAccountSubmitButton"]',
            '[data-automation-id="signInContent"]',
        ):
            try:
                page.locator(_pp_sel).first.wait_for(state="visible", timeout=4000)
                _post_popup_auth = True
                print(f"  [auth-post-popup] Create Account step detected via: {_pp_sel!r}")
                break
            except Exception:
                pass
        if _post_popup_auth:
            # Try Sign In (for tenants with stored credentials) then fall back to Create Account
            _relogin_ok = _try_create_account(page, tenant)

            # If auth form is STILL showing, try navigating directly to login URL
            if not _relogin_ok and tenant in TENANT_CREDENTIALS:
                print("  [auth-post-popup] Create Account failed — trying direct login URL")
                _login_base = re.match(r"(https?://[^/]+)", job_url).group(1)
                _sp_m = re.search(r"/en-US/([^/]+)/", job_url)
                _sp = _sp_m.group(1) if _sp_m else "External"
                _direct_login_url = _login_base + f"/en-US/{_sp}/login"
                try:
                    page.goto(_direct_login_url, wait_until="networkidle", timeout=30000)
                    settle(page, 2000)
                    _login_ok = _try_login(page, tenant)
                    if _login_ok:
                        # Re-navigate to autofill URL after successful login
                        page.goto(_autofill_url, wait_until="domcontentloaded", timeout=30000)
                        settle(page, 3000)
                        _relogin_ok = True
                    else:
                        print(f"  [auth-post-popup] Direct login also failed — will proceed anyway")
                        page.goto(_autofill_url, wait_until="domcontentloaded", timeout=30000)
                        settle(page, 3000)
                except Exception as _lx:
                    print(f"  [auth-post-popup] Direct login error: {_lx}")

            settle(page, 5000)
            # May need to click Continue/Next to advance past Create Account step
            try_next(page)
            settle(page, 4000)
            # After auth, ensure we're on the apply flow (not job page or login)
            _post_auth_url = page.url
            print(f"  [5b] URL after auth: {_post_auth_url}")
            if "apply" not in _post_auth_url and "autofill" not in _post_auth_url.lower():
                print(f"  [5b] Not on apply flow — re-navigating to autofill URL")
                page.goto(_autofill_url, wait_until="domcontentloaded", timeout=30000)
                settle(page, 5000)
            # Take a step6-pre screenshot to see the state
            try:
                page.screenshot(path=f"/tmp/wd_pre_step6_{tenant}.png")
            except Exception:
                pass

        # ── Step 6: Upload resume ─────────────────────────────────────────────
        print("[STEP 6] Uploading resume…")
        print(f"  [6] Current URL: {page.url}")
        try:
            fi = page.locator(
                '[data-automation-id="file-upload-input-ref"], input[type="file"]'
            ).first
            fi.wait_for(state="attached", timeout=15000)
            fi.set_input_files(resume_pdf)
            settle(page, 5000)
            # Wait for upload processing to complete (loading dots disappear)
            try:
                page.locator('[data-automation-id="file-loading-dots"]').wait_for(
                    state="hidden", timeout=20000
                )
            except Exception:
                pass
            settle(page, 2000)
            print("  Resume uploaded.")
        except Exception as exc:
            print(f"  [WARN] Upload: {exc}")

        # Wait for Continue button to become ENABLED (Workday processes resume server-side)
        print("  Waiting for Continue to be enabled…")
        for _retry in range(30):  # up to 30s
            try:
                _btn = page.locator('[data-automation-id="pageFooterNextButton"]').first
                # Use get_attribute to avoid is_disabled() default 30s wait
                _disabled = _btn.get_attribute("aria-disabled", timeout=500) or ""
                _class = _btn.get_attribute("class", timeout=500) or ""
                if _disabled != "true" and "disabled" not in _class.lower():
                    print(f"  Continue enabled after {_retry}s")
                    break
            except Exception:
                pass
            time.sleep(1)
        try_next(page)

        # Wait for My Information step to load (SPA transition)
        _mi_loaded = False
        for _aid_mi in ("applyFlowMyInfoPage", "applyFlowMyInformationPage",
                        "formField-phoneNumber", "formField-legalName--firstName"):
            try:
                page.locator(f'[data-automation-id="{_aid_mi}"]').wait_for(
                    state="visible", timeout=8000
                )
                print(f"  My Information step loaded (detected: {_aid_mi})")
                _mi_loaded = True
                break
            except Exception:
                pass
        if not _mi_loaded:
            print("  [WARN] Could not confirm My Information step loaded")
        settle(page, 1000)

        # ── Step 7: Fill contact info ─────────────────────────────────────────
        print("[STEP 7] Filling contact info…")
        # Try multiple automation-id patterns — varies by Workday instance:
        #   Standard: legalNameSection_firstName / addressSection_addressLine1 / phone-number
        #   MS External: formField containers with inputs inside
        def _fill_field(field_ids, value):
            """Try multiple selectors for a form field (Playwright + JS fallback)."""
            for _fid in field_ids:
                # Try direct automation-id and formField container children
                for sel in [
                    f'[data-automation-id="{_fid}"]',
                    f'[data-automation-id="formField-{_fid}"] input',
                    f'[data-automation-id="formField-{_fid}"] textarea',
                    f'[data-automation-id="formField-{_fid}"] [role="textbox"]',
                ]:
                    try:
                        loc = page.locator(sel).first
                        loc.wait_for(state="visible", timeout=1500)
                        loc.click(click_count=3)
                        loc.fill(value)
                        print(f"  Filled: {sel}")
                        return True
                    except Exception:
                        pass
                # JS fallback — set value via React native input setter
                try:
                    _set = page.evaluate('''(args) => {
                        var container = document.querySelector('[data-automation-id="formField-' + args.fid + '"]');
                        if (!container) return false;
                        var inp = container.querySelector('input') || container.querySelector('textarea') || container.querySelector('[role="textbox"]');
                        if (!inp) return false;
                        var setter = Object.getOwnPropertyDescriptor(inp.__proto__, 'value');
                        if (setter && setter.set) setter.set.call(inp, args.val);
                        else inp.value = args.val;
                        inp.dispatchEvent(new Event('input', {bubbles:true}));
                        inp.dispatchEvent(new Event('change', {bubbles:true}));
                        return true;
                    }''', {"fid": _fid, "val": value})
                    if _set:
                        print(f"  Filled via JS: formField-{_fid}")
                        return True
                except Exception:
                    pass
                # Last resort: click the container to focus, select-all, type
                try:
                    loc = page.locator(f'[data-automation-id="formField-{_fid}"]').first
                    loc.wait_for(state="visible", timeout=1500)
                    loc.click()
                    page.keyboard.key("Control+A")
                    page.keyboard.type(value)
                    time.sleep(0.3)
                    print(f"  Filled via keyboard: formField-{_fid}")
                    return True
                except Exception:
                    pass
            return False
        _fn = PROFILE["first_name"]
        _ln = PROFILE["last_name"]
        _mn = PROFILE["middle_name"]
        _fill_field(["legalNameSection_firstName", "legalName--firstName"], _fn)
        _fill_field(["legalNameSection_lastName",  "legalName--lastName"],  _ln)
        _fill_field(["legalNameSection_middleName","legalName--middleName"], _mn)
        _fill_field(["addressSection_addressLine1", "addressLine1"], PROFILE["address1"])
        _fill_field(["addressSection_city", "city"],                 PROFILE["city"])
        _fill_field(["addressSection_postalCode", "postalCode"],     PROFILE["postal_code"])
        # Phone: clear existing (autofill may have wrong format) and fill digits only
        _fill_field(["phone-number", "phoneNumber"], PROFILE["phone"])

        # Phone Device Type — select "Mobile" (required field, Workday single-select)
        try:
            _pt_cont = page.locator('[data-automation-id="formField-phoneType"]').first
            if _pt_cont.is_visible(timeout=2000):
                _pt_res = page.evaluate('''() => {
                    var c = document.querySelector('[data-automation-id="formField-phoneType"]');
                    if (!c) return "no-cont";
                    // Try radio inputs first
                    var radios = c.querySelectorAll('input[type="radio"]');
                    for (var i = 0; i < radios.length; i++) {
                        var r = radios[i];
                        var label = (r.labels && r.labels[0]) ? r.labels[0].innerText : "";
                        var v = (r.value + " " + label).toLowerCase();
                        if (v.includes("mobile") || v.includes("cell")) {
                            r.click();
                            r.dispatchEvent(new Event("change", {bubbles: true}));
                            return "radio-mobile:" + r.value;
                        }
                    }
                    // Workday custom select: open dropdown and pick Mobile
                    var btn = c.querySelector("button") || c.querySelector('[data-automation-id="promptIcon"]');
                    if (!btn) return "no-btn";
                    var br = btn.getBoundingClientRect();
                    var o = {bubbles:true, cancelable:true, clientX:br.left+br.width/2, clientY:br.top+br.height/2};
                    btn.dispatchEvent(new MouseEvent("mousedown", o));
                    btn.dispatchEvent(new MouseEvent("mouseup", o));
                    btn.dispatchEvent(new MouseEvent("click", o));
                    return "opened-dropdown";
                }''')
                print(f"  [phoneType] {_pt_res}")
                if "opened-dropdown" in str(_pt_res):
                    time.sleep(0.8)
                    _pt_pick = page.evaluate('''() => {
                        var opts = Array.from(document.querySelectorAll('[role="option"]'))
                            .filter(function(e) {
                                var r = e.getBoundingClientRect();
                                return r.width > 0 && r.height > 0;
                            });
                        for (var i = 0; i < opts.length; i++) {
                            var t = (opts[i].innerText || "").toLowerCase();
                            if (t.includes("mobile") || t.includes("cell")) {
                                var r = opts[i].getBoundingClientRect();
                                var o = {bubbles:true, cancelable:true,
                                         clientX:r.left+r.width/2, clientY:r.top+r.height/2};
                                opts[i].dispatchEvent(new MouseEvent("click", o));
                                return "picked:" + opts[i].innerText.trim();
                            }
                        }
                        // Fallback: pick first non-empty option
                        for (var i = 0; i < opts.length; i++) {
                            var t = (opts[i].innerText || "").trim().toLowerCase();
                            if (t && t !== "select one") {
                                var r = opts[i].getBoundingClientRect();
                                var o = {bubbles:true, cancelable:true,
                                         clientX:r.left+r.width/2, clientY:r.top+r.height/2};
                                opts[i].dispatchEvent(new MouseEvent("click", o));
                                return "picked-first:" + opts[i].innerText.trim();
                            }
                        }
                        return "no-opts";
                    }''')
                    print(f"  [phoneType-pick] {_pt_pick}")
        except Exception as _pte:
            print(f"  [phoneType-err] {_pte}")

        # Debug: print all formField IDs + their text so we can identify required fields
        try:
            _all_ff = page.evaluate('''() => {
                return Array.from(
                    document.querySelectorAll('[data-automation-id^="formField-"]')
                ).map(el => ({
                    id: el.getAttribute('data-automation-id'),
                    text: (el.innerText || '').substring(0, 80).replace(/\\n/g, ' ')
                }));
            }''')
            for _ff in _all_ff:
                print(f"  [field] {_ff['id']}: {_ff['text']!r}")
        except Exception as _dbe:
            print(f"  [field-dump] {_dbe}")

        # "Have you previously worked at Morgan Stanley?" → click No
        # Find container by visible text (not automation-id which varies per instance)
        _prev_done = False
        try:
            _pq = page.locator('[data-automation-id^="formField-"]').filter(
                has_text=re.compile(r"previously worked|former employee|previous worker", re.I)
            ).first
            _pq.wait_for(state="visible", timeout=3000)
            _pq_id = _pq.get_attribute("data-automation-id")
            print(f"  [prev-worker] container: {_pq_id}")
            # JS-based radio click to ensure React state update fires
            _prev_js_result = page.evaluate(f'''() => {{
                var c = document.querySelector('[data-automation-id="{_pq_id}"]');
                if (!c) return "container not found";
                var radios = c.querySelectorAll('input[type="radio"]');
                for (var r of radios) {{
                    var v = (r.value || "").toLowerCase();
                    if (v === "false" || v === "n" || v === "no" || v === "0") {{
                        r.click();
                        var ev = new Event('change', {{bubbles:true}});
                        r.dispatchEvent(ev);
                        return "clicked radio value=" + r.value;
                    }}
                }}
                // Fallback: click last radio (No is usually last)
                if (radios.length >= 2) {{
                    radios[radios.length-1].click();
                    var ev2 = new Event('change', {{bubbles:true}});
                    radios[radios.length-1].dispatchEvent(ev2);
                    return "clicked last radio";
                }}
                return "no radio found, count=" + radios.length;
            }}''')
            print(f"  [prev-worker-js] {_prev_js_result}")
            if "clicked" in (_prev_js_result or ""):
                _prev_done = True
                time.sleep(0.3)
            if not _prev_done:
                for _no_sel in (
                    'label:has-text("No")',
                    '[data-automation-id$="-N"]', '[data-automation-id$="-false"]',
                    '[data-automation-id*="No"]', 'input[value="false"]', 'input[value="N"]',
                ):
                    try:
                        _pq.locator(_no_sel).first.click(timeout=1000)
                        print(f"  Selected: previous worker = No ({_no_sel})")
                        _prev_done = True
                        time.sleep(0.3)
                        break
                    except Exception:
                        pass
        except Exception:
            pass
        if not _prev_done:
            print("  [WARN] Could not fill previous-worker field")

        # "How Did You Hear About Us?" → open multiselect, wait for options, select one
        _src_done = False
        try:
            _sq = page.locator('[data-automation-id^="formField-"]').filter(
                has_text=re.compile(r"hear about us|how did you hear", re.I)
            ).first
            _sq.wait_for(state="visible", timeout=3000)
            _sq_id = _sq.get_attribute("data-automation-id")
            print(f"  [source] container: {_sq_id}")
            _sq.scroll_into_view_if_needed()
            time.sleep(0.5)
            # Dump children of the source container for debugging
            try:
                _src_children = page.evaluate('''() => {
                    var c = document.querySelector('[data-automation-id="formField-source"]');
                    if (!c) return "not found";
                    return Array.from(c.querySelectorAll("*")).slice(0,20).map(e => ({
                        tag: e.tagName, aid: e.getAttribute("data-automation-id"),
                        role: e.getAttribute("role"), text: (e.innerText||"").substring(0,20)
                    }));
                }''')
                print(f"  [source-children] {_src_children}")
            except Exception:
                pass
            # Count options BEFORE opening (baseline for phone-code options already visible)
            _opts_before = page.locator('[role="option"]:visible').count()
            # Open the source multiselect.
            # The ≡ icon is [data-automation-id="promptIcon"] — this is the actual click target.
            _opened = False
            for _open_sel in (
                '[data-automation-id="promptIcon"]',    # ≡ icon — primary trigger
                'input',                                 # search input inside the multiselect
                '[data-automation-id="multiselectInputContainer"]',
                '[data-automation-id="multiSelectContainer"]',
            ):
                try:
                    _el = _sq.locator(_open_sel).first
                    _el.wait_for(state="visible", timeout=1500)
                    _el.scroll_into_view_if_needed()
                    _el.click(force=True)
                    time.sleep(1.2)
                    _opts_after = page.locator('[role="option"]:visible').count()
                    if _opts_after > _opts_before:
                        print(f"  Opened source via {_open_sel}: {_opts_after} options visible")
                        _opened = True
                        break
                    print(f"  Clicked {_open_sel} — options: {_opts_before} → {_opts_after}")
                except Exception as _oe:
                    print(f"  [open-err] {_open_sel}: {_oe}")
            if not _opened:
                # JS fallback: click promptIcon, then mousedown/mouseup sequence
                try:
                    _js_opened = page.evaluate('''() => {
                        var c = document.querySelector('[data-automation-id="formField-source"]');
                        if (!c) return "no container";
                        // Try promptIcon first
                        var icon = c.querySelector('[data-automation-id="promptIcon"]');
                        if (icon) {
                            icon.dispatchEvent(new MouseEvent("mousedown", {bubbles:true}));
                            icon.click();
                            icon.dispatchEvent(new MouseEvent("mouseup", {bubbles:true}));
                            return "JS-promptIcon";
                        }
                        // Try the INPUT
                        var inp = c.querySelector("input");
                        if (inp) { inp.focus(); inp.click(); return "JS-input"; }
                        c.click(); return "JS-container";
                    }''')
                    print(f"  [source-js-open] {_js_opened}")
                    time.sleep(1.2)
                    _opts_after2 = page.locator('[role="option"]:visible').count()
                    if _opts_after2 > _opts_before:
                        print(f"  JS open worked: {_opts_after2} options visible")
                        _opened = True
                except Exception as _je:
                    print(f"  [source-js-open-err] {_je}")
            # Snapshot after open attempt
            try:
                page.screenshot(path="/tmp/wd_source_open_ms.png")
            except Exception:
                pass
            # Tree-select uses promptLeafNode elements, NOT role="option" — check both
            if not _opened:
                _leaf_count = page.locator('[data-automation-id="promptLeafNode"]:visible').count()
                if _leaf_count > 0:
                    print(f"  [source] Dropdown open via promptLeafNode ({_leaf_count} nodes)")
                    _opened = True
            if not _opened:
                # Also check aria instruction for "Expanded" state
                try:
                    _open_aria = page.locator(
                        f'[data-automation-id="{_sq_id}"] '
                        '[data-automation-id="promptAriaInstruction"]'
                    ).inner_text(timeout=500)
                    if "Expanded" in _open_aria:
                        print(f"  [source] Dropdown open via aria Expanded")
                        _opened = True
                except Exception:
                    pass
            if _opened:
                # MS tree-select: all visible items are BRANCH nodes that navigate
                # deeper when clicked. We need to keep clicking the first visible
                # item until aria-instruction shows "1 item selected" (reached leaf).
                # Also inspect the DOM to understand the tree depth.

                # First: JS-inspect the visible options to understand their aria attrs
                try:
                    _tree_info = page.evaluate('''() => {
                        var opts = Array.from(document.querySelectorAll(
                            '[data-automation-id="promptLeafNode"]'
                        )).filter(e => {
                            var r = e.getBoundingClientRect();
                            return r.width > 0 && r.height > 0;
                        });
                        return opts.slice(0, 8).map(o => ({
                            text: (o.innerText || "").substring(0, 30).trim(),
                            expanded: o.getAttribute("aria-expanded"),
                            level:    o.getAttribute("aria-level"),
                            selected: o.getAttribute("aria-selected"),
                            hasPopup: o.getAttribute("aria-haspopup")
                        }));
                    }''')
                    print(f"  [source-tree-info] {_tree_info}")
                except Exception as _ti_e:
                    print(f"  [source-tree-info-err] {_ti_e}")

                # The tree uses a ReactVirtualized list. promptLeafNode lives inside
                # menuItem[role="option"]. Clicking a branch (e.g. "Career Site")
                # navigates into it — the [role="option"] items change to sub-items
                # (eFinancialCareers, LinkedIn, Morgan Stanley Career Site, Naukri, etc.)
                # while aria stays "Expanded". Strategy:
                #   1. Record initial [role="option"] texts
                #   2. Click the first branch item (Career Site)
                #   3. Wait for [role="option"] items to change → those are the sub-items
                #   4. Click the first sub-item that's not "India (+91)"
                #   5. Aria should now say "1 item selected"

                # Record initial role=option texts (include phone code items)
                _orig_role_opts = set()
                for _ro in page.locator('[role="option"]:visible').all():
                    try:
                        _orig_role_opts.add(_ro.inner_text(timeout=300).strip())
                    except Exception:
                        pass
                print(f"  [source-orig-opts] {sorted(_orig_role_opts)}")

                # Click the CHEVRON ">" (right side) of the first branch item to
                # navigate INTO it. Clicking the text side does nothing in Workday's
                # tree-select — only the right-side chevron triggers branch navigation.
                _leaf_loc = page.locator('[data-automation-id="promptLeafNode"]:visible')
                _bb0 = None
                try:
                    _bb0 = _leaf_loc.first.bounding_box()
                    _bt0 = _leaf_loc.first.inner_text(timeout=300).strip()
                    print(f"  [source-branch] clicking chevron of: {_bt0!r} at {_bb0}")
                except Exception:
                    _bt0 = "?"
                # Attach JS event listeners to verify clicks reach the element.
                # Then dispatch full mouse event sequence (the same approach that
                # worked in an earlier test to navigate into Career Site branch).
                try:
                    _listen_res = page.evaluate('''() => {
                        var item = Array.from(document.querySelectorAll('[role="option"]'))
                            .find(e => {
                                var r = e.getBoundingClientRect();
                                return r.width > 0 && r.height > 0 &&
                                       !e.innerText.includes("(+");
                            });
                        if (!item) return "no item";
                        window.__wd_click_events = [];
                        ["click","mousedown","mouseup","mouseover","mouseenter",
                         "pointerdown","pointerup"].forEach(function(evt) {
                            item.addEventListener(evt, function(e) {
                                window.__wd_click_events.push(evt);
                            }, true);
                        });
                        return "listening on: " + item.innerText.substring(0,20).trim();
                    }''')
                    print(f"  [source-listen] {_listen_res}")
                except Exception as _le2:
                    print(f"  [source-listen-err] {_le2}")

                # Test 1: Playwright locator click
                try:
                    page.get_by_role("option", name="Career Site").first.click(timeout=2000)
                    time.sleep(0.5)
                    _evt1 = page.evaluate("() => window.__wd_click_events || []")
                    print(f"  [source-locator-click] events received: {_evt1}")
                    _opts1 = [e.inner_text(timeout=200).strip()
                              for e in page.locator('[role="option"]:visible').all()]
                    print(f"  [source-locator-click] opts: {_opts1}")
                except Exception as _lce:
                    print(f"  [source-locator-click-err] {_lce}")

                # Test 2: Full dispatchEvent sequence on the promptLeafNode element
                # (This is what worked in a previous test to navigate into Career Site)
                try:
                    _dispatch_res = page.evaluate('''() => {
                        window.__wd_click_events = [];
                        var items = Array.from(document.querySelectorAll(
                            '[data-automation-id="promptLeafNode"]'
                        )).filter(e => e.getBoundingClientRect().width > 0);
                        if (!items.length) return "no items";
                        var el = items[0];
                        var r = el.getBoundingClientRect();
                        var cx = r.left + 20, cy = r.top + r.height / 2;
                        var opts = {bubbles:true, cancelable:true, clientX:cx, clientY:cy};
                        el.dispatchEvent(new MouseEvent("mouseover", opts));
                        el.dispatchEvent(new MouseEvent("mouseenter", opts));
                        el.dispatchEvent(new PointerEvent("pointerdown", opts));
                        el.dispatchEvent(new MouseEvent("mousedown", opts));
                        el.dispatchEvent(new PointerEvent("pointerup", opts));
                        el.dispatchEvent(new MouseEvent("mouseup", opts));
                        el.dispatchEvent(new MouseEvent("click", opts));
                        return "dispatched on: " + el.innerText.substring(0,20).trim();
                    }''')
                    print(f"  [source-dispatch] {_dispatch_res}")
                    time.sleep(1.5)
                    _opts2 = [e.inner_text(timeout=200).strip()
                              for e in page.locator('[role="option"]:visible').all()]
                    print(f"  [source-dispatch] opts after: {_opts2}")
                    # Branch navigated — now use dispatchEvent on the first leaf sub-item
                    _phone_codes = {"India (+91)", "United States (+1)", "United Kingdom (+44)"}
                    _sub_items = [t for t in _opts2
                                  if t and t not in _orig_role_opts and "(+" not in t
                                  and t not in _phone_codes]
                    print(f"  [source-sub-items] {_sub_items}")
                    if _sub_items:
                        _target_text = _sub_items[0]
                        _sel_res = page.evaluate(f'''() => {{
                            var items = Array.from(document.querySelectorAll(
                                '[data-automation-id="promptLeafNode"]'
                            )).filter(e => e.getBoundingClientRect().width > 0);
                            var el = items.find(e => e.innerText.trim().startsWith({_target_text[:30]!r}));
                            if (!el && items.length) el = items[0];
                            if (!el) return "no leaf found";
                            var r = el.getBoundingClientRect();
                            var cx = r.left + r.width / 2, cy = r.top + r.height / 2;
                            var opts = {{bubbles:true, cancelable:true, clientX:cx, clientY:cy}};
                            el.dispatchEvent(new MouseEvent("mouseover", opts));
                            el.dispatchEvent(new MouseEvent("mouseenter", opts));
                            el.dispatchEvent(new PointerEvent("pointerdown", opts));
                            el.dispatchEvent(new MouseEvent("mousedown", opts));
                            el.dispatchEvent(new PointerEvent("pointerup", opts));
                            el.dispatchEvent(new MouseEvent("mouseup", opts));
                            el.dispatchEvent(new MouseEvent("click", opts));
                            return "dispatched-leaf: " + el.innerText.substring(0,30).trim();
                        }}''')
                        print(f"  [source-leaf-dispatch] {_sel_res}")
                        time.sleep(1.0)
                        _aria_leaf = page.locator(
                            f'[data-automation-id="{_sq_id}"] '
                            '[data-automation-id="promptAriaInstruction"]'
                        ).inner_text(timeout=500)
                        print(f"  [source-leaf-aria] {_aria_leaf!r}")
                        if "1 item" in _aria_leaf:
                            _src_done = True
                        elif "Expanded" in _aria_leaf:
                            # Another branch level — dispatch on its first promptLeafNode
                            time.sleep(0.5)
                            _opts3 = [e.inner_text(timeout=200).strip()
                                      for e in page.locator('[role="option"]:visible').all()]
                            _sub2 = [t for t in _opts3
                                     if t and t not in _orig_role_opts and t not in _sub_items
                                     and "(+" not in t and t not in _phone_codes]
                            print(f"  [source-sub2] {_sub2}")
                            if _sub2:
                                _t2 = _sub2[0]
                                _sel2 = page.evaluate(f'''() => {{
                                    var items = Array.from(document.querySelectorAll(
                                        '[data-automation-id="promptLeafNode"]'
                                    )).filter(e => e.getBoundingClientRect().width > 0);
                                    var el = items.find(e => e.innerText.trim().startsWith({_t2[:30]!r}));
                                    if (!el && items.length) el = items[0];
                                    if (!el) return "no leaf2";
                                    var r = el.getBoundingClientRect();
                                    var cx = r.left + r.width / 2, cy = r.top + r.height / 2;
                                    var opts = {{bubbles:true, cancelable:true, clientX:cx, clientY:cy}};
                                    el.dispatchEvent(new MouseEvent("mouseover", opts));
                                    el.dispatchEvent(new MouseEvent("mouseenter", opts));
                                    el.dispatchEvent(new PointerEvent("pointerdown", opts));
                                    el.dispatchEvent(new MouseEvent("mousedown", opts));
                                    el.dispatchEvent(new PointerEvent("pointerup", opts));
                                    el.dispatchEvent(new MouseEvent("mouseup", opts));
                                    el.dispatchEvent(new MouseEvent("click", opts));
                                    return "dispatched-leaf2: " + el.innerText.substring(0,30).trim();
                                }}''')
                                print(f"  [source-leaf2-dispatch] {_sel2}")
                                time.sleep(1.0)
                                _aria2 = page.locator(
                                    f'[data-automation-id="{_sq_id}"] '
                                    '[data-automation-id="promptAriaInstruction"]'
                                ).inner_text(timeout=500)
                                print(f"  [source-leaf2-aria] {_aria2!r}")
                                if "1 item" in _aria2:
                                    _src_done = True
                except Exception as _de:
                    print(f"  [source-dispatch-err] {_de}")

            # Close source dropdown: press Tab to blur the source field and close popup
            try:
                page.keyboard.press("Tab")
                time.sleep(0.6)
                # Check if activeListContainer is now hidden
                _src_popup_vis = page.evaluate('''() => {
                    var c = document.querySelector('[data-automation-id="activeListContainer"]');
                    if (!c) return 0;
                    var r = c.getBoundingClientRect();
                    return r.width > 0 && r.height > 0 ? 1 : 0;
                }''')
                print(f"  [source-popup-vis] {_src_popup_vis}")
                if _src_popup_vis:
                    # Still open — press Tab again and then Escape
                    page.keyboard.press("Tab")
                    time.sleep(0.3)
                    page.keyboard.press("Escape")
                    time.sleep(0.4)
                    _src_popup_vis2 = page.evaluate('''() => {
                        var c = document.querySelector('[data-automation-id="activeListContainer"]');
                        if (!c) return 0;
                        var r = c.getBoundingClientRect();
                        return r.width > 0 && r.height > 0 ? 1 : 0;
                    }''')
                    print(f"  [source-popup-vis2] {_src_popup_vis2}")
            except Exception as _sc_e:
                print(f"  [source-close-err] {_sc_e}")
            # Verify final state
            try:
                _src_final = page.locator(
                    f'[data-automation-id="{_sq_id}"]'
                ).inner_text(timeout=1000)
                print(f"  [source-closed] state: {_src_final[:80]!r}")
            except Exception:
                print(f"  [source-closed] could not verify state")
        except Exception as _se:
            print(f"  [source-error] {_se}")
        if not _src_done:
            print("  [WARN] Could not fill source/hear-about-us field")

        # Ensure source popup is fully closed before filling state.
        # (Tab blur is the most reliable way to close Workday multi-select tree popups)
        try:
            _src_still_open = page.evaluate('''() => {
                var c = document.querySelector('[data-automation-id="activeListContainer"]');
                if (!c) return false;
                var r = c.getBoundingClientRect();
                return r.width > 0 && r.height > 20;
            }''')
            if _src_still_open:
                print(f"  [state-pre-close] source popup still open — pressing Tab")
                page.keyboard.press("Tab")
                time.sleep(0.6)
                # Fallback: Escape
                _still = page.evaluate('''() => {
                    var c = document.querySelector('[data-automation-id="activeListContainer"]');
                    if (!c) return false;
                    var r = c.getBoundingClientRect();
                    return r.width > 0 && r.height > 20;
                }''')
                if _still:
                    page.keyboard.press("Escape")
                    time.sleep(0.4)
        except Exception:
            pass
        time.sleep(0.3)

        # Fill State field (formField-countryRegion) — Workday single-select dropdown
        try:
            _state_cont = page.locator('[data-automation-id="formField-countryRegion"]').first
            _state_cont.wait_for(state="visible", timeout=3000)
            _state_cont.scroll_into_view_if_needed()
            time.sleep(0.3)

            # Record pre-existing [role="option"] elements so we can identify NEW ones
            # that appear only after the state dropdown opens
            _pre_opts_set = set(page.evaluate('''() => {
                return Array.from(document.querySelectorAll('[role="option"]'))
                    .filter(function(e) {
                        var r = e.getBoundingClientRect();
                        return r.width > 0 && r.height > 0;
                    })
                    .map(function(e) { return (e.innerText || "").trim().substring(0, 40); });
            }'''))
            print(f"  [state-pre-opts] pre-existing options: {sorted(_pre_opts_set)}")

            # --- Attempt 1: native <select> (Workday sometimes hides one behind custom widget)
            _st_native_res = page.evaluate('''() => {
                var cont = document.querySelector('[data-automation-id="formField-countryRegion"]');
                if (!cont) return "no-cont";
                var sel = cont.querySelector('select');
                if (!sel) return "no-native-select";
                var opts = Array.from(sel.options).map(function(o) { return o.text; });
                return "native-select opts=" + opts.length + " first5=" + opts.slice(0,5).join("|");
            }''')
            print(f"  [state-native-check] {_st_native_res}")

            _state_selected_native = False
            if "native-select" not in _st_native_res or "no-native-select" in _st_native_res:
                pass  # No native select — will use custom widget approach
            else:
                # Use Playwright's select_option on the hidden native select
                try:
                    page.locator('[data-automation-id="formField-countryRegion"] select').select_option(
                        label="Karnataka", timeout=2000
                    )
                    time.sleep(0.5)
                    _st_nat_verify = page.evaluate('''() => {
                        var cont = document.querySelector('[data-automation-id="formField-countryRegion"]');
                        return cont ? (cont.innerText||"").substring(0,60).trim() : "";
                    }''')
                    if "Karnataka" in _st_nat_verify:
                        _state_selected_native = True
                        print(f"  [state-native-select] Selected Karnataka via native select!")
                except Exception as _nat_e:
                    print(f"  [state-native-err] {_nat_e}")

            # --- Attempt 2: Playwright locator force-click the trigger
            # Scroll state into center of viewport first
            page.evaluate('''() => {
                var el = document.querySelector('[data-automation-id="formField-countryRegion"]');
                if (el) el.scrollIntoView({block: "center", behavior: "instant"});
            }''')
            time.sleep(0.3)

            # Check what's actually at the state field position
            _elem_at_point = page.evaluate('''() => {
                var cont = document.querySelector('[data-automation-id="formField-countryRegion"]');
                if (!cont) return "no-cont";
                var r = cont.getBoundingClientRect();
                // Check trigger area (lower half of container = the dropdown widget)
                var trigY = r.top + r.height * 0.75;
                var trigX = r.left + r.width * 0.3;
                var topEl = document.elementFromPoint(trigX, trigY);
                var info = topEl ? {
                    tag: topEl.tagName,
                    aid: topEl.dataset ? topEl.dataset.automationId||'' : '',
                    x: Math.round(trigX), y: Math.round(trigY),
                    isChild: cont.contains(topEl)
                } : null;
                return JSON.stringify(info);
            }''')
            print(f"  [state-elem-at-point] {_elem_at_point}")

            # Set up MutationObserver to capture ALL DOM changes during click
            page.evaluate('''() => {
                window._stMutations = [];
                var obs = new MutationObserver(function(muts) {
                    muts.forEach(function(m) {
                        m.addedNodes.forEach(function(n) {
                            if (n.nodeType === 1) {
                                window._stMutations.push('ADD:' + n.tagName + ':' + (n.dataset ? n.dataset.automationId||'' : '') + ':' + (n.innerText||'').trim().substring(0,30));
                            }
                        });
                        m.removedNodes.forEach(function(n) {
                            if (n.nodeType === 1) {
                                window._stMutations.push('REM:' + n.tagName + ':' + (n.dataset ? n.dataset.automationId||'' : '') + ':' + (n.innerText||'').trim().substring(0,15));
                            }
                        });
                    });
                });
                obs.observe(document.body, {childList: true, subtree: true, attributes: false});
                window._stObserver = obs;
            }''')

            # Reset focus away from state button (Tab from source may have landed focus there).
            # Click a neutral element (postalCode field) to ensure state dropdown is not pre-focused.
            try:
                _pc = page.locator('[data-automation-id="formField-postalCode"] input').first
                _pc.click(timeout=2000)
                time.sleep(0.2)
                page.keyboard.press("Escape")
                time.sleep(0.2)
            except Exception:
                pass

            # Dump the DOM structure of formField-countryRegion for diagnosis
            _st_cont_dump = page.evaluate('''() => {
                var cont = document.querySelector('[data-automation-id="formField-countryRegion"]');
                if (!cont) return "no-cont";
                var all = Array.from(cont.querySelectorAll("*")).slice(0, 25);
                return all.map(function(e) {
                    var r = e.getBoundingClientRect();
                    return {tag:e.tagName,aid:e.dataset?e.dataset.automationId||'':'',role:e.getAttribute('role')||'',text:(e.innerText||'').trim().substring(0,20),y:Math.round(r.top),w:Math.round(r.width),h:Math.round(r.height)};
                });
            }''')
            print(f"  [state-cont-dump] {_st_cont_dump}")

            # The state trigger is a BUTTON inside countryRegion.
            # Try 3 methods in order:
            # 1) JS dispatchEvent on the button (same pattern that worked for source leaf)
            # 2) Playwright force-click on the button
            # 3) JS native .click() on the button
            _st_click_res = "not-tried"
            _st_js_dispatch = page.evaluate('''() => {
                var cont = document.querySelector('[data-automation-id="formField-countryRegion"]');
                if (!cont) return "no-cont";
                // Find the dropdown trigger: prefer button, fallback to promptIcon, fallback to container
                var btn = cont.querySelector('[data-automation-id="promptIcon"]')
                         || cont.querySelector('button')
                         || cont;
                var r = btn.getBoundingClientRect();
                if (r.width === 0 && r.height === 0) return "btn-zero-size";
                var cx = r.left + r.width/2, cy = r.top + r.height/2;
                var o = {bubbles:true, cancelable:true, clientX:cx, clientY:cy, view:window};
                btn.dispatchEvent(new MouseEvent('mouseover', o));
                btn.dispatchEvent(new PointerEvent('pointerover', o));
                btn.dispatchEvent(new PointerEvent('pointerenter', o));
                btn.dispatchEvent(new PointerEvent('pointerdown', {bubbles:true,cancelable:true,clientX:cx,clientY:cy,pointerId:1}));
                btn.dispatchEvent(new MouseEvent('mousedown', o));
                btn.dispatchEvent(new PointerEvent('pointerup', {bubbles:true,cancelable:true,clientX:cx,clientY:cy,pointerId:1}));
                btn.dispatchEvent(new MouseEvent('mouseup', o));
                btn.dispatchEvent(new MouseEvent('click', o));
                return "dispatched:" + btn.tagName + ":" + (btn.dataset?btn.dataset.automationId||'':'') + " at " + Math.round(cx) + "," + Math.round(cy);
            }''')
            print(f"  [state-js-dispatch] {_st_js_dispatch}")
            _st_click_res = _st_js_dispatch
            time.sleep(1.8)  # Give React time to render any popup

            # Stop observer and get mutations
            _st_mut_log = page.evaluate('''() => {
                if (window._stObserver) window._stObserver.disconnect();
                return window._stMutations || [];
            }''')
            print(f"  [state-mutations] count={len(_st_mut_log)} sample={_st_mut_log[:10]}")

            # Check focused element
            _focused_el = page.evaluate('''() => {
                var ae = document.activeElement;
                if (!ae) return null;
                return {tag: ae.tagName, aid: ae.dataset ? ae.dataset.automationId||'' : ''};
            }''')
            print(f"  [state-focused] {_focused_el}")

            # Broad DOM dump: ALL new elements in entire doc (portals may render anywhere)
            _st_dom_new = page.evaluate('''() => {
                return Array.from(document.querySelectorAll(
                    '[role="option"],[role="listbox"],[data-automation-id*="Option"],'
                    + '[data-automation-id*="Item"],[data-automation-id*="Leaf"],'
                    + '[data-automation-id*="select"],[data-automation-id*="list"]'
                )).filter(function(e) {
                    var r = e.getBoundingClientRect();
                    return r.width > 0 && r.height > 0;
                }).map(function(e) {
                    return {
                        aid: e.dataset ? e.dataset.automationId : null,
                        role: e.getAttribute('role'),
                        text: (e.innerText || '').trim().substring(0, 30),
                        y: Math.round(e.getBoundingClientRect().y)
                    };
                });
            }''')
            print(f"  [state-dom-after-click] {_st_dom_new[:12]}")

            # Full ALL visible data-automation-id elements in range y=-200 to 1200
            _st_all_aids = page.evaluate('''() => {
                return Array.from(document.querySelectorAll('[data-automation-id]'))
                    .filter(function(e) {
                        var r = e.getBoundingClientRect();
                        return r.width > 10 && r.height > 5 && r.y > -200 && r.y < 1200;
                    }).map(function(e) {
                        return {
                            aid: e.dataset.automationId,
                            text: (e.innerText||'').trim().substring(0,25),
                            y: Math.round(e.getBoundingClientRect().y)
                        };
                    });
            }''')
            print(f"  [state-all-aids] count={len(_st_all_aids)} sample={_st_all_aids[10:20]}")

            # Check ALL options, then filter out pre-existing ones to find NEW state options
            _all_opts_after = [e['text'] for e in _st_dom_new if e.get('role') == 'option' or (e.get('aid') and ('Option' in (e.get('aid') or '') or 'Item' in (e.get('aid') or '')))]
            _new_opts = [o for o in _all_opts_after if o not in _pre_opts_set]
            print(f"  [state-opts-initial] all={_all_opts_after[:6]} new={_new_opts[:6]}")

            # Verify these are Indian state names
            _STATE_HINTS = {"Pradesh", "Karnataka", "Maharashtra", "Goa", "Bihar",
                            "Gujarat", "Rajasthan", "Kerala", "Tamil", "Jharkhand",
                            "Chhattisgarh", "Uttarakhand", "Andaman", "Arunachal",
                            "Assam", "Chandigarh", "Delhi", "Haryana", "Himachal"}
            _check_opts = _new_opts if _new_opts else _all_opts_after
            _is_state_dd = any(any(s in o for s in _STATE_HINTS) for o in _check_opts)
            _st_opts_initial = _check_opts

            # If state dropdown is open, scroll via JS on the listbox element
            if _is_state_dd:
                _scroll_res = page.evaluate('''() => {
                    // Find the open listbox (Karnataka is ~16th, need to scroll ~600px)
                    var lbs = Array.from(document.querySelectorAll('[role="listbox"]'))
                        .filter(function(e) {
                            var r = e.getBoundingClientRect();
                            return r.width > 50 && r.height > 50;
                        });
                    if (lbs.length) {
                        lbs.sort(function(a,b) {
                            return b.getBoundingClientRect().height - a.getBoundingClientRect().height;
                        });
                        var lb = lbs[0];
                        lb.scrollTop = 600;
                        return "lb-scrolled h=" + Math.round(lb.getBoundingClientRect().height)
                            + " scrollH=" + lb.scrollHeight + " top=" + lb.scrollTop;
                    }
                    // Fallback: find scrollable parent of any option
                    var firstOpt = document.querySelector('[role="option"]');
                    if (!firstOpt) return "no-opts";
                    var p = firstOpt.parentElement;
                    while (p && p.scrollHeight <= p.clientHeight + 10 && p !== document.body) {
                        p = p.parentElement;
                    }
                    if (p && p !== document.body) {
                        p.scrollTop = 600;
                        return "parent-scrolled " + p.tagName + " scrollH=" + p.scrollHeight;
                    }
                    return "no-scrollable-parent";
                }''')
                print(f"  [state-lb-scroll] {_scroll_res}")
                time.sleep(0.5)

                _st_opts_scrolled = page.evaluate('''() => {
                    return Array.from(document.querySelectorAll(
                        '[role="option"],[data-automation-id="selectOption"],[data-automation-id="listItem"]'
                    )).filter(function(e) {
                            var r = e.getBoundingClientRect();
                            return r.width > 0 && r.height > 0;
                        })
                        .map(function(e) { return (e.innerText || "").trim().substring(0, 40); });
                }''')
                print(f"  [state-opts-scrolled] {_st_opts_scrolled[:12]}")

            # Select Karnataka via dispatchEvent
            _state_selected = False
            _st_dispatch_res = page.evaluate('''() => {
                var opts = Array.from(document.querySelectorAll(
                    '[role="option"],[data-automation-id="selectOption"],[data-automation-id="listItem"]'
                )).filter(function(e) {
                        var r = e.getBoundingClientRect();
                        var t = e.innerText || "";
                        return r.width > 0 && r.height > 0
                            && (t.includes("Karnataka") || t.includes("Karn")) && !t.includes("(+");
                    });
                if (!opts.length) return "not-found";
                var el = opts[0];
                var r = el.getBoundingClientRect();
                var cx = r.left + r.width/2, cy = r.top + r.height/2;
                var o = {bubbles:true, cancelable:true, clientX:cx, clientY:cy};
                el.dispatchEvent(new MouseEvent("mouseover", o));
                el.dispatchEvent(new PointerEvent("pointerdown", o));
                el.dispatchEvent(new MouseEvent("mousedown", o));
                el.dispatchEvent(new PointerEvent("pointerup", o));
                el.dispatchEvent(new MouseEvent("mouseup", o));
                el.dispatchEvent(new MouseEvent("click", o));
                return "dispatched: " + (el.innerText || "").substring(0, 30);
            }''')
            print(f"  [state-dispatch] {_st_dispatch_res}")

            if "dispatched" in str(_st_dispatch_res):
                time.sleep(0.5)
                _st_verify = page.evaluate('''() => {
                    var cont = document.querySelector('[data-automation-id="formField-countryRegion"]');
                    return cont ? (cont.innerText || "").substring(0, 80).trim() : "not found";
                }''')
                print(f"  [state-verify] {_st_verify!r}")
                if "Karnataka" in str(_st_verify) or "Karn" in str(_st_verify):
                    _state_selected = True
                    print(f"  Selected state: {_st_verify!r}")

            if not _state_selected:
                _st_all_vis = page.evaluate('''() => {
                    return Array.from(document.querySelectorAll('[role="option"]'))
                        .filter(function(e) {
                            var r = e.getBoundingClientRect();
                            return r.width > 0 && r.height > 0;
                        })
                        .map(function(e) { return (e.innerText || "").trim().substring(0, 40); });
                }''')
                print(f"  [state-debug] visible options: {_st_all_vis}")
        except Exception as _state_ex:
            print(f"  [state-error] {_state_ex}")
        # Email fields (guest flow)
        _fill_field(["email", "emailAddress"], PROFILE["email"])

        # ── Education section (My Experience page) ────────────────────────────
        # Dismiss cookie banner first (may block interactions)
        try:
            for _ck_sel in ['button:has-text("Decline")', 'button:has-text("Accept Cookies")', 'button:has-text("Accept")']:
                try:
                    _ck = page.locator(_ck_sel).first
                    if _ck.is_visible(timeout=1000):
                        _ck.evaluate("el => el.click()")
                        print(f"  [cookie] Dismissed via {_ck_sel}")
                        time.sleep(0.5)
                        break
                except Exception:
                    pass
        except Exception:
            pass

        # ── Helper: open a Workday custom select and pick an option ──────────────
        def _open_wd_select_and_pick(cont_aid, pick_texts):
            """Open Workday custom select inside cont_aid, select first option matching any pick_text."""
            _r = page.evaluate(f'''() => {{
                var c = document.querySelector('[data-automation-id="{cont_aid}"]');
                if (!c) return "no-cont";
                var b = c.querySelector('button');
                if (!b) return "no-btn";
                var r = b.getBoundingClientRect();
                if (!r.width) return "zero-size";
                var cx=r.left+r.width/2,cy=r.top+r.height/2;
                var o={{bubbles:true,cancelable:true,clientX:cx,clientY:cy,view:window}};
                b.dispatchEvent(new MouseEvent('mouseover',o));
                b.dispatchEvent(new PointerEvent('pointerdown',{{bubbles:true,cancelable:true,clientX:cx,clientY:cy,pointerId:1}}));
                b.dispatchEvent(new MouseEvent('mousedown',o));
                b.dispatchEvent(new PointerEvent('pointerup',{{bubbles:true,cancelable:true,clientX:cx,clientY:cy,pointerId:1}}));
                b.dispatchEvent(new MouseEvent('mouseup',o));
                b.dispatchEvent(new MouseEvent('click',o));
                return "opened";
            }}''')
            if "opened" not in str(_r):
                return f"open-failed:{_r}"
            time.sleep(0.8)
            _pick_js = f'''() => {{
                var targets = {pick_texts};
                var opts = Array.from(document.querySelectorAll('[role="option"]'))
                    .filter(function(e){{var r=e.getBoundingClientRect();return r.width>0&&r.height>0;}});
                if (!opts.length) return "no-opts";
                for (var p = 0; p < targets.length; p++) {{
                    for (var i = 0; i < opts.length; i++) {{
                        var t = (opts[i].innerText||"").trim().toLowerCase();
                        if (t.includes(targets[p].toLowerCase())) {{
                            var r=opts[i].getBoundingClientRect();
                            var cx=r.left+r.width/2,cy=r.top+r.height/2;
                            var o={{bubbles:true,cancelable:true,clientX:cx,clientY:cy}};
                            opts[i].dispatchEvent(new MouseEvent("click",o));
                            return "picked:" + (opts[i].innerText||"").trim().substring(0,30);
                        }}
                    }}
                }}
                // Fallback: pick first non-placeholder option
                for (var i = 0; i < opts.length; i++) {{
                    var t = (opts[i].innerText||"").trim().toLowerCase();
                    if (t !== "select one" && t !== "" && !t.startsWith("select")) {{
                        var r=opts[i].getBoundingClientRect();
                        var cx=r.left+r.width/2,cy=r.top+r.height/2;
                        var o={{bubbles:true,cancelable:true,clientX:cx,clientY:cy}};
                        opts[i].dispatchEvent(new MouseEvent("click",o));
                        return "picked-first:" + (opts[i].innerText||"").trim().substring(0,30);
                    }}
                }}
                return "no-match opts=" + opts.slice(0,4).map(function(e){{return e.innerText.trim();}}).join("|");
            }}'''
            return page.evaluate(_pick_js)

        # ── Handle Language entry if accidentally present on My Experience page ──
        # Language entry: delete it if present (it was accidentally added) or fill it
        try:
            page.evaluate("window.scrollTo(0, 800)")
            time.sleep(0.3)

            # Try to DELETE the Language entry (look for Delete button near formField-language)
            _lang_del_res = page.evaluate('''() => {
                var langField = document.querySelector('[data-automation-id="formField-language"]');
                if (!langField) return "no-lang-section";
                var container = langField.parentElement;
                for (var i = 0; i < 10; i++) {
                    if (!container || container === document.body) break;
                    var btns = Array.from(container.querySelectorAll('button,a'));
                    for (var j = 0; j < btns.length; j++) {
                        var b = btns[j];
                        var t = (b.innerText||b.getAttribute("aria-label")||"").trim().toLowerCase();
                        var r = b.getBoundingClientRect();
                        if (r.width > 0 && r.height > 0 &&
                            (t === "delete" || t.includes("delete") || t === "remove" || t === "×" || t === "x")) {
                            b.click();
                            return "deleted-lang-entry:" + t;
                        }
                    }
                    container = container.parentElement;
                }
                return "lang-field-found-no-delete-btn";
            }''')
            print(f"  [lang-delete] {_lang_del_res}")

            if "deleted" in str(_lang_del_res):
                time.sleep(1.0)
            elif "lang-field-found" in str(_lang_del_res):
                print("  [lang-fill] Cannot delete, filling Language entry with English")
                _lr1 = _open_wd_select_and_pick("formField-language", ["english"])
                print(f"  [lang-language] {_lr1}")
                time.sleep(0.3)
                try:
                    _fl_cb = page.locator('[data-automation-id="formField-native"] input[type="checkbox"]').first
                    if not _fl_cb.is_visible(timeout=1000):
                        _fl_cb = page.locator('[data-automation-id="formField-native"] input').first
                    _fl_cb.evaluate("el => { if (!el.checked) el.click(); }")
                    print("  [lang-fluent] checked")
                except Exception as _fl_ex:
                    print(f"  [lang-fluent-err] {_fl_ex}")
                for _lang_aid in [
                    "formField-5a72a211d621102a1ee413132c0582a8",
                    "formField-5a72a211d621102a1ee411fb7e5d82a7",
                    "formField-5a72a211d621102a1ee41082058d82a6",
                    "formField-5a72a211d621102a1ee40f616abd82a5",
                    "formField-5a72a211d621102a1ee40c6c9c8d82a4",
                ]:
                    _lpr = _open_wd_select_and_pick(_lang_aid, ["native", "fluent", "proficient", "advanced", "yes"])
                    print(f"  [lang-prof] {_lang_aid[-8:]} → {_lpr}")
                    time.sleep(0.2)
        except Exception as _lang_ex:
            print(f"  [lang-err] {_lang_ex}")

        # ── Add Education entry (required by Morgan Stanley) ──────────────────────
        try:
            page.evaluate("window.scrollTo(0, 600)")
            time.sleep(0.4)
            # Check if education entry already exists (has school field visible)
            _edu_already = page.evaluate('''() => {
                var schoolF = document.querySelector(
                    '[data-automation-id="formField-school"] input, '
                    + '[data-automation-id="formField-schoolName"] input, '
                    + '[data-automation-id="formField-educationalInstitution"] input, '
                    + '[data-automation-id="schoolName"]'
                );
                return schoolF && schoolF.getBoundingClientRect().width > 0 ? "exists" : "none";
            }''')
            print(f"  [edu-check] {_edu_already}")

            if "exists" not in str(_edu_already):
                # Click the "Add" button specifically under the Education heading
                _edu_add_res = page.evaluate('''() => {
                    // Strategy 1: find heading whose text is exactly "Education"
                    var allElems = Array.from(document.querySelectorAll('h2,h3,h4,p,div,span'));
                    for (var i = 0; i < allElems.length; i++) {
                        var el = allElems[i];
                        var txt = (el.innerText || el.textContent || "").trim();
                        if (txt === "Education") {
                            var parent = el.parentElement;
                            for (var d = 0; d < 5; d++) {
                                if (!parent || parent === document.body) break;
                                var btns = Array.from(parent.querySelectorAll("button"));
                                for (var j = 0; j < btns.length; j++) {
                                    var b = btns[j];
                                    var bt = (b.innerText || b.textContent || "").trim();
                                    var r = b.getBoundingClientRect();
                                    if (bt === "Add" && r.width > 0 && r.height > 0) {
                                        b.scrollIntoView({block:"center"});
                                        b.click();
                                        return "clicked-edu-add-via-heading";
                                    }
                                }
                                parent = parent.parentElement;
                            }
                        }
                    }
                    // Strategy 2: all "Add" buttons — pick the one whose ancestor mentions Education but not Work/Cert
                    var addBtns = Array.from(document.querySelectorAll("button"))
                        .filter(function(b) {
                            var t = (b.innerText||b.textContent||"").trim();
                            var r = b.getBoundingClientRect();
                            return t === "Add" && r.width > 0 && r.height > 0;
                        });
                    for (var i = 0; i < addBtns.length; i++) {
                        var btn = addBtns[i];
                        var p = btn.parentElement;
                        for (var d = 0; d < 8; d++) {
                            if (!p || p === document.body) break;
                            var ptxt = (p.innerText || "").toLowerCase();
                            if (ptxt.includes("education") && !ptxt.includes("work experience") && !ptxt.includes("certification")) {
                                btn.scrollIntoView({block:"center"});
                                btn.click();
                                return "clicked-edu-add-via-parent";
                            }
                            p = p.parentElement;
                        }
                    }
                    // Strategy 3: pick first "Add" (not "Add Another") button
                    for (var i = 0; i < addBtns.length; i++) {
                        addBtns[i].scrollIntoView({block:"center"});
                        addBtns[i].click();
                        return "clicked-first-add-fallback";
                    }
                    return "no-edu-add-found";
                }''')
                print(f"  [edu-add] {_edu_add_res}")

                if "clicked" in str(_edu_add_res):
                    time.sleep(1.8)  # Wait for education form to expand
                    page.screenshot(path=f"/tmp/wd_edu_form_{tenant}.png")

                    # Fill school name using typeahead — must pick from autocomplete
                    # Uses React native input setter to properly trigger Workday's search
                    _edu_school_ok = False

                    def _wd_typeahead_search(field_aid, search_term, wait_s=3.0):
                        """Type into a Workday typeahead field using React-compatible events. Returns list of visible option texts."""
                        # Find the visible search input (not hidden value inputs)
                        _set_res = page.evaluate(f'''() => {{
                            var cont = document.querySelector('[data-automation-id="{field_aid}"]');
                            if (!cont) return "no-cont";
                            // Prefer visible input with placeholder "Search" or any visible input
                            var inp = cont.querySelector('input[placeholder="Search"]')
                                   || Array.from(cont.querySelectorAll("input"))
                                       .filter(function(i){{var r=i.getBoundingClientRect();return r.width>0&&r.height>0&&i.type!=="hidden";}})
                                       [0];
                            if (!inp) return "no-inp";
                            inp.focus();
                            inp.click();
                            // Use React's native input value setter
                            try {{
                                var setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "value").set;
                                setter.call(inp, "{search_term}");
                            }} catch(e) {{
                                inp.value = "{search_term}";
                            }}
                            inp.dispatchEvent(new Event("focus", {{bubbles:true}}));
                            inp.dispatchEvent(new Event("input", {{bubbles:true}}));
                            inp.dispatchEvent(new Event("change", {{bubbles:true}}));
                            inp.dispatchEvent(new KeyboardEvent("keydown", {{bubbles:true,key:"k",keyCode:75}}));
                            inp.dispatchEvent(new KeyboardEvent("keyup", {{bubbles:true,key:"k",keyCode:75}}));
                            return "set:" + inp.value + " on " + inp.tagName + "[placeholder=" + inp.placeholder + "]";
                        }}''')
                        print(f"  [wd-typeahead-set] {_set_res}")
                        time.sleep(wait_s)
                        # Collect visible options from multiple possible selectors
                        _opt_texts = page.evaluate('''() => {
                            var sels = [
                                '[role="option"]', '[role="listitem"]', 'li[tabindex]',
                                '[data-automation-id*="listItem"]', '[data-automation-id*="Option"]',
                                '[data-automation-id*="option"]', '[class*="autocomplete"] li',
                            ];
                            var seen = {};
                            var results = [];
                            sels.forEach(function(sel) {
                                Array.from(document.querySelectorAll(sel))
                                    .filter(function(e){
                                        var r=e.getBoundingClientRect();
                                        return r.width>0&&r.height>0;
                                    })
                                    .forEach(function(e){
                                        var t=(e.innerText||"").trim();
                                        if (t && !seen[t]) { seen[t]=true; results.push({el:e.outerHTML.substring(0,50),text:t.substring(0,40)}); }
                                    });
                            });
                            return results;
                        }''')
                        return _opt_texts

                    # Attempt to find school via typeahead — try multiple terms
                    # KIIT is not in Workday DB; fallback to any Indian/known university
                    _SKIP_OPT_TEXTS = {"no items.", "no results.", "no matches.", "no items found.", "no results found."}

                    def _pick_first_valid_option():
                        """Click the first non-empty, non-placeholder option visible on page."""
                        return page.evaluate(f'''() => {{
                            var sels = ['[role="option"]','[role="listitem"]','li[tabindex]','[data-automation-id*="listItem"]','[data-automation-id*="Option"]'];
                            var skip = {list(_SKIP_OPT_TEXTS)};
                            for (var si=0;si<sels.length;si++) {{
                                var opts = Array.from(document.querySelectorAll(sels[si]))
                                    .filter(function(e){{var r=e.getBoundingClientRect();return r.width>0&&r.height>0;}});
                                for (var i=0;i<opts.length;i++) {{
                                    var t=(opts[i].innerText||"").trim().toLowerCase();
                                    if (!t || skip.indexOf(t) >= 0) continue;
                                    var r=opts[i].getBoundingClientRect();
                                    opts[i].dispatchEvent(new MouseEvent("click",{{bubbles:true,cancelable:true,clientX:r.left+r.width/2,clientY:r.top+r.height/2}}));
                                    return "clicked:" + (opts[i].innerText||"").trim().substring(0,40);
                                }}
                            }}
                            return "no-valid-opts";
                        }}''')

                    for _sterm in ["KIIT", "Kalinga", "IIT", "Manipal", "National Institute", "India"]:
                        _opts_data = _wd_typeahead_search("formField-school", _sterm)
                        # Filter out empty-state items ("No Items.", "No results", etc.)
                        _real_opts = [o for o in _opts_data if o["text"].lower().rstrip(".") not in {t.rstrip(".") for t in _SKIP_OPT_TEXTS} and len(o["text"].strip()) > 2]
                        print(f"  [edu-school-opts] term={_sterm!r} total={len(_opts_data)} valid={len(_real_opts)} sample={[o['text'][:25] for o in _real_opts[:3]]}")
                        if _real_opts:
                            # Prefer KIIT/Kalinga/Bhubaneswar match, else pick first valid
                            _kw_pref = ["kiit", "kalinga", "bhubaneswar", "iit"]
                            _picked_text = None
                            for _od in _real_opts:
                                for _k in _kw_pref:
                                    if _k in _od["text"].lower():
                                        _picked_text = _od["text"][:30]
                                        break
                                if _picked_text:
                                    break
                            if not _picked_text:
                                _picked_text = _real_opts[0]["text"][:30]

                            _click_res = _pick_first_valid_option()
                            print(f"  [edu-school-pick] target={_picked_text!r} → {_click_res}")
                            if "clicked" in _click_res:
                                _edu_school_ok = True
                                break
                        # Diagnostic screenshot after first search
                        if _sterm == "KIIT":
                            try:
                                page.screenshot(path=f"/tmp/wd_school_search_{tenant}.png")
                            except Exception:
                                pass

                    if not _edu_school_ok:
                        # Fallback: browse widget - clear school input (resets dropdown to show "Partial List"),
                        # navigate into it, pick any university from sub-list
                        try:
                            # Close any open dropdown first (FoS may be open), then focus school
                            page.keyboard.press("Escape")
                            time.sleep(0.5)
                            # Clear the school search input to reset dropdown to browse mode
                            _br_clear = page.evaluate('''() => {
                                var cont = document.querySelector('[data-automation-id="formField-school"]');
                                if (!cont) return "no-cont";
                                // Find the search input inside the field
                                var inp = cont.querySelector('input[placeholder="Search"]')
                                       || Array.from(cont.querySelectorAll("input"))
                                           .filter(function(i){var r=i.getBoundingClientRect();return r.width>0&&r.height>0&&i.type!=="hidden";})[0];
                                if (inp) {
                                    inp.focus(); inp.click();
                                    try {
                                        var setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype,"value").set;
                                        setter.call(inp,"");
                                    } catch(e) { inp.value=""; }
                                    inp.dispatchEvent(new Event("focus",{bubbles:true}));
                                    inp.dispatchEvent(new Event("input",{bubbles:true}));
                                    inp.dispatchEvent(new Event("change",{bubbles:true}));
                                    inp.dispatchEvent(new KeyboardEvent("keydown",{bubbles:true,key:"Backspace",keyCode:8}));
                                    return "cleared-input";
                                }
                                // No input found - click the container to open
                                cont.click();
                                return "clicked-container";
                            }''')
                            time.sleep(2.0)
                            _br_partial = page.evaluate('''() => {
                                var opts = Array.from(document.querySelectorAll('[role="option"],[role="listitem"],li[tabindex]'))
                                    .filter(function(e){var r=e.getBoundingClientRect();return r.width>0&&r.height>0;});
                                var found = [];
                                for (var i=0;i<opts.length;i++) {
                                    found.push((opts[i].innerText||"").trim().substring(0,30));
                                }
                                for (var i=0;i<opts.length;i++) {
                                    var t=(opts[i].innerText||"").toLowerCase();
                                    if (t.includes("partial list")) {
                                        var r=opts[i].getBoundingClientRect();
                                        opts[i].dispatchEvent(new MouseEvent("click",{bubbles:true,cancelable:true,clientX:r.left+r.width/2,clientY:r.top+r.height/2}));
                                        return "clicked-partial opts=" + JSON.stringify(found.slice(0,5));
                                    }
                                }
                                return "no-partial opts=" + JSON.stringify(found.slice(0,5));
                            }''')
                            time.sleep(3.0)
                            _br_nav = ["partial list (first 500 entries)", "partial list", "all"]
                            _br_pick = page.evaluate(f'''() => {{
                                var skip = {list(_SKIP_OPT_TEXTS)};
                                var nav = {_br_nav};
                                var opts = Array.from(document.querySelectorAll('[role="option"],[role="listitem"],li[tabindex]'))
                                    .filter(function(e){{var r=e.getBoundingClientRect();return r.width>0&&r.height>0;}});
                                for (var i=0;i<opts.length;i++) {{
                                    var t=(opts[i].innerText||"").trim().toLowerCase();
                                    if (!t||t.length<3) continue;
                                    var bad=false;
                                    for (var j=0;j<skip.length;j++) if (t===skip[j]||t===skip[j].replace(".","")) {{bad=true;break;}}
                                    for (var j=0;j<nav.length;j++) if (t.startsWith(nav[j])) {{bad=true;break;}}
                                    if (bad) continue;
                                    var r=opts[i].getBoundingClientRect();
                                    opts[i].dispatchEvent(new MouseEvent("click",{{bubbles:true,cancelable:true,clientX:r.left+r.width/2,clientY:r.top+r.height/2}}));
                                    return "clicked:" + (opts[i].innerText||"").trim().substring(0,40);
                                }}
                                // debug: return all visible opts
                                var all = opts.map(function(e){{return (e.innerText||"").trim().substring(0,20);}});
                                return "no-valid-opts all=" + JSON.stringify(all.slice(0,8));
                            }}''')
                            print(f"  [edu-school-browse] clear={_br_clear} partial={_br_partial} pick={_br_pick}")
                            if "clicked:" in str(_br_pick):
                                _edu_school_ok = True
                        except Exception as _br_ex:
                            print(f"  [edu-school-browse-err] {_br_ex}")

                    if not _edu_school_ok:
                        # School DB empty — delete the education entry to avoid blocking validation
                        # Strategy: walk up from the school field to find the entry's Delete button
                        page.keyboard.press("Escape")  # close any open dropdown first
                        time.sleep(0.3)
                        _edu_del = page.evaluate('''() => {
                            // Walk up from school field to find the education entry container with Delete
                            var schoolField = document.querySelector('[data-automation-id="formField-school"]');
                            if (schoolField) {
                                var par = schoolField.parentElement;
                                for (var d=0; d<12; d++) {
                                    if (!par || par === document.body) break;
                                    var btns = Array.from(par.querySelectorAll("button"));
                                    for (var j=0; j<btns.length; j++) {
                                        var bt = (btns[j].innerText||btns[j].textContent||"").trim();
                                        var r = btns[j].getBoundingClientRect();
                                        if (bt === "Delete" && r.width > 0) {
                                            btns[j].scrollIntoView({block:"center"});
                                            btns[j].click();
                                            return "deleted-via-school-ancestor";
                                        }
                                    }
                                    par = par.parentElement;
                                }
                                return "no-delete-in-school-ancestors";
                            }
                            return "no-school-field";
                        }''')
                        print(f"  [edu-del] {_edu_del}")
                        time.sleep(0.5)

                    # Degree type (Workday custom dropdown) — only if we still have an edu entry
                    if not _edu_school_ok:
                        pass  # edu entry deleted above; skip degree/FoS fill
                    for _deg_aid in ["formField-degreeType", "formField-degree", "formField-degreeReceived"] if _edu_school_ok else []:
                        _dr = _open_wd_select_and_pick(_deg_aid, ["bachelor", "b.tech", "b. tech", "b.e."])
                        if "no-cont" not in str(_dr):
                            print(f"  [edu-degree] {_dr}")
                            time.sleep(0.2)
                            break

                    # Field of study (optional) — only if edu entry still exists
                    for _fos_aid in ["formField-fieldOfStudy", "formField-major", "formField-discipline"] if _edu_school_ok else []:
                        try:
                            _fos_cont = page.locator(f'[data-automation-id="{_fos_aid}"]').first
                            if not _fos_cont.is_visible(timeout=1000):
                                continue
                            _fos_opts_data = _wd_typeahead_search(_fos_aid, "Computer Science", wait_s=2.0)
                            if _fos_opts_data:
                                _fos_pick = page.evaluate(f'''() => {{
                                    var sels = ['[role="option"]','[role="listitem"]','li[tabindex]','[data-automation-id*="listItem"]'];
                                    for (var si=0;si<sels.length;si++) {{
                                        var opts=Array.from(document.querySelectorAll(sels[si]))
                                            .filter(function(e){{var r=e.getBoundingClientRect();return r.width>0&&r.height>0;}});
                                        for (var i=0;i<opts.length;i++) {{
                                            var t=(opts[i].innerText||"").toLowerCase();
                                            if (t.includes("computer")||t.includes("software")||t.includes("engineering")) {{
                                                var r=opts[i].getBoundingClientRect();
                                                opts[i].dispatchEvent(new MouseEvent("click",{{bubbles:true,cancelable:true,clientX:r.left+r.width/2,clientY:r.top+r.height/2}}));
                                                return "picked:" + (opts[i].innerText||"").trim().substring(0,40);
                                            }}
                                        }}
                                    }}
                                    return "no-match";
                                }}''')
                                print(f"  [edu-fos] {_fos_pick}")
                            else:
                                print(f"  [edu-fos] No options found for FoS (optional, skip)")
                            break
                        except Exception:
                            pass

                    # Graduation year / end date
                    for _gd_sel in [
                        '[data-automation-id="graduationDate"] input',
                        '[data-automation-id="formField-graduationDate"] input',
                        '[data-automation-id="formField-endDate"] input',
                        '[data-automation-id="endDate"] input',
                    ]:
                        try:
                            _gd = page.locator(_gd_sel).first
                            if _gd.is_visible(timeout=1000):
                                _gd.fill("2020")
                                print(f"  [edu-grad] Filled 2020 via {_gd_sel}")
                                break
                        except Exception:
                            pass

                    time.sleep(0.5)
        except Exception as _edu_fill_ex:
            print(f"  [edu-fill-err] {_edu_fill_ex}")

        # Screenshot before submitting My Information to verify all fields filled
        try:
            page.screenshot(path=f"/tmp/wd_before_next_step7_{tenant}.png")
            print(f"  [step7-screenshot] /tmp/wd_before_next_step7_{tenant}.png")
        except Exception:
            pass
        try_next(page)
        settle(page, 3000)
        try:
            page.evaluate("window.scrollTo(0, 0)")
            time.sleep(0.3)
            page.screenshot(path=f"/tmp/wd_after_step7_{tenant}.png")
        except Exception:
            pass

        # ── Step 8: Walk through remaining steps ──────────────────────────────
        print("[STEP 8] Walking through form steps…")

        # Work experience
        try_next(page)
        settle(page, 2000)

        # Education — dump current fields + fill required School and Degree fields before advancing
        try:
            page.screenshot(path="/tmp/wd_edu_step_ms.png")
        except Exception:
            pass
        _edu_fields_dump = page.evaluate('''() => {
            return Array.from(document.querySelectorAll('[data-automation-id^="formField-"]'))
                .filter(function(e){var r=e.getBoundingClientRect();return r.width>0&&r.height>0;})
                .map(function(e){return {aid:e.dataset.automationId,text:(e.innerText||"").trim().substring(0,50)};})
        }''')
        print(f"  [edu-page-fields] {_edu_fields_dump[:15]}")
        try:
            # Check if education school field is visible - use browse widget to pick any university
            _edu_school_filled = False
            _s8_nav = ["partial list (first 500 entries)", "partial list", "all"]
            _s8_skip = ["no items.", "no results.", "no matches.", "no items found.", "no results found."]
            # Close any open dropdown first (FoS may still be open)
            page.keyboard.press("Escape")
            time.sleep(0.5)
            for _sch_aid in ["formField-school", "formField-educationalInstitution", "formField-schoolName"]:
                try:
                    _sch_cont = page.locator(f'[data-automation-id="{_sch_aid}"]').first
                    if not _sch_cont.is_visible(timeout=1500):
                        continue
                    # Step 1: Clear school input to reset dropdown from "No Items." to browse mode
                    _s8_open = page.evaluate(f'''() => {{
                        var cont = document.querySelector('[data-automation-id="{_sch_aid}"]');
                        if (!cont) return "no-cont";
                        var inp = cont.querySelector('input[placeholder="Search"]')
                               || Array.from(cont.querySelectorAll("input"))
                                   .filter(function(i){{var r=i.getBoundingClientRect();return r.width>0&&r.height>0&&i.type!=="hidden";}})[0];
                        if (inp) {{
                            inp.focus(); inp.click();
                            try {{
                                var setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype,"value").set;
                                setter.call(inp,"");
                            }} catch(e) {{ inp.value=""; }}
                            inp.dispatchEvent(new Event("focus",{{bubbles:true}}));
                            inp.dispatchEvent(new Event("input",{{bubbles:true}}));
                            inp.dispatchEvent(new Event("change",{{bubbles:true}}));
                            inp.dispatchEvent(new KeyboardEvent("keydown",{{bubbles:true,key:"Backspace",keyCode:8}}));
                            return "cleared-input";
                        }}
                        cont.click();
                        return "clicked-container";
                    }}''')
                    time.sleep(2.0)
                    # Step 2: Click "Partial List" to navigate into full university list
                    _s8_partial = page.evaluate('''() => {
                        var opts = Array.from(document.querySelectorAll('[role="option"],[role="listitem"],li[tabindex]'))
                            .filter(function(e){var r=e.getBoundingClientRect();return r.width>0&&r.height>0;});
                        var found = opts.map(function(e){return (e.innerText||"").trim().substring(0,25);});
                        for (var i=0;i<opts.length;i++) {
                            var t=(opts[i].innerText||"").toLowerCase();
                            if (t.includes("partial list")) {
                                var r=opts[i].getBoundingClientRect();
                                opts[i].dispatchEvent(new MouseEvent("click",{bubbles:true,cancelable:true,clientX:r.left+r.width/2,clientY:r.top+r.height/2}));
                                return "clicked-partial opts=" + JSON.stringify(found.slice(0,5));
                            }
                        }
                        return "no-partial opts=" + JSON.stringify(found.slice(0,5));
                    }''')
                    time.sleep(3.0)
                    # Step 3: Pick first real university from sub-list (skip navigation nodes)
                    _s8_pick = page.evaluate(f'''() => {{
                        var skip = {_s8_skip};
                        var nav = {_s8_nav};
                        var sels = ['[role="option"]','[role="listitem"]','li[tabindex]'];
                        for (var si=0;si<sels.length;si++) {{
                            var opts = Array.from(document.querySelectorAll(sels[si]))
                                .filter(function(e){{var r=e.getBoundingClientRect();return r.width>0&&r.height>0;}});
                            for (var i=0;i<opts.length;i++) {{
                                var t=(opts[i].innerText||"").trim().toLowerCase();
                                if (!t||t.length<3) continue;
                                var bad=false;
                                for (var j=0;j<skip.length;j++) if (t===skip[j]||t===skip[j].replace(".","")) {{bad=true;break;}}
                                for (var j=0;j<nav.length;j++) if (t.startsWith(nav[j])) {{bad=true;break;}}
                                if (bad) continue;
                                var r=opts[i].getBoundingClientRect();
                                opts[i].dispatchEvent(new MouseEvent("click",{{bubbles:true,cancelable:true,clientX:r.left+r.width/2,clientY:r.top+r.height/2}}));
                                return "clicked:" + (opts[i].innerText||"").trim().substring(0,40);
                            }}
                        }}
                        var all=Array.from(document.querySelectorAll('[role="option"],[role="listitem"]'))
                            .filter(function(e){{var r=e.getBoundingClientRect();return r.width>0&&r.height>0;}})
                            .map(function(e){{return (e.innerText||"").trim().substring(0,20);}});
                        return "no-valid-opts all=" + JSON.stringify(all.slice(0,8));
                    }}''')
                    print(f"  [edu-school-s8] aid={_sch_aid} open={_s8_open} partial={_s8_partial} pick={_s8_pick}")
                    if "clicked:" in str(_s8_pick):
                        _edu_school_filled = True
                        print(f"  [edu-school] Filled via {_sch_aid}")
                        break
                except Exception as _s8_e:
                    print(f"  [edu-school-s8-err] {_s8_e}")

            # Fill field of study / major
            for _fos_sel in [
                '[data-automation-id="fieldOfStudy"]',
                '[data-automation-id="formField-fieldOfStudy"] input',
                '[data-automation-id="formField-major"] input',
                'input[data-automation-id*="field" i][data-automation-id*="study" i]',
            ]:
                try:
                    _fos = page.locator(_fos_sel).first
                    if _fos.is_visible(timeout=1000):
                        _fos.fill("Computer Science")
                        print(f"  [edu-fos] Filled via {_fos_sel}")
                        break
                except Exception:
                    pass

            # Degree dropdown — find button trigger inside degree formField
            _edu_degree_done = False
            for _deg_cont in [
                '[data-automation-id="formField-degreeType"]',
                '[data-automation-id="formField-degree"]',
                '[data-automation-id="formField-degreeReceived"]',
            ]:
                try:
                    _deg_btn = page.locator(f'{_deg_cont} button').first
                    if not _deg_btn.is_visible(timeout=1000):
                        continue
                    # Use JS dispatchEvent (same pattern as state dropdown)
                    _deg_js = page.evaluate(f'''() => {{
                        var cont = document.querySelector('{_deg_cont}');
                        if (!cont) return "no-cont";
                        var btn = cont.querySelector('button');
                        if (!btn) return "no-btn";
                        var r = btn.getBoundingClientRect();
                        if (r.width === 0) return "zero-size";
                        var cx = r.left + r.width/2, cy = r.top + r.height/2;
                        var o = {{bubbles:true,cancelable:true,clientX:cx,clientY:cy,view:window}};
                        btn.dispatchEvent(new MouseEvent('mouseover',o));
                        btn.dispatchEvent(new PointerEvent('pointerdown',{{bubbles:true,cancelable:true,clientX:cx,clientY:cy,pointerId:1}}));
                        btn.dispatchEvent(new MouseEvent('mousedown',o));
                        btn.dispatchEvent(new PointerEvent('pointerup',{{bubbles:true,cancelable:true,clientX:cx,clientY:cy,pointerId:1}}));
                        btn.dispatchEvent(new MouseEvent('mouseup',o));
                        btn.dispatchEvent(new MouseEvent('click',o));
                        return "dispatched at " + Math.round(cx) + "," + Math.round(cy);
                    }}''')
                    print(f"  [edu-degree-open] {_deg_js} via {_deg_cont}")
                    time.sleep(1.2)
                    # Select "Bachelor" option
                    _deg_select = page.evaluate('''() => {
                        var opts = Array.from(document.querySelectorAll('[role="option"]'))
                            .filter(function(e) {
                                var r = e.getBoundingClientRect();
                                var t = (e.innerText||"").toLowerCase();
                                return r.width > 0 && r.height > 0
                                    && (t.includes("bachelor") || t.includes("b.tech") || t.includes("b.e."));
                            });
                        if (!opts.length) {
                            // Return all visible options for debug
                            var all = Array.from(document.querySelectorAll('[role="option"]'))
                                .filter(function(e){var r=e.getBoundingClientRect();return r.width>0&&r.height>0;})
                                .map(function(e){return (e.innerText||"").trim().substring(0,40);});
                            return "no-match all=" + JSON.stringify(all.slice(0,8));
                        }
                        var el = opts[0];
                        var r = el.getBoundingClientRect();
                        var cx = r.left+r.width/2, cy = r.top+r.height/2;
                        var o = {bubbles:true,cancelable:true,clientX:cx,clientY:cy};
                        el.dispatchEvent(new MouseEvent("click",o));
                        return "selected:" + (el.innerText||"").trim().substring(0,40);
                    }''')
                    print(f"  [edu-degree-select] {_deg_select}")
                    if "selected:" in str(_deg_select):
                        _edu_degree_done = True
                    break
                except Exception as _deg_ex:
                    print(f"  [edu-degree-err] {_deg_ex}")

            # Graduation year / end date
            for _gd_sel in [
                '[data-automation-id="graduationDate"]',
                '[data-automation-id="formField-graduationDate"] input',
                '[data-automation-id="formField-endDate"] input',
                '[data-automation-id="endDate"]',
            ]:
                try:
                    _gd = page.locator(_gd_sel).first
                    if _gd.is_visible(timeout=1000):
                        _gd.fill("05/2020")
                        print(f"  [edu-grad] Filled graduation date via {_gd_sel}")
                        break
                except Exception:
                    pass

            if _edu_school_filled:
                print(f"  [edu] Education fields filled. degree_done={_edu_degree_done}")
            else:
                print("  [edu] No school field found (may be auto-filled or different step)")
        except Exception as _edu_err:
            print(f"  [edu-error] {_edu_err}")
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
        q_yes_kw = (
            "legally authorized", "authorized to work", "eligible to work",
            "right to work", "citizen or permanent", "sponsorship not required",
            "consent to receive", "sms", "whatsapp", "follow up communication",
            "background check", "willing to submit a background",
            "provide documentation", "documentation establishing",
            "able to work on a daily basis", "work in the country",
            "identity and right",
        )
        # Questions that should ALWAYS be answered No regardless of yes_kw matches
        q_no_kw = (
            "related to, closely connected", "referred by a client", "vendor/potential vendor",
            "vendor of morgan stanley", "client of morgan stanley",
            "government official", "family member", "spouse", "partner",
            "require morgan stanley to sponsor", "require a work visa",
            "currently employed as", "previously employed as",
            "currently employed or previously", "board member", "contingent worker",
        )
        for _q_iter in range(12):   # up to 12 questionnaire pages
            # Close any lingering open dropdown from previous iteration
            page.keyboard.press("Escape")
            time.sleep(0.2)
            # Scroll to find all questions
            page.evaluate("window.scrollTo(0, 0)")
            time.sleep(0.2)
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            time.sleep(0.3)
            page.evaluate("window.scrollTo(0, 0)")
            time.sleep(0.2)

            answered = False
            _q_iter_url = page.url
            print(f"  [q-iter-{_q_iter}] url={_q_iter_url.split('/')[-1][:50]}")

            # Handle radio buttons — try both radioBtn and radioButton automation-id patterns
            _radio_count = 0
            for _rbtn_sel in ['[data-automation-id*="radioBtn"]:visible', '[data-automation-id*="radioButton"]:visible',
                              'input[type="radio"]:visible']:
                for radio in page.locator(_rbtn_sel).all():
                    try:
                        label_text = ""
                        try:
                            label_text = radio.inner_text(timeout=500).lower()
                        except Exception:
                            pass
                        if not label_text:
                            try:
                                # For input[type="radio"], find associated label
                                label_text = radio.evaluate(
                                    "el => { var id=el.id; if(id){var l=document.querySelector('label[for=\"'+id+'\"]'); if(l) return l.innerText.toLowerCase();} "
                                    "var p=el.parentElement; return p? p.innerText.toLowerCase() : ''; }"
                                )
                            except Exception:
                                pass
                        question_text = ""
                        try:
                            question_text = radio.evaluate(
                                "el => { let p = el.closest('[data-automation-id*=\"formField\"],[data-automation-id*=\"rmField\"]'); if(p) return p.innerText; return ''; }"
                            ).lower()
                        except Exception:
                            pass
                        full_text = label_text + " " + question_text
                        _is_no_r = any(kw in full_text for kw in q_no_kw)
                        _is_yes_r = any(kw in full_text for kw in q_yes_kw)
                        want_yes = (not _is_no_r) and _is_yes_r
                        if want_yes and ("yes" in label_text or "true" in label_text):
                            radio.evaluate("el => el.click()")
                            answered = True
                            _radio_count += 1
                            time.sleep(0.2)
                        elif not want_yes and ("no" in label_text or "false" in label_text):
                            radio.evaluate("el => el.click()")
                            answered = True
                            _radio_count += 1
                            time.sleep(0.2)
                    except Exception:
                        pass
            if _radio_count:
                print(f"  [q-radio] clicked {_radio_count} radio button(s)")

            # Handle Workday custom select dropdowns (Morgan Stanley Application Questions)
            _wd_q_fields = page.evaluate('''() => {
                var result = [];
                var seen = {};
                Array.from(document.querySelectorAll('[data-automation-id^="formField-"]'))
                    .filter(function(f) {
                        var r = f.getBoundingClientRect();
                        if (r.width === 0 || r.height === 0) return false;
                        var btn = f.querySelector("button");
                        if (!btn) return false;
                        var btext = (btn.innerText || btn.textContent || "").trim().toLowerCase();
                        // Exclude already-selected multi-select fields ("1 item selected, X")
                        if (/^[1-9][0-9]* item/.test(btext)) return false;
                        return btext.includes("select") || btext === "";
                    })
                    .forEach(function(f) {
                        var aid = f.dataset.automationId;
                        if (!aid || aid === "formField-" || seen[aid]) return;
                        seen[aid] = true;
                        // Extract question text using PRECEDING SIBLING approach
                        // (avoids capturing all questions' text by walking up to a shared ancestor)
                        var qtext = "";
                        var par = f.parentElement;
                        if (par) {
                            // Strategy 1: look at preceding siblings for the question label/text
                            var sibs = Array.from(par.children);
                            var fIdx = -1;
                            for (var k=0; k<sibs.length; k++) {
                                if (sibs[k] === f || sibs[k].contains(f)) { fIdx = k; break; }
                            }
                            if (fIdx > 0) {
                                for (var k = fIdx - 1; k >= Math.max(0, fIdx - 4); k--) {
                                    var sibText = (sibs[k].innerText || "").trim();
                                    if (sibText.length > 15 && sibText.length < 600) {
                                        qtext = sibText;
                                        break;
                                    }
                                }
                            }
                            // Strategy 2: if no preceding sibling, use parent text only if small
                            if (!qtext) {
                                var pt = (par.innerText || "").trim();
                                if (pt.length < 600) qtext = pt;
                            }
                        }
                        // Strategy 3: walk up max 3 levels (not 8), stop if container is large
                        if (!qtext) {
                            var p = par ? par.parentElement : null;
                            for (var i = 0; i < 3 && p && p !== document.body; i++, p=p.parentElement) {
                                var t = (p.innerText || "").trim();
                                if (t.length > 20 && t.length < 600) { qtext = t; break; }
                            }
                        }
                        result.push({aid: aid, qtext: qtext.substring(0,300).toLowerCase()});
                    });
                return result;
            }''')

            for _wdq in _wd_q_fields:
                _q_aid = _wdq.get("aid", "")
                _q_text_lower = _wdq.get("qtext", "")
                if not _q_aid:
                    continue
                _is_no = any(kw in _q_text_lower for kw in q_no_kw)
                _is_yes = any(kw in _q_text_lower for kw in q_yes_kw)
                # No keywords take priority (conservative: only say Yes if explicitly recognized)
                _ans = "No" if _is_no else ("Yes" if _is_yes else "No")

                # Scroll field into view
                page.evaluate(f'() => {{ var f = document.querySelector(\'[data-automation-id="{_q_aid}"]\'); if(f) f.scrollIntoView({{block:"center"}}); }}')
                time.sleep(0.2)

                # Open dropdown via dispatchEvent
                _open_r = page.evaluate(f'''() => {{
                    var c = document.querySelector('[data-automation-id="{_q_aid}"]');
                    if (!c) return "no-cont";
                    var b = c.querySelector("button") || c;
                    var r = b.getBoundingClientRect();
                    if (!r.width) return "zero";
                    var cx=r.left+r.width/2, cy=r.top+r.height/2;
                    var o={{bubbles:true,cancelable:true,clientX:cx,clientY:cy,view:window}};
                    b.dispatchEvent(new MouseEvent("mouseover",o));
                    b.dispatchEvent(new PointerEvent("pointerdown",{{bubbles:true,cancelable:true,clientX:cx,clientY:cy,pointerId:1}}));
                    b.dispatchEvent(new MouseEvent("mousedown",o));
                    b.dispatchEvent(new PointerEvent("pointerup",{{bubbles:true,cancelable:true,clientX:cx,clientY:cy,pointerId:1}}));
                    b.dispatchEvent(new MouseEvent("mouseup",o));
                    b.dispatchEvent(new MouseEvent("click",o));
                    return "opened";
                }}''')
                if "opened" not in str(_open_r):
                    print(f"  [q-wd-sel-err] {_q_aid[-12:]}: {_open_r}")
                    continue
                time.sleep(0.8)

                _target = _ans.lower()
                # For degree/education fields in questionnaire, target specific degree keywords
                _edu_field_kw = ("degree", "major", "fieldstudy", "fieldofstudy", "discipline", "subject")
                _is_edu_q = any(kw in _q_aid.lower() for kw in _edu_field_kw)
                if _is_edu_q:
                    _target = "bachelor"  # prefer bachelor for edu fields
                _pick_r = page.evaluate(f'''() => {{
                    var target = "{_target}";
                    var opts = Array.from(document.querySelectorAll('[role="option"]'))
                        .filter(function(e){{var r=e.getBoundingClientRect();return r.width>0&&r.height>0;}});
                    if (!opts.length) return "no-opts";
                    for (var i=0;i<opts.length;i++) {{
                        var t=(opts[i].innerText||"").trim().toLowerCase();
                        if (t===target||t.startsWith(target)||t.includes(target)) {{
                            var r=opts[i].getBoundingClientRect();
                            opts[i].dispatchEvent(new MouseEvent("click",{{bubbles:true,cancelable:true,clientX:r.left+r.width/2,clientY:r.top+r.height/2}}));
                            return "picked:" + (opts[i].innerText||"").trim();
                        }}
                    }}
                    // Fallback: first non-placeholder
                    for (var i=0;i<opts.length;i++) {{
                        var t=(opts[i].innerText||"").trim().toLowerCase();
                        if (t && t!=="select one" && !t.startsWith("select")) {{
                            var r=opts[i].getBoundingClientRect();
                            opts[i].dispatchEvent(new MouseEvent("click",{{bubbles:true,cancelable:true,clientX:r.left+r.width/2,clientY:r.top+r.height/2}}));
                            return "picked-first:" + (opts[i].innerText||"").trim();
                        }}
                    }}
                    return "no-match opts=" + opts.slice(0,4).map(function(e){{return e.innerText.trim();}}).join("|");
                }}''')
                # After JS click on option, also try Playwright-level click to ensure React registers it
                if "picked" in _pick_r:
                    _picked_text = _pick_r.split(":", 1)[1][:40] if ":" in _pick_r else ""
                    try:
                        _pl_opts = page.locator('[role="option"]:visible').all()
                        for _plo in _pl_opts[:6]:
                            _pt = (_plo.inner_text(timeout=200) or "").strip()
                            if _pt.lower() == _picked_text.lower() or _pt.lower().startswith(_picked_text.lower()[:10]):
                                _plo.click(timeout=1000)
                                break
                    except Exception:
                        pass
                # Close dropdown after selection
                page.keyboard.press("Escape")
                time.sleep(0.2)
                print(f"  [q-wd-select] {_q_aid[-14:]} ans={_ans} → {_pick_r}")
                if "picked" in _pick_r:
                    answered = True
                time.sleep(0.25)

            # Handle native <select> dropdowns
            for _sel_loc in page.locator('select:visible').all():
                try:
                    _cur_val = _sel_loc.evaluate("el => el.value")
                    if _cur_val and _cur_val.strip() and _cur_val.lower() not in ("", "select one", "select_one", "none", "null"):
                        continue
                    _q_text = _sel_loc.evaluate(
                        "el => { var p = el.parentElement; while (p && !p.innerText.includes('?') && p !== document.body) p = p.parentElement; return p ? p.innerText.substring(0, 300) : ''; }"
                    ).lower()
                    _ans = "Yes" if any(kw in _q_text for kw in q_yes_kw) else "No"
                    _sel_res = _sel_loc.evaluate(f'''el => {{
                        var target = "{_ans}".toLowerCase();
                        var opts = Array.from(el.options);
                        for (var i = 0; i < opts.length; i++) {{
                            var t = opts[i].text.trim().toLowerCase();
                            if (t === target || t.startsWith(target)) {{
                                el.selectedIndex = i;
                                el.dispatchEvent(new Event("change", {{bubbles:true}}));
                                return "selected:" + opts[i].text.trim();
                            }}
                        }}
                        return "no-match opts=" + opts.map(function(o){{return o.text.trim();}}).join("|");
                    }}''')
                    print(f"  [q-native-sel] {_sel_res} ans={_ans} q={_q_text[:60]}")
                    if "selected:" in _sel_res:
                        answered = True
                    time.sleep(0.15)
                except Exception as _sq_ex:
                    print(f"  [q-select-err] {_sq_ex}")

            # Fill any required follow-up text fields that appear after Yes answers
            try:
                _txt_fills = page.evaluate('''(profileLinkedin) => {
                    var filled = 0;
                    Array.from(document.querySelectorAll('[data-automation-id^="formField-"]'))
                        .filter(function(f) {
                            var r = f.getBoundingClientRect();
                            if (r.width===0||r.height===0) return false;
                            var inp = f.querySelector('textarea,input[type="text"]');
                            if (!inp) return false;
                            var r2 = inp.getBoundingClientRect();
                            return r2.width>0 && r2.height>0 && !inp.value;
                        })
                        .forEach(function(f) {
                            var inp = f.querySelector('textarea,input[type="text"]');
                            if (!inp) return;
                            var faid = (f.dataset.automationId||"").toLowerCase();
                            var flabel = (f.innerText||"").toLowerCase();
                            // Choose fill value based on field type
                            var fillVal = "N/A";
                            if (faid.includes("linkedin") || faid.includes("social") ||
                                flabel.includes("linkedin") || flabel.includes("profile site")) {
                                fillVal = profileLinkedin;
                            } else if (faid.includes("url") || faid.includes("website") || faid.includes("portfolio") ||
                                       flabel.includes("url") || flabel.includes("website") || flabel.includes("portfolio")) {
                                fillVal = profileLinkedin;
                            } else if (faid.includes("school") || faid.includes("institution") || faid.includes("university") ||
                                flabel.includes("school") || flabel.includes("institution") || flabel.includes("university")) {
                                fillVal = "KIIT University";
                            } else if (faid.includes("gpa") || faid.includes("grade") || faid.includes("cgpa")) {
                                fillVal = "8.0";
                            } else if (faid.includes("year") || faid.includes("graduation") || flabel.includes("graduation year")) {
                                fillVal = "2019";
                            } else if (faid.includes("city") || flabel.includes("city")) {
                                fillVal = "Bengaluru";
                            }
                            inp.focus();
                            try {
                                var s = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype,"value")
                                         ||Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype,"value");
                                if (s && s.set) s.set.call(inp, fillVal);
                                else inp.value = fillVal;
                            } catch(e) { inp.value = fillVal; }
                            inp.dispatchEvent(new Event("input",{bubbles:true}));
                            inp.dispatchEvent(new Event("change",{bubbles:true}));
                            inp.blur();
                            filled++;
                        });
                    return filled;
                }''', PROFILE["linkedin"])
                if _txt_fills:
                    print(f"  [q-textfill] filled {_txt_fills} required text field(s)")
                    answered = True
            except Exception as _tf_ex:
                pass

            # Handle multi-select checkbox groups — e.g. "Have you ever worked at Adobe in the following capacity?"
            # → check "I have not worked for X in the past" option
            try:
                _chk_filled = page.evaluate('''() => {
                    var filled = 0;
                    // Find all visible required checkbox groups
                    var groups = Array.from(document.querySelectorAll(
                        '[data-automation-id^="formField-"],[data-automation-id^="checkboxGroup"]'
                    )).filter(function(f) {
                        var r = f.getBoundingClientRect();
                        return r.width > 0 && r.height > 0 && f.querySelectorAll('input[type="checkbox"]').length > 1;
                    });
                    groups.forEach(function(g) {
                        var checkboxes = Array.from(g.querySelectorAll('input[type="checkbox"]'));
                        var anyChecked = checkboxes.some(function(c) { return c.checked; });
                        if (anyChecked) return;  // already answered
                        var labelText = (g.innerText || "").toLowerCase();
                        // For "worked at X" questions, find "have not worked" or "I have not worked" option
                        var notWorkedCb = null;
                        checkboxes.forEach(function(cb) {
                            var cbLabel = "";
                            var lbl = document.querySelector('label[for="'+cb.id+'"]');
                            if (lbl) cbLabel = lbl.innerText.toLowerCase();
                            else { var p = cb.parentElement; if (p) cbLabel = p.innerText.toLowerCase(); }
                            if (cbLabel.includes("have not worked") || cbLabel.includes("not worked") ||
                                cbLabel.includes("i have not") || cbLabel.includes("none of the above") ||
                                cbLabel.includes("not previously")) {
                                notWorkedCb = cb;
                            }
                        });
                        if (notWorkedCb) {
                            notWorkedCb.checked = true;
                            notWorkedCb.dispatchEvent(new Event("change", {bubbles: true}));
                            notWorkedCb.dispatchEvent(new Event("click", {bubbles: true}));
                            filled++;
                        }
                    });
                    return filled;
                }''')
                if _chk_filled:
                    print(f"  [q-checkbox] ticked {_chk_filled} 'not worked' checkbox(es)")
                    answered = True
            except Exception:
                pass

            # Handle EEO custom-select dropdowns (gender/disability/veteran/military)
            # Pick "Prefer not to say" / "Decline to state" for these fields
            _eeo_kw = ["gender", "disability", "veteran", "military service", "ethnic", "race",
                       "sexual orientation", "rmField-gender", "rmField-disability", "rmField-veteran"]
            _eeo_q_fields = page.evaluate('''() => {
                var result = [];
                var seen = {};
                Array.from(document.querySelectorAll('[data-automation-id^="formField-"],[data-automation-id^="rmField-"]'))
                    .filter(function(f) {
                        var r = f.getBoundingClientRect();
                        if (r.width===0||r.height===0) return false;
                        var btn = f.querySelector("button");
                        if (!btn) return false;
                        var btext = (btn.innerText||btn.textContent||"").trim().toLowerCase();
                        // Must be unanswered (shows select/empty) OR already showing a value we should change
                        return btext.includes("select") || btext === "" || btext.includes("female") || btext.includes("male");
                    })
                    .forEach(function(f) {
                        var aid = f.dataset.automationId;
                        if (!aid || seen[aid]) return;
                        seen[aid] = true;
                        result.push({aid: aid, label: (f.innerText||"").trim().substring(0,100).toLowerCase()});
                    });
                return result;
            }''')
            for _eeoq in _eeo_q_fields:
                _eeo_aid = _eeoq.get("aid","")
                _eeo_lbl = _eeoq.get("label","")
                _is_eeo = any(kw in _eeo_aid.lower() or kw in _eeo_lbl for kw in _eeo_kw)
                if not _is_eeo:
                    continue
                # Open dropdown
                _eeo_open = page.evaluate(f'''() => {{
                    var c = document.querySelector('[data-automation-id="{_eeo_aid}"]');
                    if (!c) return "no-cont";
                    var b = c.querySelector("button") || c;
                    var r = b.getBoundingClientRect();
                    if (!r.width) return "zero";
                    var cx=r.left+r.width/2, cy=r.top+r.height/2;
                    var o={{bubbles:true,cancelable:true,clientX:cx,clientY:cy}};
                    b.dispatchEvent(new MouseEvent("click",o));
                    return "opened";
                }}''')
                time.sleep(0.8)
                # Pick "Prefer not to say" or "Decline" option
                _eeo_pick = page.evaluate('''() => {
                    var pref = ["prefer not to say","prefer not to disclose","decline to state",
                                "choose not to disclose","i do not wish to answer","not disclosed",
                                "prefer not","decline","i choose not","choose not to answer"];
                    var opts = Array.from(document.querySelectorAll('[role="option"]'))
                        .filter(function(e){var r=e.getBoundingClientRect();return r.width>0&&r.height>0;});
                    for (var i=0;i<opts.length;i++) {
                        var t=(opts[i].innerText||"").trim().toLowerCase();
                        for (var j=0;j<pref.length;j++) {
                            if (t.includes(pref[j])) {
                                var r=opts[i].getBoundingClientRect();
                                opts[i].dispatchEvent(new MouseEvent("click",{bubbles:true,cancelable:true,clientX:r.left+r.width/2,clientY:r.top+r.height/2}));
                                return "eeo-picked:" + (opts[i].innerText||"").trim().substring(0,40);
                            }
                        }
                    }
                    return "eeo-no-prefer-opt";
                }''')
                print(f"  [q-eeo] {_eeo_aid} {_eeo_pick}")
                if "eeo-picked" in _eeo_pick:
                    answered = True
                time.sleep(0.25)

            # Handle agreement/T&C checkboxes (must be checked before advancing)
            _chk_result = page.evaluate('''() => {
                var checked = 0;
                var chks = Array.from(document.querySelectorAll('input[type="checkbox"]'))
                    .filter(function(c){var r=c.getBoundingClientRect();return r.width>0&&r.height>0&&!c.checked;});
                chks.forEach(function(c) {
                    var label = "";
                    if (c.id) {
                        var lbl = document.querySelector('label[for="' + c.id + '"]');
                        if (lbl) label = (lbl.innerText||"").toLowerCase();
                    }
                    if (!label) {
                        var p = c.parentElement;
                        if (p) label = (p.innerText||p.textContent||"").toLowerCase();
                    }
                    if (label.includes("read and consent") || label.includes("terms and condition") ||
                        label.includes("i accept") || label.includes("i agree") ||
                        label.includes("acknowledge") || label.includes("privacy")) {
                        c.click();
                        checked++;
                    }
                });
                return checked;
            }''')
            if _chk_result:
                print(f"  [q-checkbox] checked {_chk_result} agreement checkbox(es)")
                answered = True
                time.sleep(0.3)

            # Source question
            try:
                page.locator('label:has-text("Word of Mouth")').first.click(timeout=1000)
            except Exception:
                pass

            # Always advance — answered tracking is for logging only
            if not try_next(page):
                break
            settle(page, 2000)

            # Stop if we've reached Review/Confirmation/Success page
            try:
                _cur_url = page.url
                _has_review = page.locator('[data-automation-id="pageHeaderTitle"]').filter(has_text="Review").count() > 0
                _has_submit = page.locator('button:has-text("Submit")').count() > 0
                # Also check page body text for review/summary indicators
                _body_text = ""
                try:
                    _body_text = page.locator("body").inner_text(timeout=1000).lower()
                except Exception:
                    pass
                _review_kw = ("review your application", "review and submit", "application summary",
                              "completed/application", "jobTasks/completed", "thank you",
                              "application submitted", "successfully submitted")
                _body_has_review = any(k in _body_text for k in _review_kw)
                _url_has_done = any(k in _cur_url.lower() for k in ("review", "completed", "thank", "confirm", "submitted"))
                if _has_review or _has_submit or _body_has_review or _url_has_done:
                    print(f"  [q] Reached Review page at iter {_q_iter}")
                    break
                # If no fields found for 2 consecutive iters and no new content, assume review page
                if _q_iter >= 2 and not answered:
                    _vis_fields = page.evaluate('''() => {
                        return Array.from(document.querySelectorAll(
                            '[data-automation-id^="formField-"],[data-automation-id^="rmField-"]'
                        )).filter(function(f){var r=f.getBoundingClientRect();return r.width>0&&r.height>0;}).length;
                    }''')
                    if _vis_fields == 0:
                        print(f"  [q] No visible fields at iter {_q_iter} — assuming review page, breaking")
                        break
            except Exception:
                pass

        # ── Step 10: Try auto-submit, then wait for confirmation ─────────────
        settle(page, 3000)  # Let page fully settle after questionnaire
        try:
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        except Exception:
            pass
        time.sleep(1)

        # Attempt auto-submit
        # On Review page, Workday shows the submit button — automation-id varies by instance.
        # MS External uses pageFooterNextButton on the Review step too.
        submitted = False
        try:
            page.screenshot(path=f"/tmp/wd_before_submit_{tenant}.png")
            print(f"  [screenshot] before-submit at /tmp/wd_before_submit_{tenant}.png")
        except Exception:
            print(f"  [screenshot-warn] Could not take before-submit screenshot")
        for sel in [
            'button:has-text("Submit")',
            'button:has-text("Submit Application")',
            '[data-automation-id="wd-CommandButton_uic_submitButton"]',
            '[data-automation-id="submitButton"]',
            '[data-automation-id="bottom-navigation-next-button"]',
            '[data-automation-id="pageFooterNextButton"]',
            # button[type="submit"] is last resort — avoid clicking account/login forms
        ]:
            try:
                btn = page.locator(sel).first
                btn.wait_for(state="visible", timeout=3000)
                btn.scroll_into_view_if_needed()
                btn.evaluate("el => el.click()")
                settle(page, 5000)
                submitted = True
                print(f"  Auto-submit clicked: {sel}")
                break
            except Exception:
                pass

        if not submitted:
            print("[WARN] No submit button found — skipping (batch mode)")

        # Workday SPA keeps the same URL on submission — check page text instead
        # Wait up to 90s for a success/thank-you indicator on the page
        _success = False
        _success_keywords = (
            "thank you", "application submitted", "application has been submitted",
            "successfully submitted", "we received your application",
            "your application is complete", "application complete",
            "you've already applied", "you have already applied",
            "already applied for this job", "already submitted",
        )
        for _sw in range(30):  # poll up to 30s
            try:
                _body = page.locator("body").inner_text(timeout=2000).lower()
                if any(k in _body for k in _success_keywords):
                    _success = True
                    break
            except Exception:
                pass
            # Also check URL
            if any(k in page.url.lower() for k in ("thank", "confirm", "submitted", "success")):
                _success = True
                break
            time.sleep(1)

        final_url = page.url
        print(f"\n[RESULT] {final_url}")
        if _success:
            print("[SUCCESS] Application submitted!")
            page.close()
            return True
        else:
            page.screenshot(path=f"/tmp/wd_apply_{tenant}.png")
            print(f"[WARN] Not confirmed — screenshot at /tmp/wd_apply_{tenant}.png")
            page.close()
            return _fail(f"not_confirmed — screenshot: /tmp/wd_apply_{tenant}.png")

    except Exception as exc:
        err_msg = str(exc)
        print(f"[ERROR] Unexpected error: {err_msg}")
        try:
            page.screenshot(path=f"/tmp/wd_error_{tenant}.png")
            err_msg += f" — screenshot: /tmp/wd_error_{tenant}.png"
        except Exception:
            pass
        page.close()
        return _fail(f"exception: {err_msg[:300]}")


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Auto-apply to Workday jobs from job store")
    parser.add_argument("--min-score", type=int,  default=7,
                        help="Minimum fit_score to consider (default: 7)")
    parser.add_argument("--job-url",               help="Apply to a single specific Workday job URL")
    parser.add_argument("--company",               help="Filter to jobs from this company")
    parser.add_argument("--limit",         type=int, default=50, help="Max jobs to apply per run (default: 50)")
    parser.add_argument("--min-companies", type=int, default=10, help="Min distinct companies in selection (default: 10)")
    parser.add_argument("--dry-run", action="store_true", help="List candidates only, no browser")
    parser.add_argument("--profile", default=os.environ.get("CANDIDATE_PROFILE_SLUG", ""), help="Profile slug")
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
        _INDIA_KEYWORDS = (
            "india", "bangalore", "bengaluru", "hyderabad", "chennai",
            "pune", "mumbai", "delhi", "noida", "gurugram", "gurgaon",
            "kolkata", "india remote", "remote, india", "remote - india",
        )
        _BLOCKED_GEO = (
            "united states", "us-remote", "us remote", "remote - us",
            "remote, us", "remote - usa", "remote, usa", "remote - california",
            "canada", "united kingdom", "uk remote", "remote - uk",
            "poland", "netherlands", "germany", "france", "spain",
            "italy", "portugal", "ireland", "switzerland", "singapore",
            "australia", "brazil", "mexico", "amsterdam",
        )

        def _is_india_or_remote_wd(job: dict) -> bool:
            loc = (job.get("location") or "").lower()
            if not loc:
                return True
            if any(kw in loc for kw in _INDIA_KEYWORDS):
                return True
            if any(kw in loc for kw in _BLOCKED_GEO):
                return False
            if any(kw in loc for kw in ("remote", "worldwide", "global", "anywhere")):
                return True
            return False

        all_jobs = job_store.all_jobs()
        jobs = [
            j for j in all_jobs
            if j.get("ats_type") == "workday"
            and j.get("fit_score", 0) >= args.min_score
            and not j.get("applied_at")
            and not j.get("removed")
            and _is_india_or_remote_wd(j)
        ]
        if args.company:
            needle = args.company.lower()
            jobs = [j for j in jobs if needle in j.get("company", "").lower()]
        # Sort by score descending
        jobs.sort(key=lambda j: j.get("fit_score", 0), reverse=True)

        # ── Dedup: at most 1 job per (company, base-title) ──────────────────
        # Prevents applying to multiple "Senior Software Engineer" variants at
        # the same company (same role with different suffixes/IDs).
        def _base_title_wd(t: str) -> str:
            t = re.split(r"\s*[-–,(/]", t)[0].strip().lower()
            return re.sub(r"\s+", " ", t)

        _already_applied_pairs = {
            (_base_title_wd(j.get("title", "")), j.get("company", "").lower())
            for j in all_jobs
            if j.get("applied_at")
        }

        deduped: list = []
        seen_pairs: set = set(_already_applied_pairs)
        for j in jobs:
            pair = (_base_title_wd(j.get("title", "")), j.get("company", "").lower())
            if pair in seen_pairs:
                print(
                    f"  [dedup] Skipping '{j.get('title')}' @ {j.get('company')} "
                    f"(already applied to same role)"
                )
                continue
            seen_pairs.add(pair)
            deduped.append(j)
        jobs = deduped

        limit = args.limit or len(jobs)
        top = jobs[:limit]

        # ── Diversity: ensure at least min_companies distinct companies ───
        min_co = args.min_companies
        if min_co and not args.company:
            remaining = jobs[limit:]
            best_by_co: dict = {}
            for j in remaining:
                co = j.get("company", "").lower()
                if co not in best_by_co:
                    best_by_co[co] = j

            distinct = {j.get("company", "").lower() for j in top}
            missing_cos = [co for co in best_by_co if co not in distinct]

            from collections import Counter
            for co in missing_cos:
                if len(distinct) >= min_co:
                    break
                co_freq = Counter(j.get("company", "").lower() for j in top)
                most_common_co, most_count = co_freq.most_common(1)[0]
                if most_count <= 1:
                    break
                for idx in range(len(top) - 1, -1, -1):
                    if top[idx].get("company", "").lower() == most_common_co:
                        top.pop(idx)
                        break
                top.append(best_by_co[co])
                top.sort(key=lambda j: j.get("fit_score", 0), reverse=True)
                distinct.add(co)

        jobs = top

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

    # ── Pre-generate tailored resumes ──────────────────────────────────────
    # For each job, tailor the base resume to maximize ATS keyword match.
    if not getattr(args, 'company', None):  # Skip for single-company targeted runs
        try:
            from resume_tailor import tailor_resume
            from pdf_generator import save_and_convert
            from pathlib import Path
            import datetime as _dt

            output_base = Path(profiles.output_dir())
            today_dir = output_base / _dt.date.today().isoformat()

            print(f"\n[TAILOR] Pre-generating tailored resumes for {len(jobs)} job(s)...")
            for j in jobs:
                if j.get("pdf_path") and os.path.isfile(j["pdf_path"]):
                    continue  # Already tailored
                if not j.get("description"):
                    continue  # No JD — can't tailor
                try:
                    safe_co    = j.get("company", "unknown").replace("/", "-").replace(" ", "_")[:25]
                    safe_title = j.get("title", "job").replace("/", "-").replace(" ", "_")[:25]
                    job_dir    = today_dir / f"{safe_co}-{safe_title}"
                    pdf_path   = job_dir / "resume.pdf"
                    if pdf_path.exists():
                        j["pdf_path"] = str(pdf_path)
                        continue

                    result = tailor_resume(j)
                    pdf_path = save_and_convert(
                        html_content=result["resume_html"],
                        output_dir=job_dir,
                        filename_stem="resume",
                    )
                    j["pdf_path"] = str(pdf_path)
                    print(f"  [TAILOR] {j.get('company')}/{j.get('title')[:30]} "
                          f"→ match={result.get('match_score',0)}/10  PDF: {pdf_path.name}")
                    import job_store as _js_t
                    _js_t.update_job(j["id"], pdf_path=str(pdf_path))
                except Exception as tailor_exc:
                    import logging as _log
                    _log.getLogger(__name__).debug(f"  [TAILOR SKIP] {j.get('company')}: {tailor_exc}")
            print("[TAILOR] Done.\n")
        except ImportError:
            pass

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
            _out = {}
            result = apply_to_job(job, ctx, dry_run=False, _out=_out)
            if result == "dead":
                # Job page 404 — soft-delete so it never appears again
                if job["id"] != "manual":
                    job_store.remove_job(job["id"])
                print(f"  [DEAD] Marked removed: {job['id']}")
            elif result == "already_applied":
                # Portal confirms we already applied — mark in store
                if job["id"] != "manual":
                    job_store.mark_applied(job["id"])
                    applied_ids.append(job["id"])
                print(f"  [ALREADY APPLIED] Marked in store: {job['id']}")
            elif result and job["id"] != "manual":
                job_store.mark_applied(job["id"])
                applied_ids.append(job["id"])
                print(f"  Marked as applied: {job['id']}")
            elif not result:
                failed_ids.append(job["id"])
                err_reason = _out.get("error", "unknown_error")
                print(f"  [FAILED] {job['title']} @ {job['company']}: {err_reason}")
                if job["id"] != "manual":
                    job_store.mark_applied(job["id"], applied=False, error=err_reason)

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
    main()
