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
import email as _emaillib
import imaplib
import os
import re
import sys
import tempfile
import time
from email.header import decode_header as _decode_header

from playwright.sync_api import sync_playwright

try:
    import browser_cookie3 as _bc3
    _HAS_BC3 = True
except ImportError:
    _HAS_BC3 = False
from playwright_stealth import Stealth
import automation_log

# ── Candidate profile (loaded from .env) ──────────────────────────────────────

import dotenv as _dotenv
_dotenv.load_dotenv(os.path.join(os.path.dirname(__file__), ".env"), override=False)

def _e(key: str, default: str = "") -> str:
    return os.environ.get(key, default)

_ef_pwds_raw = _e("EIGHTFOLD_PASSWORDS")
PROFILE = {
    "first_name":    _e("CANDIDATE_FIRST_NAME"),
    "last_name":     _e("CANDIDATE_LAST_NAME"),
    "email":         _e("CANDIDATE_EMAIL"),
    "phone":         _e("CANDIDATE_PHONE_E164"),
    "country":       _e("CANDIDATE_COUNTRY", "India"),
    "state":         _e("CANDIDATE_STATE"),
    "city":          _e("CANDIDATE_CITY"),
    "address1":      _e("CANDIDATE_ADDRESS"),
    "postal_code":   _e("CANDIDATE_POSTAL_CODE"),
    "linkedin":      _e("CANDIDATE_LINKEDIN"),
    "passwords":     [p.strip() for p in _ef_pwds_raw.split(",") if p.strip()],
    "current_ctc":   _e("CANDIDATE_CURRENT_CTC"),
    "expected_ctc":  _e("CANDIDATE_EXPECTED_CTC"),
    "notice_period": _e("CANDIDATE_NOTICE_PERIOD"),
    "resume_pdf":    os.path.expanduser(_e("CANDIDATE_RESUME_PDF")),
}

# ── Gmail OTP helper ──────────────────────────────────────────────────────────

def _fetch_eightfold_otp(timeout_sec: int = 90) -> str | None:
    """
    Poll Gmail for a Eightfold/Qualcomm OTP email and return the 6-digit code.
    Reads credentials from .env in the same directory.
    """
    import dotenv
    _env = os.path.join(os.path.dirname(__file__), ".env")
    dotenv.load_dotenv(_env, override=False)
    _user = os.environ.get("GMAIL_ADDRESS", "")
    _pwd  = os.environ.get("GMAIL_APP_PASSWORD", "")
    if not _user or not _pwd:
        print("  [auth] No Gmail creds in .env — cannot auto-fetch OTP")
        return None

    deadline = time.time() + timeout_sec
    print(f"  [auth] Polling Gmail for Eightfold OTP (up to {timeout_sec}s)…")
    while time.time() < deadline:
        try:
            mail = imaplib.IMAP4_SSL("imap.gmail.com")
            mail.login(_user, _pwd)
            mail.select("INBOX")
            # Look for recent UNSEEN from eightfold
            _, msgs = mail.search(None, 'FROM "eightfold.ai" UNSEEN')
            ids = msgs[0].split()
            if not ids:
                # Fallback: any recent eightfold email
                _, msgs2 = mail.search(None, 'FROM "eightfold.ai"')
                ids = msgs2[0].split()
            for uid in reversed(ids[-3:]):
                _, data = mail.fetch(uid, "(RFC822)")
                msg = _emaillib.message_from_bytes(data[0][1])
                subj_parts = _decode_header(msg.get("Subject", ""))
                subj = "".join(
                    p.decode(enc or "utf-8") if isinstance(p, bytes) else str(p)
                    for p, enc in subj_parts
                )
                if "verify" not in subj.lower() and "code" not in subj.lower() and "otp" not in subj.lower():
                    continue
                body = ""
                for part in msg.walk():
                    ct = part.get_content_type()
                    if ct in ("text/plain", "text/html"):
                        raw = part.get_payload(decode=True).decode("utf-8", errors="replace")
                        if ct == "text/html":
                            raw = re.sub(r'<[^>]+>', ' ', raw)
                            raw = re.sub(r'\s+', ' ', raw)
                        body += raw + "\n"
                # Extract 4-8 digit OTP
                m = re.search(r'\b(\d{4,8})\b', body)
                if m:
                    code = m.group(1)
                    print(f"  [auth] Got Eightfold OTP: {code}")
                    mail.store(uid, "+FLAGS", "\\Seen")
                    mail.logout()
                    return code
            mail.logout()
        except Exception as exc:
            pass
        time.sleep(5)
    print("  [auth] Timed out waiting for Eightfold OTP")
    return None


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

    # Use a PERSISTENT profile directory keyed by domain so login sessions survive
    # between jobs. After first OTP/Google login, subsequent runs reuse the session.
    import urllib.parse as _urlparse
    _domain_key = _urlparse.urlparse(job_url).hostname or "eightfold"
    _profile_dir = os.path.expanduser(f"~/.ef_profiles/{_domain_key}")
    os.makedirs(_profile_dir, exist_ok=True)
    print(f"[INFO] Profile  : {_profile_dir}")

    with sync_playwright() as pw:
        try:
            ctx = pw.chromium.launch_persistent_context(
                user_data_dir=_profile_dir, headless=False, channel="chrome",
                viewport={"width": 1280, "height": 900},
                args=["--disable-blink-features=AutomationControlled"],
            )
        except Exception:
            ctx = pw.chromium.launch_persistent_context(
                user_data_dir=_profile_dir, headless=False,
                viewport={"width": 1280, "height": 900},
                args=["--disable-blink-features=AutomationControlled"],
            )

        page = ctx.new_page()
        Stealth().apply_stealth_sync(page)


        # Inject ONLY consent/analytics cookies from Chrome (not auth/session tokens).
        # Qualcomm's Eightfold SPA requires consent cookies to render, but injecting
        # stale auth tokens causes a blank "zombie session". We filter them out.
        if _HAS_BC3:
            try:
                import urllib.parse as _up
                _domain = _up.urlparse(job_url).hostname or "qualcomm.com"
                _base_domain = ".".join(_domain.split(".")[-2:])
                _auth_patterns = (
                    "session", "token", "auth", "user_id", "uid", "jwt",
                    "access", "refresh", "login", "signed",
                )
                chrome_cookies = _bc3.chrome(domain_name=_base_domain)
                pw_cookies = []
                for c in chrome_cookies:
                    _lname = c.name.lower()
                    # Skip any cookie whose name suggests auth/session
                    if any(p in _lname for p in _auth_patterns):
                        continue
                    pw_cookies.append({
                        "name":     c.name,
                        "value":    c.value,
                        "domain":   c.domain if c.domain.startswith(".") else "." + c.domain,
                        "path":     c.path or "/",
                        "secure":   bool(c.secure),
                        "httpOnly": False,
                        "sameSite": "None",
                    })
                if pw_cookies:
                    ctx.add_cookies(pw_cookies)
                    print(f"[INFO] Injected {len(pw_cookies)} non-auth Chrome cookies")
            except Exception as _e:
                print(f"[WARN] Cookie injection: {_e}")

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

        # ── Step 2b: Handle Sign In modal (Eightfold / Qualcomm) ─────────────
        # Qualcomm's Eightfold uses a two-step modal:
        #   Step A: Enter email → click "Continue"
        #   Step B: Password field appears → enter password → click "Sign In"
        # After sign-in, the SPA re-renders to show the apply form.
        _ef_email = PROFILE["email"]
        _ef_passwords = PROFILE.get("passwords", [])
        if not _ef_passwords and PROFILE.get("password"):
            _ef_passwords = [PROFILE["password"]]

        _auth_success = False

        # Check if a sign-in form is present at all
        # Wait up to 8s for the modal/form to render after Apply click
        _signin_present = False
        for _chk in (
            'input[type="email"]',
            'input[name="username"]',
            'input[name="email"]',
            'button:has-text("Continue")',
            'button:has-text("Sign in using Google")',
            'h2:has-text("Sign in")',
            'h1:has-text("Sign in")',
            ':text("Sign in")',
            ':text("First time here")',
        ):
            try:
                if page.locator(_chk).first.is_visible(timeout=5000):
                    _signin_present = True
                    print(f"  [auth] Sign-in detected via: {_chk!r}")
                    break
            except Exception:
                pass

        if not _signin_present:
            _auth_success = True  # no login required
            print("  [auth] No sign-in modal — proceeding")
        else:
            print(f"  [auth] Sign-in modal detected — trying {len(_ef_passwords)} password(s)")
            for _ef_password in _ef_passwords:
                if not _ef_password:
                    continue
                try:
                    # ── Step A: Fill email ─────────────────────────────────────
                    for sel in ('input[name="username"]', 'input[type="email"]',
                                'input[placeholder*="Email"]', 'input[id*="email"]'):
                        try:
                            el = page.locator(sel).first
                            if el.is_visible(timeout=1500):
                                if not el.input_value():
                                    el.fill(_ef_email)
                                    time.sleep(0.3)
                                break
                        except Exception:
                            pass

                    # Check if password field already visible (single-step form)
                    _pwd_el = None
                    for sel in ('input[type="password"]', 'input[name="password"]',
                                'input[id*="password"]', 'input[placeholder*="Password"]'):
                        try:
                            el = page.locator(sel).first
                            if el.is_visible(timeout=1000):
                                _pwd_el = el
                                break
                        except Exception:
                            pass

                    if _pwd_el is None:
                        # Two-step: click Continue to reveal password field
                        for sel in ('button:has-text("Continue")', 'button:has-text("Next")',
                                    'button[type="submit"]'):
                            try:
                                btn = page.locator(sel).first
                                if btn.is_visible(timeout=1500):
                                    btn.click()
                                    print(f"  [auth] Clicked Continue (email step)")
                                    # Wait explicitly for password field to appear
                                    try:
                                        page.locator('input[type="password"]').first.wait_for(
                                            state="visible", timeout=8000
                                        )
                                    except Exception:
                                        pass
                                    break
                            except Exception:
                                pass
                        # Re-find password field
                        for sel in ('input[type="password"]', 'input[name="password"]',
                                    'input[id*="password"]', 'input[placeholder*="Password"]'):
                            try:
                                el = page.locator(sel).first
                                if el.is_visible(timeout=2000):
                                    _pwd_el = el
                                    break
                            except Exception:
                                pass

                    if _pwd_el is None:
                        # No password field — Qualcomm uses OTP/magic-link flow.
                        # Qualcomm Eightfold OTP: 6 separate text inputs with
                        #   className="numberInput-1-Id4"
                        #   aria-label="Please enter OTP character N"
                        # Wait up to 30s for the first OTP box to appear.
                        _OTP_CHAR_SEL = (
                            'input.numberInput-1-Id4',
                            '[aria-label*="OTP character"]',
                            '[aria-label*="enter OTP"]',
                            # Generic fallbacks for other Eightfold portals
                            'input[placeholder*="code" i]',
                            'input[placeholder*="OTP" i]',
                            'input[placeholder*="verification" i]',
                            'input[maxlength="6"]',
                            'input[maxlength="8"]',
                            'input[autocomplete="one-time-code"]',
                        )
                        _otp_el = None
                        for _attempt in range(6):  # 6 x 5s = 30s max
                            for sel in _OTP_CHAR_SEL:
                                try:
                                    el = page.locator(sel).first
                                    if el.is_visible(timeout=3000):
                                        _otp_el = el
                                        break
                                except Exception:
                                    pass
                            if _otp_el:
                                break
                            time.sleep(5)

                        if _otp_el is not None:
                            print("  [auth] OTP field(s) detected — auto-fetching from Gmail…")
                            _otp_code = _fetch_eightfold_otp(timeout_sec=90)
                            if _otp_code:
                                # Check if it's a multi-box OTP (Qualcomm: 6 separate inputs)
                                _otp_boxes = []
                                try:
                                    _otp_boxes = page.locator(
                                        'input.numberInput-1-Id4, '
                                        '[aria-label*="OTP character"], '
                                        '[aria-label*="enter OTP"]'
                                    ).all()
                                    _otp_boxes = [b for b in _otp_boxes if b.is_visible(timeout=500)]
                                except Exception:
                                    pass

                                if len(_otp_boxes) > 1:
                                    # Multi-box: type one digit per box
                                    for _i, _box in enumerate(_otp_boxes):
                                        if _i < len(_otp_code):
                                            try:
                                                _box.click()
                                                time.sleep(0.1)
                                                _box.type(_otp_code[_i])
                                                time.sleep(0.1)
                                            except Exception:
                                                pass
                                    print(f"  [auth] OTP filled in {len(_otp_boxes)} boxes: {_otp_code}")
                                else:
                                    # Single input box
                                    _otp_el.fill(_otp_code)
                                    print(f"  [auth] OTP filled (single box): {_otp_code}")

                                time.sleep(0.5)
                                # Click Verify / Continue / Submit button
                                for _btn_sel in (
                                    'button:has-text("Verify")',
                                    'button:has-text("Submit")',
                                    'button:has-text("Continue")',
                                    'button[type="submit"]',
                                ):
                                    try:
                                        _btn = page.locator(_btn_sel).first
                                        if _btn.is_visible(timeout=2000):
                                            _btn.click()
                                            print(f"  [auth] OTP submitted")
                                            break
                                    except Exception:
                                        pass
                            else:
                                print("  [auth] Could not auto-fetch OTP — waiting 5 min for manual entry")
                            # Wait for OTP boxes to disappear (modal closes after verify)
                            try:
                                page.locator('input.numberInput-1-Id4').first.wait_for(
                                    state="hidden", timeout=300_000
                                )
                            except Exception:
                                pass
                            settle(page, 5000)
                            _auth_success = True
                            print("  [auth] OTP login complete")
                            break
                        else:
                            print(f"  [auth] No password or OTP field after 30s — skipping")
                            continue

                    # ── Step B: Fill password + click Sign In ──────────────────
                    _pwd_el.fill(_ef_password)
                    time.sleep(0.3)

                    for sel in (
                        'button:has-text("Sign In")', 'button:has-text("Log In")',
                        'button:has-text("Sign in")',
                        'button[type="submit"]',
                        'input[type="submit"]',
                    ):
                        try:
                            btn = page.locator(sel).first
                            if btn.is_visible(timeout=1500):
                                btn.click()
                                print(f"  [auth] Clicked sign-in btn (pwd: {_ef_password[:4]}***)")
                                break
                        except Exception:
                            pass

                    # Wait for modal to close / page to re-render
                    try:
                        page.locator('input[type="password"]').first.wait_for(
                            state="hidden", timeout=8000
                        )
                    except Exception:
                        pass
                    settle(page, 5000)

                    # Confirm sign-in modal is gone (key success indicator)
                    _modal_gone = True
                    for sel in ('input[type="password"]', 'h2:has-text("Sign in")'):
                        try:
                            if page.locator(sel).first.is_visible(timeout=1500):
                                _modal_gone = False
                                break
                        except Exception:
                            pass

                    if _modal_gone:
                        print(f"  [auth] Login succeeded with pwd {_ef_password[:4]}*** — modal closed")
                        _auth_success = True
                        # Give SPA time to fully re-render after auth
                        settle(page, 5000)
                        break
                    else:
                        print(f"  [auth] Sign-in modal still visible after pwd {_ef_password[:4]}*** — trying next")

                except Exception as _login_ex:
                    print(f"  [auth] Exception during login: {_login_ex}")
                    break

        if _ef_passwords and not _auth_success and _signin_present:
            print("[WARN] All passwords failed — apply form may not be accessible")

        # ── Step 2c: Re-click Apply after login (Qualcomm redirects to job page) ─
        if _auth_success and "/apply" not in page.url:
            print("[STEP 2c] Post-login: clicking Apply again...")
            for sel in (
                'a:has-text("Apply Now")', 'button:has-text("Apply Now")',
                'a:has-text("Apply")',     'button:has-text("Apply")',
                '[data-ph-id="ph-page-element-page-applyButton"]',
            ):
                if click_if_visible(page, sel, 4000):
                    print(f"  Re-clicked Apply: {sel}")
                    settle(page, 4000)
                    break

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

        # ── Step 3b: Advance to next form step if file input not yet visible ─────
        # Qualcomm/Eightfold multi-step: step 1 = personal info, step 2 = resume
        _file_present = False
        try:
            _file_present = page.locator('input[type="file"]').first.is_visible(timeout=3000)
        except Exception:
            pass
        if not _file_present:
            # Try clicking Next/Continue to advance to resume upload step
            for sel in (
                'button:has-text("Next")', 'button:has-text("Continue")',
                'button:has-text("Proceed")',
                '[data-ph-id*="next"]', '[data-ph-id*="continue"]',
                'button[type="submit"]:not(:has-text("Submit"))',
            ):
                try:
                    btn = page.locator(sel).first
                    if btn.is_visible(timeout=1500):
                        btn.click()
                        settle(page, 3000)
                        print(f"  [step-advance] Clicked: {sel}")
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

        # ── Step 5b: Check consent/T&C checkboxes ────────────────────────────
        print("[STEP 5b] Checking consent checkboxes...")
        for _consent_sel in (
            'label:has-text("I consent")',
            'input[type="checkbox"] ~ label:has-text("consent")',
            'label:has-text("consent")',
            'label:has-text("I agree")',
            'label:has-text("agree")',
            'label:has-text("certify")',
        ):
            try:
                _cbx = page.locator(_consent_sel).first
                if _cbx.is_visible(timeout=2000):
                    _cbx.scroll_into_view_if_needed()
                    _cbx.click()
                    print(f"  Checked consent: {_consent_sel}")
                    time.sleep(0.3)
            except Exception:
                pass
        # Also check any unchecked checkboxes in T&C / acknowledgement sections
        try:
            _tnc_section = page.locator(
                'section:has-text("Terms and Conditions"), '
                'div:has-text("Terms and Conditions"), '
                '.terms-and-conditions'
            ).first
            if _tnc_section.is_visible(timeout=1000):
                for _cb in _tnc_section.locator('input[type="checkbox"]').all():
                    try:
                        if not _cb.is_checked():
                            _cb.check()
                            print("  Checked T&C checkbox via section")
                    except Exception:
                        pass
        except Exception:
            pass

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

        # ── Step 7: Auto-submit, then wait for confirmation ──────────────────
        _submitted = False
        for _sel in (
            'button:has-text("Submit Application")',
            'button:has-text("Submit")',
            'input[value="Submit Application"]',
            'button[type="submit"]:not(:has-text("Next")):not(:has-text("Continue"))',
        ):
            try:
                btn = page.locator(_sel).first
                if btn.is_visible(timeout=3000):
                    btn.click()
                    settle(page, 5000)
                    print(f"  [auto-submit] Clicked: {_sel}")
                    _submitted = True
                    break
            except Exception:
                pass

        if not _submitted:
            print("\n" + "=" * 60)
            print("  ALL FIELDS FILLED. Please review and click Submit")
            print("  in the browser window. Waiting up to 10 minutes...")
            print("=" * 60 + "\n")

        _wait_ms = 90_000 if not _submitted else 600_000
        # Success detection patterns for Eightfold
        try:
            page.wait_for_url(
                re.compile(r"(success|thank|submitted|profile.review)", re.I),
                timeout=_wait_ms,
            )
        except Exception:
            pass

        final_url = page.url
        print(f"\n[RESULT] {final_url}")
        success = any(k in final_url.lower() for k in ("success", "thank", "submitted", "profile-review"))
        if success:
            print("[SUCCESS] Application submitted!")
        else:
            page.screenshot(path="/tmp/ef_apply_result.png")
            print("[WARN] Not confirmed — screenshot at /tmp/ef_apply_result.png")

        time.sleep(5)
        ctx.close()
        return success


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Apply to Eightfold AI careers jobs (Micron, Applied Materials, Qualcomm, Microsoft, etc.)"
    )
    parser.add_argument("--job-url",   help="Single apply URL (single-job mode)")
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
        if j.get("ats_type") == "eightfold"
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
        print(f"No Eightfold jobs with fit_score >= {args.min_score} ready to apply.")
        sys.exit(0)

    print(f"\nFound {len(jobs)} Eightfold job(s) to apply to:\n")
    for j in jobs:
        print(f"  [{j.get('fit_score','?')}/10] {j.get('title','?')[:55]} @ {j.get('company','?')}")

    applied_count, failed_count = 0, 0
    for j in jobs:
        print(f"\n{'='*60}")
        print(f"  JOB  : {j['title']} @ {j['company']}")
        print(f"  SCORE: {j.get('fit_score','?')}/10")
        print(f"  URL  : {j['apply_link']}")
        print(f"{'='*60}")
        _run_id = automation_log.log_start(j)
        try:
            ok = apply(j["apply_link"], dry_run=args.dry_run)
            if ok:
                job_store.mark_applied(j["id"], applied=True)
                applied_count += 1
                automation_log.log_finish(_run_id, "success")
                print(f"  Marked applied: {j['id']}")
            else:
                err_msg = "Form not confirmed — manual review needed"
                job_store.mark_applied(j["id"], applied=False, error=err_msg)
                automation_log.log_finish(_run_id, "failed", error=err_msg)
                failed_count += 1
        except Exception as exc:
            err = str(exc)[:200]
            print(f"[ERROR] {err}")
            job_store.mark_applied(j["id"], applied=False, error=err)
            automation_log.log_finish(_run_id, "error", error=err)
            failed_count += 1
        time.sleep(2)

    print(f"\n{'='*60}")
    print(f"  Eightfold apply complete — Applied: {applied_count}  Failed: {failed_count}")
    print(f"{'='*60}")
