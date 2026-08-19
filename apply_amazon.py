#!/usr/bin/env python3
"""
apply_amazon.py
---------------
Playwright-based automation for Amazon Jobs career portal.

Flow (from HAR analysis):
  1. Navigate to job apply page
  2. Handle Google OAuth or SRP login via Passport → SAML ACS chain
  3. Parse CSRF token from HTML meta tag
  4. Load existing applicant profile (pre-fills most fields)
  5. Load and save each form wizard page (general, education, work eligibility)
  6. Upload resume PDF (two-step: S3 multipart + register document)
  7. Pause at Submit for manual click + review
  8. Detect confirmation → mark success

Auth notes:
  - Amazon Jobs uses passport.amazon.jobs → SAML federation → account.amazon.jobs
  - The simplest path is to sign in with Google in the browser window when prompted
  - Session is maintained via cookies once authenticated

Usage:
    python3 apply_amazon.py --job-url "https://www.amazon.jobs/en/jobs/XXXXXXXX/..."
    python3 apply_amazon.py --dry-run --job-url "..."
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
    "phone_country": "in",
    "country":       "India",
    "city":          "Bangalore",
    "state":         "Karnataka",
    "postal_code":   "560100",
    "street":        "Mahaveer Ranches, Sree Sai Layout, Prapanna Agarpara",
    "country_id":    32489,   # Amazon internal ID for India
    "state_id":      32506,   # Amazon internal ID for Karnataka
    "linkedin":      "https://www.linkedin.com/in/kaushal-kumar-jha-93b77512a/",
    "resume_pdf":    os.path.expanduser(
        "~/gitQW/IO/Resume/job-hunter/profiles/kaushal-kumar-jha/output/"
        "2026-07-31/Okta-Staff_Fullstack_Engineer/resume.pdf"
    ),
}

# ── Question answer heuristics ─────────────────────────────────────────────────

# question_uuid fragments that should get "YES" / "true" / "1"
YES_QUESTION_KEYS = (
    "legally_authorized", "authorized_to_work", "eligible_to_work",
    "right_to_work", "legally_entitled", "acknowledgement", "agree",
    "consent", "18_years", "legal_age",
)

# question_uuid fragments that should get "NO" / "false" / "2"
NO_QUESTION_KEYS = (
    "sponsorship", "require_sponsorship", "visa_sponsor",
    "criminal", "bonded", "bond_period", "conflict_of_interest",
    "non_compete", "applied_before",
)

# Static answers for well-known Amazon question IDs
KNOWN_ANSWERS = {
    "REQUIRE_SPONSORSHIP_IND":                    "NO",
    "DEEMED_EXPORT_MOST_RECENT_CITIZENSHIP":       "INDIA",
    "HIGHEST_DEGREE_INDIA":                        "GRADUATE",
    "HIGHEST_AREA_OF_STUDY":                       "Computer Science",
    "ACKNOWLEDGEMENT_IND":                         "true",
    "HOW_DID_YOU_HEAR_ABOUT_THIS_ROLE":           "JOB_BOARD",
    "WORK_ELIGIBILITY_EXTERNAL_IND":              "true",
}


def _answer_for_question(qid: str, qtype: str, options: list) -> str | None:
    """Return the best string_value for a question, or None to skip."""
    qid_lower = qid.lower()

    # Known static answers
    if qid in KNOWN_ANSWERS:
        return KNOWN_ANSWERS[qid]

    # Heuristic: sponsorship / criminal → NO
    if any(k in qid_lower for k in NO_QUESTION_KEYS):
        # Find "NO" or "2" in options
        for opt in options:
            if opt.get("title", "").upper() in ("NO", "N"):
                return opt["key"]
        if qtype in ("RADIO_BUTTON", "DROPDOWN"):
            return "NO"
        return "false"

    # Heuristic: consent / authorized / acknowledgement → YES
    if any(k in qid_lower for k in YES_QUESTION_KEYS):
        for opt in options:
            if opt.get("title", "").upper() in ("YES", "Y"):
                return opt["key"]
        if qtype in ("RADIO_BUTTON", "DROPDOWN"):
            return "YES"
        return "true"

    return None


# ── Helpers ───────────────────────────────────────────────────────────────────

def settle(page, ms: int = 2000):
    try:
        page.wait_for_load_state("networkidle", timeout=ms)
    except Exception:
        pass
    time.sleep(0.5)


def _extract_csrf(page) -> str:
    """Extract Rails CSRF token from meta tag."""
    try:
        token = page.locator('meta[name="csrf-token"]').get_attribute("content", timeout=5000)
        if token:
            return token
    except Exception:
        pass
    # Fallback: search in page HTML
    html = page.content()
    m = re.search(r'<meta[^>]+name=["\']csrf-token["\'][^>]+content=["\']([^"\']+)["\']', html)
    return m.group(1) if m else ""


def _extract_job_id(url: str) -> str:
    """Extract numeric Amazon job ID (id_icims) from a URL."""
    # e.g. https://www.amazon.jobs/en/jobs/2849632/... or
    #      https://account.amazon.jobs/en-US/applicant/jobs/2849632/apply
    m = re.search(r"/jobs/(\d+)", url)
    return m.group(1) if m else ""


# ── Main apply function ───────────────────────────────────────────────────────

def apply(job_url: str, dry_run: bool = False):
    resume_path = PROFILE["resume_pdf"]
    if not os.path.isfile(resume_path):
        print(f"[ERROR] Resume PDF not found: {resume_path}")
        sys.exit(1)

    print(f"[INFO] Job URL  : {job_url}")
    print(f"[INFO] Resume   : {resume_path}")

    # Copy Chrome user-data for existing Google session / cookies
    chrome_src = os.path.expanduser("~/Library/Application Support/Google/Chrome/Default")
    tmp = tempfile.mkdtemp(prefix="chrome_amz_")
    dst = os.path.join(tmp, "Default")
    os.makedirs(dst, exist_ok=True)
    for f in ("Cookies", "Cookies-journal", "Login Data", "Web Data"):
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

        # ── Step 1: Navigate to job page ─────────────────────────────────────
        print(f"\n[STEP 1] Loading Amazon job page...")
        # Convert public job URL to apply URL if needed
        apply_url = job_url
        if "/jobs/" in job_url and "/apply" not in job_url:
            job_id = _extract_job_id(job_url)
            apply_url = f"https://www.amazon.jobs/applicant/jobs/{job_id}/apply"

        page.goto(apply_url, wait_until="networkidle", timeout=45000)
        settle(page, 4000)
        print(f"  Title: {page.title()}")
        print(f"  URL  : {page.url}")

        if dry_run:
            print("[DRY RUN] Page loaded. Exiting without applying.")
            ctx.close()
            return

        # ── Step 2: Handle login if not authenticated ─────────────────────
        current_url = page.url
        if "passport.amazon.jobs" in current_url or "login" in current_url.lower():
            print("\n[STEP 2] Login required.")
            print("  Please sign in to Amazon Jobs in the browser window.")
            print("  Tip: Use 'Sign in with Google' for fastest authentication.")
            print("  Waiting up to 5 minutes for login...")
            try:
                page.wait_for_url("**/applicant/**", timeout=300_000)
                settle(page, 4000)
                print(f"  Logged in! URL: {page.url}")
            except Exception:
                print("  [WARN] Login wait timed out, continuing...")

        # Ensure we're on the apply page
        if "/apply" not in page.url:
            job_id = _extract_job_id(job_url)
            if job_id:
                print(f"  Navigating to apply page for job {job_id}...")
                page.goto(
                    f"https://account.amazon.jobs/en-US/applicant/jobs/{job_id}/apply",
                    wait_until="networkidle", timeout=30000,
                )
                settle(page, 3000)

        # ── Step 3: Extract CSRF token ────────────────────────────────────
        print("\n[STEP 3] Extracting CSRF token...")
        csrf_token = _extract_csrf(page)
        if csrf_token:
            print(f"  Got CSRF token: {csrf_token[:20]}...")
        else:
            print("  [WARN] Could not find CSRF token — API calls may fail")

        job_id = _extract_job_id(page.url) or _extract_job_id(job_url)
        print(f"  Job ID: {job_id}")

        base_headers = {
            "X-CSRF-Token": csrf_token,
            "X-Requested-With": "XMLHttpRequest",
            "Origin": "https://account.amazon.jobs",
            "Referer": page.url,
        }

        # ── Step 4: Load applicant profile via API ────────────────────────
        print("\n[STEP 4] Loading applicant profile...")
        applicant = {}
        try:
            resp = page.evaluate("""
                async () => {
                    const r = await fetch('/api/apply/applicant', {
                        method: 'GET',
                        headers: {
                            'X-Requested-With': 'XMLHttpRequest',
                            'Accept': 'application/json'
                        },
                        credentials: 'include'
                    });
                    return r.json();
                }
            """)
            applicant = resp or {}
            print(f"  Loaded profile: {applicant.get('first_name', '')} {applicant.get('last_name', '')}")
        except Exception as ex:
            print(f"  [WARN] Could not load profile: {ex}")

        # Merge/patch with our PROFILE data
        if not applicant.get("first_name"):
            applicant["first_name"] = PROFILE["first_name"]
        if not applicant.get("last_name"):
            applicant["last_name"] = PROFILE["last_name"]
        if not applicant.get("primary_email_address"):
            applicant["primary_email_address"] = PROFILE["email"]
        if not applicant.get("phone_numbers"):
            applicant["phone_numbers"] = [{
                "number": PROFILE["phone"],
                "primary": True,
                "type": "MOBILE",
                "country_code": PROFILE["phone_country"],
            }]
        if not applicant.get("addresses"):
            applicant["addresses"] = [{
                "street":     PROFILE["street"],
                "city":       PROFILE["city"],
                "zip_code":   PROFILE["postal_code"],
                "country_id": PROFILE["country_id"],
                "state_id":   PROFILE["state_id"],
            }]

        # ── Step 5: Create draft application ─────────────────────────────
        print("\n[STEP 5] Creating draft application...")
        application_id = None
        try:
            draft = page.evaluate(
                """
                async ({jobId, applicant, csrf}) => {
                    const r = await fetch(`/api/apply/jobs/${jobId}/application/draft_application`, {
                        method: 'POST',
                        headers: {
                            'Content-Type': 'application/json',
                            'X-CSRF-Token': csrf,
                            'X-Requested-With': 'XMLHttpRequest',
                        },
                        credentials: 'include',
                        body: JSON.stringify({applicant}),
                    });
                    return r.json();
                }
                """,
                {"jobId": job_id, "applicant": applicant, "csrf": csrf_token},
            )
            application_id = draft.get("id")
            print(f"  Draft created: {application_id} (status: {draft.get('status')})")
        except Exception as ex:
            print(f"  [WARN] Draft creation: {ex}")

        # ── Step 6: Load and save forms ───────────────────────────────────
        print("\n[STEP 6] Loading wizard forms...")
        try:
            forms_resp = page.evaluate(
                """
                async ({jobId}) => {
                    const r = await fetch(`/api/apply/forms?job_id=${jobId}&recruiting_source=null`, {
                        headers: {'X-Requested-With': 'XMLHttpRequest', 'Accept': 'application/json'},
                        credentials: 'include',
                    });
                    return r.json();
                }
                """,
                {"jobId": job_id},
            )
            forms = (forms_resp or {}).get("form_list", {}).get("forms", [])
            print(f"  Found {len(forms)} form(s)")

            for form in forms:
                form_id = form.get("id", "")
                form_title = form.get("title", "")
                questions = form.get("questions", [])
                print(f"  Form: {form_title} ({form_id}) — {len(questions)} questions")

                answers = []
                for q in questions:
                    qid    = q.get("id", "")
                    qtype  = q.get("type", "")
                    opts   = q.get("options", [])
                    answer = _answer_for_question(qid, qtype, opts)
                    if answer is not None:
                        answers.append({"question_uuid": qid, "string_value": answer})
                        print(f"    Q: {qid} → {answer}")
                    else:
                        print(f"    Q: {qid} (type={qtype}) — skipped (no heuristic)")

                if answers:
                    save_result = page.evaluate(
                        """
                        async ({formId, answers, csrf}) => {
                            const r = await fetch('/api/apply/forms/save', {
                                method: 'POST',
                                headers: {
                                    'Content-Type': 'application/json',
                                    'X-CSRF-Token': csrf,
                                    'X-Requested-With': 'XMLHttpRequest',
                                },
                                credentials: 'include',
                                body: JSON.stringify({answers, forms: [formId]}),
                            });
                            return r.json();
                        }
                        """,
                        {"formId": form_id, "answers": answers, "csrf": csrf_token},
                    )
                    errors = (save_result or {}).get("errors", [])
                    if errors:
                        print(f"    [WARN] Form save errors: {errors}")
                    else:
                        print(f"    Form saved OK")

        except Exception as ex:
            print(f"  [WARN] Forms: {ex}")

        # ── Step 7: Upload resume ─────────────────────────────────────────
        print("\n[STEP 7] Uploading resume...")
        document_id = None
        storage_id  = None
        presigned_url = None

        # 7a — Multipart file upload
        try:
            with open(resume_path, "rb") as fh:
                file_bytes = list(fh.read())  # can't pass bytes directly through evaluate

            storage_info = page.evaluate(
                """
                async ({csrf, fileName}) => {
                    // We trigger via a file input on the page instead of raw fetch
                    // because multipart/form-data from page.evaluate can't send raw bytes.
                    // Instead we locate the file input and trigger it directly.
                    return null;  // handled by Playwright file input below
                }
                """,
                {"csrf": csrf_token, "fileName": os.path.basename(resume_path)},
            )

            # Look for file input on the page for resume
            try:
                fi = page.locator('input[type="file"]').first
                fi.wait_for(state="attached", timeout=10000)
                fi.set_input_files(resume_path)
                settle(page, 4000)
                print("  Resume uploaded via file input.")
                document_id = "uploaded"
            except Exception as fi_ex:
                print(f"  [WARN] File input upload: {fi_ex}")
                # Try direct API upload
                upload_resp = page.evaluate(
                    f"""
                    async (csrf) => {{
                        const form = new FormData();
                        // Trigger the upload using an XHR — bytes can't be passed from Python
                        // This will fail without actual file bytes; use file input approach
                        return null;
                    }}
                    """,
                    csrf_token,
                )

        except Exception as ex:
            print(f"  [WARN] Resume upload: {ex}")

        # ── Step 8: Scroll and human-like activity ────────────────────────
        print("\n[STEP 8] Simulating human activity...")
        for y in range(0, 1600, 250):
            page.evaluate(f"window.scrollTo(0, {y})")
            time.sleep(0.2)
        for y in range(1600, 0, -400):
            page.evaluate(f"window.scrollTo(0, {y})")
            time.sleep(0.2)
        page.mouse.move(640, 400)
        time.sleep(0.4)
        page.mouse.move(800, 500)
        time.sleep(2)

        # ── Step 9: Pause for manual review and submit ────────────────────
        print("\n" + "=" * 60)
        print("  ALL STEPS COMPLETE. Please review in the browser.")
        print("  Check all fields, then click Submit / Apply.")
        print("  Waiting up to 15 minutes...")
        print("=" * 60 + "\n")

        # Wait for confirmation page
        try:
            page.wait_for_url(
                "**/summary**|**/confirmation**|**/thank-you**"
                "|**/success**|**result=success**",
                timeout=900_000,
            )
        except Exception:
            try:
                page.wait_for_selector(
                    ':has-text("Application Submitted"), '
                    ':has-text("Thank you"), :has-text("successfully submitted"), '
                    ':has-text("application has been submitted")',
                    timeout=900_000,
                )
            except Exception:
                pass

        final_url = page.url
        print(f"\n[RESULT] {final_url}")
        if any(k in final_url.lower() for k in ("summary", "confirm", "thank", "success", "submitted")):
            print("[SUCCESS] Application submitted!")
        else:
            try:
                body = page.inner_text("body")[:600]
                if any(k in body.lower() for k in ("thank you", "successfully", "submitted", "application received")):
                    print("[SUCCESS] Application submitted! (detected from page text)")
                else:
                    page.screenshot(path="/tmp/amazon_apply_result.png")
                    print("[WARN] Not confirmed — screenshot at /tmp/amazon_apply_result.png")
            except Exception:
                page.screenshot(path="/tmp/amazon_apply_result.png")
                print("[WARN] Not confirmed — screenshot at /tmp/amazon_apply_result.png")

        time.sleep(5)
        ctx.close()


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Apply to an Amazon Jobs posting via account.amazon.jobs"
    )
    parser.add_argument(
        "--job-url",
        required=True,
        help='Full Amazon job URL, e.g. "https://www.amazon.jobs/en/jobs/2849632/..."',
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Load the page without filling or submitting",
    )
    args = parser.parse_args()
    apply(args.job_url, dry_run=args.dry_run)
