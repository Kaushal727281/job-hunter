#!/usr/bin/env python3
"""
apply_okta.py
-------------
Automates job application on Okta's careers site (Drupal + Greenhouse embed).

Flow (from HAR analysis):
  1. GET  job page → extract form_build_id + question field names
  2. POST resume file → get fid + fid_token
  3. POST cover letter file (optional) → get fid + fid_token
  4. POST final form submission (multipart/form-data) → 303 → /thankyou/

Usage:
    python3 apply_okta.py --job-url "https://www.okta.com/company/careers/opportunity/8064490?gh_jid=8064490"
    python3 apply_okta.py --dry-run    # parse form only, do not submit
"""

import argparse
import json
import os
import re
import sys
import time

import dotenv
dotenv.load_dotenv(os.path.join(os.path.dirname(__file__), ".env"), override=False)

def _e(key: str, default: str = "") -> str:
    return os.environ.get(key, default)

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
from playwright_stealth import Stealth

# ── Candidate profile ────────────────────────────────────────────────────────

PROFILE = {
    "first_name": f"{_e('CANDIDATE_FIRST_NAME')} {_e('CANDIDATE_MIDDLE_NAME')}".strip(),
    "last_name":  _e("CANDIDATE_LAST_NAME"),
    "email":      _e("CANDIDATE_EMAIL"),
    "phone":      _e("CANDIDATE_PHONE"),
    "linkedin":   _e("CANDIDATE_LINKEDIN"),
    # Resume PDF path — adjust to current profile PDF
    "resume_pdf": os.path.expanduser(_e("CANDIDATE_RESUME_PDF")),
    "cover_letter_pdf": None,   # set to a path or leave None to skip
}

# Compliance defaults (from HAR: disability=2 decline, veteran=1 not veteran, race=2 decline, gender=1 male)
COMPLIANCE = {
    "disability_status": "2",   # 2 = I don't wish to answer
    "veteran_status":    "1",   # 1 = I am not a protected veteran
    "race":              "2",   # 2 = I don't wish to answer
    "gender":            "1",   # 1 = Male
}

# ── Helpers ───────────────────────────────────────────────────────────────────

def normalize_job_url(url: str) -> str:
    """Ensure the URL ends with a trailing slash (required by Drupal)."""
    base = url.split("?")[0].rstrip("/") + "/"
    return base


def extract_form_meta(page_html: str) -> dict:
    """Parse form_build_id and question field names from page HTML."""
    meta = {}

    m = re.search(r'name="form_build_id"\s+value="([^"]+)"', page_html)
    if m:
        meta["form_build_id"] = m.group(1)

    # Question fields: name="question_NNNN" or name="question_NNNN[NNNNN]"
    questions = re.findall(r'name="(question_\d+(?:\[\d+\])?)"', page_html)
    meta["questions"] = list(dict.fromkeys(questions))  # dedup, preserve order

    # reCAPTCHA site key
    m2 = re.search(r'data-sitekey="([^"]+)"', page_html)
    if m2:
        meta["recaptcha_sitekey"] = m2.group(1)

    return meta


def build_answer_map(question_fields: list, linkedin_url: str) -> dict:
    """
    Map question field names to answers.
    LinkedIn URL goes into the first question that accepts a URL.
    Everything else defaults to empty / reasonable values.
    """
    answers = {}
    for field in question_fields:
        if not answers:
            # First question is typically LinkedIn
            answers[field] = linkedin_url
        else:
            answers[field] = ""
    return answers


# ── Main automation ───────────────────────────────────────────────────────────

def apply(job_url: str, dry_run: bool = False):
    resume_path = PROFILE["resume_pdf"]
    if not os.path.exists(resume_path):
        print(f"[ERROR] Resume PDF not found: {resume_path}")
        sys.exit(1)

    job_base_url = normalize_job_url(job_url)
    print(f"[INFO] Job URL: {job_base_url}")

    with sync_playwright() as pw:
        # Launch with a persistent Chrome profile so reCAPTCHA v3 sees real
        # Google cookies and gives a high trust score.
        # Copy only the Cookies file (a few MB) — not the full 400 MB profile.
        import shutil, tempfile
        src_profile = os.path.expanduser("~/Library/Application Support/Google/Chrome/Default")
        tmp_profile  = tempfile.mkdtemp(prefix="chrome_okta_")
        dest_default = os.path.join(tmp_profile, "Default")
        os.makedirs(dest_default, exist_ok=True)
        for fname in ("Cookies", "Cookies-journal", "Local State"):
            src = os.path.join(src_profile, fname)
            if os.path.isfile(src):
                shutil.copy2(src, os.path.join(dest_default, fname))
                print(f"[INFO] Copied {fname} from Chrome profile")
        try:
            ctx = pw.chromium.launch_persistent_context(
                user_data_dir=tmp_profile,
                headless=False,
                channel="chrome",
                viewport={"width": 1280, "height": 900},
                args=["--disable-blink-features=AutomationControlled"],
            )
        except Exception:
            ctx = pw.chromium.launch_persistent_context(
                user_data_dir=tmp_profile,
                headless=False,
                viewport={"width": 1280, "height": 900},
                args=["--disable-blink-features=AutomationControlled"],
            )
        page = ctx.new_page()
        Stealth().apply_stealth_sync(page)

        # ── Step 1: Load job page ─────────────────────────────────────────────
        print("[STEP 1] Loading job page...")
        page.goto(job_base_url, wait_until="networkidle", timeout=30000)
        time.sleep(2)

        html = page.content()
        meta = extract_form_meta(html)

        if not meta.get("form_build_id"):
            print("[ERROR] Could not find form_build_id — page may not have loaded correctly.")
            print("        Saving page HTML for inspection...")
            with open("/tmp/okta_job_page.html", "w") as f:
                f.write(html)
            ctx.close()
            sys.exit(1)

        print(f"  form_build_id : {meta['form_build_id']}")
        print(f"  questions     : {meta['questions']}")
        print(f"  recaptcha key : {meta.get('recaptcha_sitekey','not found')}")

        if dry_run:
            print("[DRY RUN] Stopping before submission.")
            ctx.close()
            return

        # ── Step 2: Fill personal info fields ────────────────────────────────
        print("[STEP 2] Filling personal info...")
        page.fill('input[name="first_name"]', PROFILE["first_name"])
        page.fill('input[name="last_name"]',  PROFILE["last_name"])
        page.fill('input[name="email"]',       PROFILE["email"])
        page.fill('input[name="phone"]',       PROFILE["phone"])

        # ── Step 3: Upload resume ─────────────────────────────────────────────
        print("[STEP 3] Uploading resume...")
        # Find the resume file input (hidden, triggered by button)
        resume_input = page.locator('input[type="file"]').first
        resume_input.set_input_files(resume_path)

        # Wait for the upload AJAX to complete
        time.sleep(4)
        print("  Resume uploaded.")

        time.sleep(1)

        # ── Step 4: Upload cover letter (optional) ────────────────────────────
        if PROFILE.get("cover_letter_pdf") and os.path.exists(PROFILE["cover_letter_pdf"]):
            print("[STEP 4] Uploading cover letter...")
            cover_inputs = page.locator('input[type="file"]').all()
            if len(cover_inputs) > 1:
                cover_inputs[1].set_input_files(PROFILE["cover_letter_pdf"])
                time.sleep(3)
                print("  Cover letter uploaded.")
        else:
            print("[STEP 4] Skipping cover letter.")

        # ── Step 5: Dismiss overlays then fill question fields ───────────────
        print("[STEP 5] Filling question fields...")

        # Dismiss cookie consent banner if present
        try:
            page.locator("#onetrust-accept-btn-handler, .accept-btn, [id*='accept']").first.click(timeout=3000)
            time.sleep(0.5)
        except Exception:
            pass

        # Scroll down so the sticky header doesn't block clicks
        page.evaluate("window.scrollTo(0, 300)")
        time.sleep(0.5)

        answer_map = build_answer_map(meta["questions"], PROFILE["linkedin"])
        for field, value in answer_map.items():
            try:
                locator = page.locator(f'[name="{field}"]').first
                tag = locator.evaluate("el => el.tagName.toLowerCase()")
                if tag == "select":
                    # Read the question label to decide Yes vs No
                    field_id = locator.get_attribute("id") or ""
                    q_label = ""
                    # Label is usually in a preceding <label> or wrapping element
                    try:
                        # Drupal wraps fields in .form-item; the label is a sibling
                        q_label = locator.evaluate(
                            "el => {"
                            "  let p = el.closest('.form-item, .js-form-item');"
                            "  if (p) { let l = p.querySelector('label'); if (l) return l.innerText; }"
                            "  return '';"
                            "}"
                        ).lower()
                    except Exception:
                        pass
                    print(f"  [SELECT] {field} label='{q_label[:80]}'")
                    # Questions that need "Yes"
                    yes_keywords = ("legally authorized", "authorized to work", "eligible to work")
                    if any(kw in q_label for kw in yes_keywords):
                        try:
                            locator.select_option(label="Yes")
                        except Exception:
                            locator.select_option(value="yes")
                    else:
                        # Everything else (past employment, conflicts, outside activity) → No
                        try:
                            locator.select_option(label="No")
                        except Exception:
                            try:
                                locator.select_option(value="no")
                            except Exception:
                                locator.select_option(index=1)
                elif tag == "input":
                    input_type = locator.get_attribute("type") or "text"
                    if input_type in ("checkbox", "radio"):
                        # Inspect label text to decide whether to check this field.
                        field_id = locator.get_attribute("id") or ""
                        label_text = ""
                        if field_id:
                            try:
                                label_text = page.locator(f'label[for="{field_id}"]').first.inner_text(timeout=1000).lower()
                            except Exception:
                                pass
                        # Visa sponsorship → answer is No; leave checkbox unchecked
                        visa_keywords = ("visa", "sponsor", "sponsorship", "work authorization", "work permit")
                        if any(kw in label_text for kw in visa_keywords):
                            print(f"  [SKIP] Visa/sponsorship checkbox '{field}' — answering No (leave unchecked)")
                            continue
                        # Scroll element into view, then JS-click to bypass overlays
                        locator.evaluate("el => el.scrollIntoView({block:'center'})")
                        time.sleep(0.3)
                        locator.evaluate("el => el.click()")
                    else:
                        locator.fill(value)
                elif tag == "textarea":
                    locator.fill(value)
            except Exception as ex:
                print(f"  [WARN] Could not fill {field}: {ex}")

        # ── Step 6: Compliance section ────────────────────────────────────────
        print("[STEP 6] Setting compliance answers...")
        for section, val in COMPLIANCE.items():
            try:
                sel = f'select[name="compliance_section[{section}][0]"]'
                page.locator(sel).select_option(val)
            except Exception:
                pass   # Not all jobs show all compliance fields

        # ── Step 7: Human-like behaviour to improve reCAPTCHA v3 score ──────
        print("[STEP 7] Simulating human activity for reCAPTCHA v3 score...")
        # Scroll slowly down the page then back up — mimics reading the form
        for y in range(300, 1800, 200):
            page.evaluate(f"window.scrollTo(0, {y})")
            time.sleep(0.3)
        for y in range(1800, 300, -300):
            page.evaluate(f"window.scrollTo(0, {y})")
            time.sleep(0.2)
        page.evaluate("window.scrollTo(0, 0)")
        time.sleep(1)
        # Move mouse around the form area
        page.mouse.move(640, 400)
        time.sleep(0.3)
        page.mouse.move(900, 600)
        time.sleep(0.3)
        page.mouse.move(700, 500)
        time.sleep(3)

        # Scroll to the Submit button so it's visible
        submit_btn = page.locator('input[value="Submit Application"], button[type="submit"]').first
        submit_btn.evaluate("el => el.scrollIntoView({block:'center'})")
        time.sleep(1)

        print("\n" + "="*60)
        print("  ALL FIELDS FILLED. Please click 'Submit Application'")
        print("  in the browser window. Waiting up to 5 minutes...")
        print("="*60 + "\n")

        # Wait for the user to manually click Submit — reCAPTCHA passes
        # because the click is a real human action.
        try:
            page.wait_for_url("**/thankyou/**", timeout=300000)   # 5 min
            final_url = page.url
        except Exception:
            final_url = page.url

        print(f"\n[RESULT] Final URL: {final_url}")

        if "thankyou" in final_url or "thank" in final_url.lower():
            print("[SUCCESS] Application submitted! Redirected to thank-you page.")
        else:
            print("[WARN] Did not reach thank-you page — may not have been submitted yet.")
            page.screenshot(path="/tmp/okta_apply_result.png")
            print("  Screenshot saved to /tmp/okta_apply_result.png")

        time.sleep(5)
        ctx.close()


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Apply to an Okta job automatically.")
    parser.add_argument(
        "--job-url",
        default="https://www.okta.com/company/careers/opportunity/8064490?gh_jid=8064490",
        help="Full Okta job URL (default: Fullstack Staff Software Engineer - PAM, Bengaluru)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse the form and print field info without submitting.",
    )
    args = parser.parse_args()
    apply(args.job_url, dry_run=args.dry_run)
