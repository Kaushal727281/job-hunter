#!/usr/bin/env python3
"""
apply_microsoft.py
------------------
Playwright-based automation for Microsoft Careers (Eightfold.ai ATS).

Microsoft uses Eightfold.ai at apply.careers.microsoft.com.

Flow (from HAR analysis):
  1. Navigate to job apply page (apply.careers.microsoft.com/jobs/<JOB_ID>/apply)
  2. Authenticate via Microsoft/Google account when prompted
  3. Extract X-CSRF-Token from page / cookie
  4. Upload resume PDF (POST /api/application/v2/resume_upload?domain=microsoft.com)
  5. Fill contact fields (name, email, phone, city, LinkedIn)
  6. Save draft (PUT /api/pcsx/draft_applications?domain=microsoft.com)
  7. Answer screening questions (stringifiedQuestions JSON)
  8. Pause at Submit for manual click
  9. Submit via POST /api/application/v2/submit?domain=microsoft.com
  10. Detect confirmation

Auth notes:
  - Eightfold uses Google OAuth (accounts.google.com) or Microsoft OAuth
  - Sign in once in the browser window; session persists via cookies
  - X-CSRF-Token is obtained from a GET /api/csrf?domain=microsoft.com call

Usage:
    python3 apply_microsoft.py --job-url "https://jobs.careers.microsoft.com/global/en/job/XXXXXXXX/..."
    python3 apply_microsoft.py --dry-run --job-url "..."
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
    "phone":         "9818147393",
    "phone_code":    "+91",
    "country":       "India",
    "city":          "Bengaluru",
    "linkedin":      "https://www.linkedin.com/in/kaushal-kumar-jha-93b77512a/",
    "current_company": "FICO",
    "current_title": "Lead Software Engineer",
    "experience_years": "7",
    "resume_pdf":    os.path.expanduser(
        "~/gitQW/IO/Resume/job-hunter/profiles/kaushal-kumar-jha/output/"
        "2026-07-31/Okta-Staff_Fullstack_Engineer/resume.pdf"
    ),
    "cover_note": (
        "I am a Lead Software Engineer with 7+ years of experience building "
        "high-throughput Java/Spring Boot microservices and full-stack systems at FICO. "
        "I have deep expertise in distributed systems, cloud-native architecture (AWS/Azure/Kubernetes), "
        "and AI integration (LLM, RAG, LangChain). I am excited about this opportunity at Microsoft."
    ),
}

# ── Eightfold domain ──────────────────────────────────────────────────────────

EIGHTFOLD_DOMAIN  = "microsoft.com"
EIGHTFOLD_API     = "https://apply.careers.microsoft.com"
EIGHTFOLD_APPLY   = "https://apply.careers.microsoft.com/jobs"

# ── Screening question heuristics ─────────────────────────────────────────────

YES_KEYWORDS = (
    "legally authorized", "authorized to work", "eligible to work",
    "right to work", "18 years", "legal age", "agree", "consent",
    "legally entitled", "citizen", "work without",
)
NO_KEYWORDS = (
    "require.*sponsor", "need.*sponsor", "visa sponsor", "sponsorship",
    "criminal", "bonded", "conflict of interest", "non.compete",
    "applied before", "family member", "relative",
)


def _answer_question(text: str, qtype: str) -> str | None:
    t = text.lower()
    want_yes = any(re.search(kw, t) for kw in YES_KEYWORDS)
    want_no  = any(re.search(kw, t) for kw in NO_KEYWORDS) and not want_yes
    if want_yes:
        return "true" if qtype == "boolean" else "Yes"
    if want_no:
        return "false" if qtype == "boolean" else "No"
    return None


# ── Helpers ───────────────────────────────────────────────────────────────────

def settle(page, ms: int = 2000):
    try:
        page.wait_for_load_state("networkidle", timeout=ms)
    except Exception:
        pass
    time.sleep(0.5)


def click_if_visible(page, selector: str, timeout: int = 3000) -> bool:
    try:
        loc = page.locator(selector).first
        if loc.is_visible(timeout=timeout):
            loc.click()
            return True
    except Exception:
        pass
    return False


def _get_csrf(page) -> str:
    """Get Eightfold CSRF token."""
    # Try fetching the CSRF endpoint
    try:
        token = page.evaluate(
            f"""
            async () => {{
                const r = await fetch('{EIGHTFOLD_API}/api/csrf?domain={EIGHTFOLD_DOMAIN}', {{
                    credentials: 'include',
                }});
                const data = await r.json();
                return data.token || data.csrf_token || '';
            }}
            """
        )
        if token:
            return token
    except Exception:
        pass

    # Fallback: from cookie
    try:
        cookies = page.context.cookies()
        for c in cookies:
            if "csrf" in c["name"].lower():
                return c["value"]
    except Exception:
        pass

    # Fallback: from page meta tag (Eightfold sometimes sets it)
    try:
        return page.locator('meta[name="csrf-token"]').get_attribute("content", timeout=2000) or ""
    except Exception:
        return ""


def _extract_job_id(url: str) -> str:
    """Extract Eightfold job ID from URL."""
    # apply.careers.microsoft.com/jobs/1970393556952571/apply
    # jobs.careers.microsoft.com/global/en/job/1970393556952571/...
    m = re.search(r"/job[s]?/(\d+)", url)
    return m.group(1) if m else ""


# ── Main apply function ───────────────────────────────────────────────────────

def apply(job_url: str, dry_run: bool = False):
    resume_path = PROFILE["resume_pdf"]
    if not os.path.isfile(resume_path):
        print(f"[ERROR] Resume PDF not found: {resume_path}")
        sys.exit(1)

    print(f"[INFO] Job URL  : {job_url}")
    print(f"[INFO] Resume   : {resume_path}")

    job_id = _extract_job_id(job_url)
    if not job_id:
        print("[WARN] Could not extract job ID from URL — will try to proceed anyway")

    # Copy Chrome cookies for existing session
    chrome_src = os.path.expanduser("~/Library/Application Support/Google/Chrome/Default")
    tmp = tempfile.mkdtemp(prefix="chrome_ms_")
    dst = os.path.join(tmp, "Default")
    os.makedirs(dst, exist_ok=True)
    for f in ("Cookies", "Cookies-journal", "Login Data"):
        s = os.path.join(chrome_src, f)
        if os.path.isfile(s):
            shutil.copy2(s, os.path.join(dst, f))
            print(f"[INFO] Copied Chrome file: {f}")

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

        # ── Step 1: Navigate to job apply page ───────────────────────────
        print(f"\n[STEP 1] Loading Microsoft Careers job page...")
        # Convert public job URL to Eightfold apply URL
        if "apply.careers.microsoft.com" in job_url:
            apply_url = job_url
        elif job_id:
            apply_url = f"{EIGHTFOLD_APPLY}/{job_id}/apply?domain={EIGHTFOLD_DOMAIN}"
        else:
            apply_url = job_url

        page.goto(apply_url, wait_until="networkidle", timeout=45000)
        settle(page, 4000)
        print(f"  Title: {page.title()}")
        print(f"  URL  : {page.url}")

        if dry_run:
            print("[DRY RUN] Page loaded. Exiting without applying.")
            ctx.close()
            return

        # ── Step 2: Handle login if needed ───────────────────────────────
        current_url = page.url
        is_login = any(k in current_url.lower() for k in (
            "login", "signin", "auth", "accounts.google", "microsoftonline", "oauth"
        ))
        if is_login or "apply.careers.microsoft.com" not in current_url:
            print("\n[STEP 2] Login required.")
            print("  Please sign in with your Google or Microsoft account in the browser.")
            print("  Waiting up to 5 minutes...")
            try:
                page.wait_for_url(
                    "**/apply.careers.microsoft.com/**",
                    timeout=300_000,
                )
                settle(page, 4000)
                print(f"  Authenticated! URL: {page.url}")
            except Exception:
                print("  [WARN] Login wait timed out, continuing...")

        # Navigate to the specific job apply page if needed
        if job_id and "/apply" not in page.url:
            page.goto(
                f"{EIGHTFOLD_APPLY}/{job_id}/apply?domain={EIGHTFOLD_DOMAIN}",
                wait_until="networkidle", timeout=30000,
            )
            settle(page, 3000)

        # ── Step 3: Get CSRF token ────────────────────────────────────────
        print("\n[STEP 3] Getting CSRF token...")
        csrf_token = _get_csrf(page)
        if csrf_token:
            print(f"  Got CSRF: {csrf_token[:20]}...")
        else:
            print("  [WARN] No CSRF token found — API calls may fail")

        # ── Step 4: Upload resume ─────────────────────────────────────────
        print("\n[STEP 4] Uploading resume...")
        try:
            fi = page.locator('input[type="file"]').first
            fi.wait_for(state="attached", timeout=15000)
            fi.set_input_files(resume_path)
            settle(page, 6000)
            print("  Resume uploaded via file input.")
        except Exception as ex:
            print(f"  [WARN] File input upload: {ex}")
            # Fallback: try API upload
            try:
                upload_resp = page.evaluate(
                    f"""
                    async (csrf) => {{
                        const data = new FormData();
                        // Can't attach actual file bytes from here
                        // Eightfold resume upload: POST /api/application/v2/resume_upload
                        return {{"error": "file-input-required"}};
                    }}
                    """,
                    csrf_token,
                )
                print(f"  [WARN] API upload: {upload_resp}")
            except Exception:
                pass

        # ── Step 5: Fill contact / personal info ──────────────────────────
        print("\n[STEP 5] Filling personal info...")
        settle(page, 2000)

        field_map = [
            # Eightfold uses data-ph-at-id or name attributes
            (
                '[data-ph-at-id="first-name-input"], input[name="firstName"], '
                'input[placeholder*="First name"], input[placeholder*="First Name"]',
                PROFILE["first_name"],
            ),
            (
                '[data-ph-at-id="last-name-input"], input[name="lastName"], '
                'input[placeholder*="Last name"], input[placeholder*="Last Name"]',
                PROFILE["last_name"],
            ),
            (
                '[data-ph-at-id="email-input"], input[name="email"], '
                'input[type="email"], input[placeholder*="Email"]',
                PROFILE["email"],
            ),
            (
                '[data-ph-at-id="phone-input"], input[name="phone"], '
                'input[type="tel"], input[placeholder*="Phone"]',
                PROFILE["phone"],
            ),
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

        # City / location (Eightfold question_id: q_city)
        for sel in (
            'input[name="q_city"]', '[data-ph-at-id="city-input"]',
            'input[name="city"]', 'input[placeholder*="City"]',
            'input[placeholder*="Location"]',
        ):
            try:
                el = page.locator(sel).first
                if el.is_visible(timeout=1000):
                    if not el.input_value():
                        el.fill(PROFILE["city"])
                    break
            except Exception:
                pass

        # State (Eightfold question_id: q_state)
        for sel in ('input[name="q_state"]', 'input[placeholder*="State"]'):
            try:
                el = page.locator(sel).first
                if el.is_visible(timeout=1000):
                    if not el.input_value():
                        el.fill("Karnataka")
                    break
            except Exception:
                pass

        # Country dropdown (question_id: q_country)
        for sel in ('select[name="q_country"]', 'select[name="country"]'):
            try:
                el = page.locator(sel).first
                if el.is_visible(timeout=1000):
                    el.select_option(label="India")
                    break
            except Exception:
                pass

        # Zip/postal (question_id: q_zip)
        for sel in (
            'input[name="q_zip"]', 'input[name*="zip"]',
            'input[placeholder*="Zip"]', 'input[placeholder*="Postal"]',
        ):
            try:
                el = page.locator(sel).first
                if el.is_visible(timeout=1000):
                    if not el.input_value():
                        el.fill("560100")
                    break
            except Exception:
                pass

        # LinkedIn URL
        for sel in (
            'input[placeholder*="LinkedIn"]', 'input[name*="linkedin"]',
            'input[placeholder*="Website"]', 'input[name*="website"]',
        ):
            try:
                el = page.locator(sel).first
                if el.is_visible(timeout=1000):
                    if not el.input_value():
                        el.fill(PROFILE["linkedin"])
                    break
            except Exception:
                pass

        # Current company / title
        for sel in ('input[placeholder*="Current company"]', 'input[name*="company"]'):
            try:
                el = page.locator(sel).first
                if el.is_visible(timeout=1000):
                    if not el.input_value():
                        el.fill(PROFILE["current_company"])
                    break
            except Exception:
                pass

        for sel in ('input[placeholder*="Current title"]', 'input[name*="title"]'):
            try:
                el = page.locator(sel).first
                if el.is_visible(timeout=1000):
                    if not el.input_value():
                        el.fill(PROFILE["current_title"])
                    break
            except Exception:
                pass

        # Work legal authorization (q_cust_workLegalAuth) → Yes
        # Microsoft-specific: "Are you legally authorized to work in India?"
        for sel in (
            'select[name="q_cust_workLegalAuth"]',
            '[data-question-id="q_cust_workLegalAuth"] select',
        ):
            try:
                el = page.locator(sel).first
                if el.is_visible(timeout=800):
                    el.select_option(label="Yes")
                    break
            except Exception:
                pass

        # Employment eligibility (q_cust_empEligibility) → No (no sponsorship needed)
        for sel in (
            'select[name="q_cust_empEligibility"]',
            '[data-question-id="q_cust_empEligibility"] select',
        ):
            try:
                el = page.locator(sel).first
                if el.is_visible(timeout=800):
                    el.select_option(label="No")
                    break
            except Exception:
                pass

        # Disclaimer checkbox (q_cust_disclaimer)
        for sel in (
            'input[name="q_cust_disclaimer"]',
            '[data-question-id="q_cust_disclaimer"] input[type="checkbox"]',
        ):
            try:
                el = page.locator(sel).first
                if el.is_visible(timeout=800) and not el.is_checked():
                    el.check()
                    break
            except Exception:
                pass

        # ── Step 6: Answer screening questions ────────────────────────────
        print("\n[STEP 6] Answering screening questions...")
        answered = 0

        # Eightfold question containers
        containers = page.locator(
            '[data-ph-at-id*="question"], .application-question, '
            '[class*="question-item"], [class*="screening"], '
            'fieldset, [role="radiogroup"]'
        ).all()

        for container in containers:
            try:
                q_text = container.inner_text(timeout=500)
            except Exception:
                continue

            answer = _answer_question(q_text, "radio")
            if not answer:
                continue

            want_yes = answer in ("Yes", "true")
            selectors = (
                ['label:has-text("Yes")', 'input[value="Yes"]', 'input[value="yes"]', '[aria-label="Yes"]']
                if want_yes else
                ['label:has-text("No")', 'input[value="No"]', 'input[value="no"]', '[aria-label="No"]']
            )
            for sel in selectors:
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

        # ── Step 7: Add cover note / message ─────────────────────────────
        print("\n[STEP 7] Adding cover note...")
        for sel in (
            'textarea[placeholder*="cover"]', 'textarea[placeholder*="message"]',
            'textarea[placeholder*="additional"]', 'textarea[name*="cover"]',
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

        # ── Step 8: Navigate wizard pages ─────────────────────────────────
        print("\n[STEP 8] Navigating wizard pages...")
        for step in range(1, 12):
            # Check for submit button
            try:
                submit_btn = page.locator(
                    'button[data-ph-at-id="submit-button"], '
                    'button:has-text("Submit"), button:has-text("Submit Application"), '
                    'button[type="submit"]'
                ).first
                if submit_btn.is_visible(timeout=2000):
                    print(f"  Submit button visible at step {step}")
                    break
            except Exception:
                pass

            # Click Next
            clicked = False
            for sel in (
                'button[data-ph-at-id="next-button"]',
                'button:has-text("Next")', 'button:has-text("Continue")',
                'button:has-text("Save and Continue")',
                '.next-button', '#next-button',
            ):
                try:
                    btn = page.locator(sel).first
                    if btn.is_visible(timeout=2000):
                        btn.click()
                        settle(page, 3000)
                        answered_step = 0
                        # Re-answer any new questions on this step
                        containers = page.locator(
                            '[data-ph-at-id*="question"], fieldset, [role="radiogroup"]'
                        ).all()
                        for container in containers:
                            try:
                                q_text = container.inner_text(timeout=500)
                                ans = _answer_question(q_text, "radio")
                                if not ans:
                                    continue
                                want_yes = ans in ("Yes", "true")
                                sels = (
                                    ['label:has-text("Yes")', 'input[value="Yes"]']
                                    if want_yes else
                                    ['label:has-text("No")', 'input[value="No"]']
                                )
                                for s in sels:
                                    try:
                                        el = container.locator(s).first
                                        if el.is_visible(timeout=400):
                                            el.evaluate("el => el.click()")
                                            answered_step += 1
                                            break
                                    except Exception:
                                        pass
                            except Exception:
                                pass
                        if answered_step:
                            print(f"  Step {step}: answered {answered_step} new questions")
                        clicked = True
                        break
                except Exception:
                    pass

            if not clicked:
                print(f"  No Next button at step {step}, stopping.")
                break

        # ── Step 9: Human-like scroll ─────────────────────────────────────
        print("\n[STEP 9] Simulating human activity...")
        for y in range(0, 1600, 200):
            page.evaluate(f"window.scrollTo(0, {y})")
            time.sleep(0.2)
        for y in range(1600, 0, -300):
            page.evaluate(f"window.scrollTo(0, {y})")
            time.sleep(0.2)
        page.mouse.move(640, 400)
        time.sleep(0.3)
        page.mouse.move(920, 550)
        time.sleep(2)

        # Scroll submit button into view
        try:
            submit_btn = page.locator(
                'button[data-ph-at-id="submit-button"], '
                'button:has-text("Submit Application"), '
                'button:has-text("Submit"), button[type="submit"]'
            ).first
            submit_btn.evaluate("el => el.scrollIntoView({block:'center'})")
            time.sleep(1)
        except Exception:
            pass

        # ── Step 10: Pause for manual submit ──────────────────────────────
        print("\n" + "=" * 60)
        print("  ALL FIELDS FILLED. Please review and click Submit")
        print("  in the browser window. Waiting up to 15 minutes...")
        print("=" * 60 + "\n")

        try:
            page.wait_for_url(
                "**/confirmation**|**/thank-you**|**/thankyou**"
                "|**/success**|**/complete**|**/submitted**",
                timeout=900_000,
            )
        except Exception:
            try:
                page.wait_for_selector(
                    ':has-text("Application submitted"), '
                    ':has-text("Thank you"), :has-text("successfully submitted"), '
                    ':has-text("application has been received")',
                    timeout=900_000,
                )
            except Exception:
                pass

        final_url = page.url
        print(f"\n[RESULT] {final_url}")
        if any(k in final_url.lower() for k in ("confirm", "thank", "success", "complete", "submitted")):
            print("[SUCCESS] Application submitted!")
        else:
            try:
                body = page.inner_text("body")[:600]
                if any(k in body.lower() for k in ("thank you", "successfully", "submitted", "received")):
                    print("[SUCCESS] Application submitted! (detected from page text)")
                else:
                    page.screenshot(path="/tmp/microsoft_apply_result.png")
                    print("[WARN] Not confirmed — screenshot at /tmp/microsoft_apply_result.png")
            except Exception:
                page.screenshot(path="/tmp/microsoft_apply_result.png")
                print("[WARN] Not confirmed — screenshot at /tmp/microsoft_apply_result.png")

        time.sleep(5)
        ctx.close()


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Apply to a Microsoft Careers job (Eightfold.ai ATS)"
    )
    parser.add_argument(
        "--job-url",
        required=True,
        help=(
            'Full job URL, e.g. '
            '"https://jobs.careers.microsoft.com/global/en/job/XXXXXXXX/..."'
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Load the page without filling or submitting",
    )
    args = parser.parse_args()
    apply(args.job_url, dry_run=args.dry_run)
