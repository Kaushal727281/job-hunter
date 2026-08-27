#!/usr/bin/env python3
"""
apply_lever.py
--------------
Applies to Lever ATS jobs via Playwright browser automation.

Features:
  • Skips jobs already applied in local job store (applied_at set)
  • Checks the actual portal — if "You've already applied" is shown, marks it
    applied in job store and skips it
  • Fills standard fields: name, email, phone, company, location, LinkedIn
  • Uploads resume PDF
  • Answers custom question cards (text, textarea, select, radio)
  • hCaptcha: runs in HEADFUL mode so user can solve it manually (120s window)
  • Marks successful applications in job store

Usage:
    python apply_lever.py                         # all lever jobs score >= 7
    python apply_lever.py --min-score 8
    python apply_lever.py --company "Meesho"
    python apply_lever.py --lever-job-id <uuid>   # specific job by Lever UUID
    python apply_lever.py --dry-run               # print what would be applied
    python apply_lever.py --limit 5               # max N jobs
    python apply_lever.py --headless              # force headless (no captcha solve)
"""

import argparse
import logging
import os
import re
import sys
import time

import truststore
import urllib3

truststore.inject_into_ssl()
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

# ── Candidate profile (loaded from .env) ──────────────────────────────────────

import dotenv as _dotenv
_dotenv.load_dotenv(os.path.join(os.path.dirname(__file__), ".env"), override=False)

def _e(key: str, default: str = "") -> str:
    return os.environ.get(key, default)

_fn = f"{_e('CANDIDATE_FIRST_NAME')} {_e('CANDIDATE_MIDDLE_NAME')}".strip()
PROFILE = {
    "name":       f"{_fn} {_e('CANDIDATE_LAST_NAME')}".strip(),
    "first_name": _fn,
    "last_name":  _e("CANDIDATE_LAST_NAME"),
    "email":      _e("CANDIDATE_EMAIL"),
    "phone":      _e("CANDIDATE_PHONE_E164"),
    "org":        _e("CANDIDATE_ORG"),
    "location":   _e("CANDIDATE_LOCATION"),
    "linkedin":   _e("CANDIDATE_LINKEDIN"),
    "website":    _e("CANDIDATE_GITHUB"),
    "resume_pdf": os.path.expanduser(_e("CANDIDATE_RESUME_PDF")),
}

# Answer keywords for yes/no questions
_YES_KEYWORDS = (
    "legally authorized", "authorized to work", "eligible to work",
    "right to work", "sponsorship not required", "can you work",
    "are you willing", "willing to relocate",
)
_NO_KEYWORDS = (
    "require visa", "require sponsorship", "need sponsorship",
    "need visa", "currently on visa",
)

# Common multi-choice answer preferences (lowercased fragment → preferred answer fragment)
_PREF_ANSWERS = {
    "years of experience":  "5",
    "notice period":        "60",      # 60 days / 2 months
    "current salary":       "skip",    # try to skip if optional
    "expected salary":      "skip",
    "gender":               "male",
    "pronouns":             "he/him",
    "race":                 "decline",
    "ethnicity":            "decline",
    "disability":           "decline",
    "veteran":              "not",
    "highest degree":       "bachelor",
    "education":            "bachelor",
    "degree":               "bachelor",
}

CAPTCHA_WAIT_SEC = 120   # seconds to wait for user to solve hCaptcha


# ── Browser helpers ───────────────────────────────────────────────────────────

def _launch_browser(playwright, headless: bool = False):
    """Launch a persistent Chrome context with stealth."""
    import tempfile
    from playwright_stealth import Stealth

    tmp_dir = tempfile.mkdtemp(prefix="lever_apply_")
    try:
        ctx = playwright.chromium.launch_persistent_context(
            tmp_dir,
            channel="chrome",
            headless=headless,
            args=["--disable-blink-features=AutomationControlled"],
            viewport={"width": 1280, "height": 900},
        )
    except Exception:
        ctx = playwright.chromium.launch_persistent_context(
            tmp_dir,
            headless=headless,
            args=["--disable-blink-features=AutomationControlled"],
            viewport={"width": 1280, "height": 900},
        )
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    Stealth().apply_stealth_sync(page)
    return ctx, page


def _settle(page, ms: int = 1500):
    """Wait for network idle or timeout."""
    try:
        page.wait_for_load_state("networkidle", timeout=ms)
    except Exception:
        pass
    time.sleep(0.5)


# ── Already-applied check ──────────────────────────────────────────────────────

_ALREADY_APPLIED_PHRASES = (
    "you've already applied",
    "you have already applied",
    "already submitted",
    "application already submitted",
    "duplicate application",
    "already applied to this position",
)


def check_already_applied_on_portal(page, apply_url: str) -> bool:
    """
    Load the apply page and check if Lever shows an "already applied" message.
    Returns True if the portal indicates a previous application exists.
    """
    try:
        page.goto(apply_url, timeout=20000, wait_until="domcontentloaded")
        _settle(page, 3000)
        body = (page.locator("body").inner_text(timeout=3000) or "").lower()
        for phrase in _ALREADY_APPLIED_PHRASES:
            if phrase in body:
                return True
        # Also check page URL for redirect to a "thank you" page
        cur_url = page.url.lower()
        if any(k in cur_url for k in ("confirmation", "thankyou", "thank-you", "submitted")):
            return True
    except Exception as exc:
        logger.debug(f"already-applied check error: {exc}")
    return False


# ── Smart answer helpers ───────────────────────────────────────────────────────

def _best_select_option(options: list[str], label_text: str) -> str | None:
    """
    Given a list of <option> texts and the question label, pick the best answer.
    Returns the option text to select, or None to skip.
    """
    label_low = label_text.lower()
    opts_low  = [o.lower() for o in options]

    # Yes/No questions
    is_yes_q = any(kw in label_low for kw in _YES_KEYWORDS)
    is_no_q  = any(kw in label_low for kw in _NO_KEYWORDS)
    if is_yes_q or is_no_q:
        want_yes = is_yes_q and not is_no_q
        for opt, opt_low in zip(options, opts_low):
            if want_yes and any(k in opt_low for k in ("yes", "i am", "i can", "i do")):
                return opt
            if not want_yes and any(k in opt_low for k in ("no", "i am not", "i cannot")):
                return opt

    # Preference-based
    for key, pref in _PREF_ANSWERS.items():
        if key in label_low:
            if pref == "skip":
                return None
            for opt, opt_low in zip(options, opts_low):
                if pref in opt_low:
                    return opt

    # Fallback: first non-empty, non-placeholder option
    for opt in options:
        opt_stripped = opt.strip()
        if opt_stripped and opt_stripped.lower() not in ("", "select", "please select",
                                                          "choose one", "--"):
            return opt
    return None


def _best_text_answer(label_text: str) -> str:
    """Return a sensible text answer for free-text Lever fields."""
    label_low = label_text.lower()
    if any(k in label_low for k in ("linkedin", "linkedin url", "linkedin profile")):
        return PROFILE["linkedin"]
    if any(k in label_low for k in ("website", "portfolio", "github", "url")):
        return PROFILE["website"]
    if any(k in label_low for k in ("city", "current city", "location")):
        return "Bengaluru"
    if any(k in label_low for k in ("notice", "notice period")):
        return "60 days"
    if any(k in label_low for k in ("salary", "ctc", "compensation")):
        return "As per industry standards"
    if any(k in label_low for k in ("year", "graduation", "passing year")):
        return "2019"
    if any(k in label_low for k in ("university", "college", "school", "institution")):
        return "KIIT University"
    if any(k in label_low for k in ("gpa", "cgpa", "grade")):
        return "8.0"
    if any(k in label_low for k in ("how did you hear", "referral", "source")):
        return "LinkedIn"
    if any(k in label_low for k in ("why", "cover letter", "tell us")):
        return (
            "I am an experienced software engineer with 6+ years at FICO building "
            "enterprise-grade Java/Spring Boot systems. I am excited about this role "
            "because it aligns with my background in scalable backend systems and my "
            "interest in impactful product engineering."
        )
    return ""


# ── Form filling ──────────────────────────────────────────────────────────────

def _fill_input(page, selector: str, value: str, label: str = ""):
    """Fill an input field, ignoring if not found."""
    if not value:
        return
    try:
        loc = page.locator(selector).first
        loc.wait_for(state="visible", timeout=5000)
        loc.triple_click(timeout=3000)
        loc.fill(value, timeout=3000)
        logger.debug(f"  filled {label or selector}")
    except Exception as exc:
        logger.debug(f"  [skip] {label or selector}: {exc}")


def _fill_standard_fields(page):
    """Fill the standard Lever apply form fields."""
    _fill_input(page, 'input[name="name"]',     PROFILE["name"],     "name")
    _fill_input(page, 'input[name="email"]',    PROFILE["email"],    "email")
    _fill_input(page, 'input[name="phone"]',    PROFILE["phone"],    "phone")
    _fill_input(page, 'input[name="org"]',      PROFILE["org"],      "org")
    _fill_input(page, 'input[name="location"]', PROFILE["location"], "location")

    # Social links section: linkedin, twitter, github, website
    for field_name, value in [
        ("urls[LinkedIn]",  PROFILE["linkedin"]),
        ("urls[GitHub]",    PROFILE["website"]),
        ("urls[Other]",     PROFILE["website"]),
        ("urls[Website]",   PROFILE["website"]),
        ("urls[Portfolio]", PROFILE["website"]),
    ]:
        try:
            loc = page.locator(f'input[name="{field_name}"]').first
            if loc.count() > 0 and loc.is_visible(timeout=500):
                loc.triple_click(timeout=2000)
                loc.fill(value, timeout=2000)
        except Exception:
            pass


def _upload_resume(page, resume_path: str) -> bool:
    """Upload the resume PDF. Returns True on success."""
    try:
        file_input = page.locator('input[type="file"]').first
        file_input.wait_for(state="attached", timeout=8000)
        file_input.set_input_files(resume_path, timeout=10000)
        time.sleep(1.5)
        logger.info("  [resume] uploaded")
        return True
    except Exception as exc:
        logger.warning(f"  [resume] upload failed: {exc}")
        return False


def _answer_custom_cards(page):
    """
    Answer Lever custom question cards.
    Cards use name pattern: cards[{uuid}][field0], cards[{uuid}][field1], ...
    """
    answered = 0

    # ── Text inputs (single-line) ──
    for inp in page.locator('input[name^="cards["]').all():
        try:
            name  = inp.get_attribute("name") or ""
            label = _get_field_label(page, inp)
            val   = _best_text_answer(label)
            if val and inp.is_visible(timeout=500):
                cur = inp.input_value(timeout=500) or ""
                if not cur.strip():
                    inp.triple_click(timeout=2000)
                    inp.fill(val, timeout=2000)
                    answered += 1
                    logger.debug(f"  card text filled: {label[:50]!r}")
        except Exception:
            pass

    # ── Textareas ──
    for ta in page.locator('textarea[name^="cards["]').all():
        try:
            label = _get_field_label(page, ta)
            val   = _best_text_answer(label)
            if val and ta.is_visible(timeout=500):
                cur = ta.input_value(timeout=500) or ""
                if not cur.strip():
                    ta.triple_click(timeout=2000)
                    ta.fill(val, timeout=2000)
                    answered += 1
                    logger.debug(f"  card textarea filled: {label[:50]!r}")
        except Exception:
            pass

    # ── Selects ──
    for sel in page.locator('select[name^="cards["]').all():
        try:
            label = _get_field_label(page, sel)
            options = sel.locator("option").all_inner_texts()
            best = _best_select_option(options, label)
            if best and sel.is_visible(timeout=500):
                sel.select_option(label=best, timeout=3000)
                answered += 1
                logger.debug(f"  card select: {label[:40]!r} → {best!r}")
        except Exception:
            pass

    # ── Radio buttons ──
    # Group by name, pick once per group
    radio_groups: dict[str, list] = {}
    for radio in page.locator('input[type="radio"][name^="cards["]').all():
        try:
            name = radio.get_attribute("name") or ""
            radio_groups.setdefault(name, []).append(radio)
        except Exception:
            pass
    for name, radios in radio_groups.items():
        try:
            label = _get_field_label(page, radios[0])
            # Determine want-yes vs want-no
            is_yes_q = any(kw in label.lower() for kw in _YES_KEYWORDS)
            is_no_q  = any(kw in label.lower() for kw in _NO_KEYWORDS)
            want_yes = is_yes_q and not is_no_q
            for radio in radios:
                if not radio.is_visible(timeout=300):
                    continue
                val = (radio.get_attribute("value") or "").lower()
                lbl_text = ""
                try:
                    rid = radio.get_attribute("id") or ""
                    if rid:
                        lbl_text = (page.locator(f'label[for="{rid}"]').inner_text(timeout=500) or "").lower()
                except Exception:
                    pass
                combined = val + " " + lbl_text
                if want_yes and any(k in combined for k in ("yes", "i am", "i can")):
                    radio.click(timeout=2000)
                    answered += 1
                    break
                if not want_yes and any(k in combined for k in ("no", "i am not")):
                    radio.click(timeout=2000)
                    answered += 1
                    break
        except Exception:
            pass

    logger.info(f"  [cards] answered {answered} custom question(s)")
    return answered


def _get_field_label(page, element) -> str:
    """Try to get the text label associated with a form element."""
    try:
        eid = element.get_attribute("id") or ""
        if eid:
            lbl = page.locator(f'label[for="{eid}"]').inner_text(timeout=500)
            if lbl:
                return lbl.strip()
    except Exception:
        pass
    try:
        # Walk up to nearest .application-field or .field wrapper and get its label/legend
        label = element.evaluate("""el => {
            let p = el.parentElement;
            for (let i = 0; i < 6; i++) {
                if (!p) break;
                let lbl = p.querySelector('label, legend, .field-label, .application-field-title');
                if (lbl) return lbl.innerText || '';
                p = p.parentElement;
            }
            return '';
        }""")
        return (label or "").strip()
    except Exception:
        return ""


# ── hCaptcha handling ─────────────────────────────────────────────────────────

def _wait_for_captcha_solve(page, headless: bool) -> bool:
    """
    If hCaptcha iframe is visible, wait for user to solve it (headful only).
    Returns True when captcha is gone or was never present.
    """
    try:
        captcha_frame = page.frame_locator('iframe[src*="hcaptcha"]').first
        # If the iframe doesn't exist this will throw — that's fine
        captcha_present = captcha_frame.locator(".challenge-container").count() > 0
    except Exception:
        captcha_present = False

    # Also check for hcaptcha by widget presence
    if not captcha_present:
        try:
            widget = page.locator('[id^="hcaptcha"]').first
            captcha_present = widget.is_visible(timeout=1000)
        except Exception:
            pass

    if not captcha_present:
        return True  # no captcha

    if headless:
        logger.warning("  [captcha] hCaptcha detected but running headless — cannot solve!")
        return False

    print("\n" + "=" * 60)
    print("  hCAPTCHA DETECTED — please solve it in the browser window")
    print(f"  You have {CAPTCHA_WAIT_SEC} seconds...")
    print("=" * 60)

    deadline = time.time() + CAPTCHA_WAIT_SEC
    while time.time() < deadline:
        time.sleep(2)
        try:
            # Check if captcha is gone (solved or dismissed)
            still_there = page.locator('iframe[src*="hcaptcha"]').is_visible(timeout=500)
            if not still_there:
                logger.info("  [captcha] solved (iframe gone)")
                return True
            # Check for h-captcha-response being filled
            resp_val = page.evaluate(
                "() => { const el = document.querySelector('[name=\"h-captcha-response\"]'); "
                "return el ? el.value : ''; }"
            )
            if resp_val:
                logger.info("  [captcha] solved (response token present)")
                return True
        except Exception:
            pass

    logger.warning(f"  [captcha] timeout after {CAPTCHA_WAIT_SEC}s — skipping this job")
    return False


# ── Main apply flow ───────────────────────────────────────────────────────────

def apply_one_job(page, job: dict, resume_path: str,
                  headless: bool, dry_run: bool) -> str:
    """
    Apply to one Lever job. Returns: "success" | "already_applied" | "skipped" | "error:<msg>"
    """
    apply_url = job.get("apply_link", "")
    title     = job["title"]
    company   = job["company"]

    print("\n" + "=" * 60)
    print(f"  JOB  : {title} @ {company}")
    print(f"  SCORE: {job.get('fit_score', '?')}/10")
    print(f"  URL  : {apply_url}")
    print("=" * 60)

    if dry_run:
        print("  [dry-run] would apply")
        return "skipped"

    # ── Step 1: check portal for already-applied ──────────────────────────────
    print("[STEP 1] Checking if already applied on portal...")
    if check_already_applied_on_portal(page, apply_url):
        print("  [already applied] detected on portal — marking in job store")
        return "already_applied"

    # ── Step 2: navigate to apply page ───────────────────────────────────────
    print("[STEP 2] Loading apply page...")
    try:
        page.goto(apply_url, timeout=25000, wait_until="domcontentloaded")
        _settle(page, 3000)
    except Exception as exc:
        return f"error:navigation failed — {exc}"

    # Double-check "already applied" after page load
    body = ""
    try:
        body = (page.locator("body").inner_text(timeout=3000) or "").lower()
    except Exception:
        pass
    for phrase in _ALREADY_APPLIED_PHRASES:
        if phrase in body:
            print("  [already applied] message found on page")
            return "already_applied"

    # Verify we're on a Lever apply form (not a redirect)
    cur_url = page.url
    if "lever.co" not in cur_url and "jobs.lever" not in cur_url:
        logger.warning(f"  [warn] Not on Lever page: {cur_url}")

    # ── Step 3: fill standard fields ─────────────────────────────────────────
    print("[STEP 3] Filling standard fields...")
    _fill_standard_fields(page)

    # ── Step 4: upload resume ─────────────────────────────────────────────────
    print("[STEP 4] Uploading resume...")
    _upload_resume(page, resume_path)
    time.sleep(1)

    # ── Step 5: answer custom cards ───────────────────────────────────────────
    print("[STEP 5] Answering custom question cards...")
    _answer_custom_cards(page)

    # ── Step 6: handle hCaptcha ───────────────────────────────────────────────
    print("[STEP 6] Checking for hCaptcha...")
    captcha_ok = _wait_for_captcha_solve(page, headless)
    if not captcha_ok:
        return "error:captcha not solved"

    # ── Step 7: submit ────────────────────────────────────────────────────────
    print("[STEP 7] Submitting application...")
    try:
        # Find the submit button
        submit_sel = (
            'button[type="submit"]:visible, '
            'button:has-text("Submit application"):visible, '
            'button:has-text("Apply"):visible'
        )
        btn = page.locator(submit_sel).first
        btn.wait_for(state="visible", timeout=8000)
        btn.scroll_into_view_if_needed(timeout=3000)
        btn.click(timeout=5000)
        _settle(page, 5000)
    except Exception as exc:
        return f"error:submit click failed — {exc}"

    # ── Step 8: verify confirmation ───────────────────────────────────────────
    print("[STEP 8] Verifying submission...")
    time.sleep(2)
    final_url  = page.url.lower()
    final_body = ""
    try:
        final_body = (page.locator("body").inner_text(timeout=4000) or "").lower()
    except Exception:
        pass

    success_phrases = (
        "thank you for applying", "application submitted", "thanks for applying",
        "we've received your application", "application received",
        "successfully submitted", "you will hear from us",
    )
    url_success = any(k in final_url for k in ("confirmation", "thankyou", "thank-you",
                                                "submitted", "success"))
    body_success = any(p in final_body for p in success_phrases)

    if url_success or body_success:
        print(f"  [SUCCESS] Application submitted for {title} @ {company}")
        return "success"

    # May still have succeeded — check for validation errors
    error_phrases = ("required field", "please fill", "invalid", "error")
    if any(e in final_body for e in error_phrases):
        logger.warning("  [warn] Possible form validation error on page")
        try:
            page.screenshot(path=f"/tmp/lever_error_{job.get('lever_job_id','?')[:8]}.png")
        except Exception:
            pass
        return "error:form validation errors present"

    # Assume success if no obvious error
    print(f"  [likely success] No confirmation text found but no errors either")
    return "success"


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Apply to Lever jobs via Playwright")
    parser.add_argument("--min-score",    type=int,   default=7,    help="Minimum fit score (default: 7)")
    parser.add_argument("--company",                                help="Filter by company name (substring)")
    parser.add_argument("--lever-job-id",                          help="Specific Lever job UUID")
    parser.add_argument("--limit",        type=int,   default=0,    help="Max number of jobs to apply to")
    parser.add_argument("--dry-run",      action="store_true",      help="Print jobs but don't apply")
    parser.add_argument("--headless",     action="store_true",      help="Run headless (disables captcha solve)")
    parser.add_argument("--profile",      default=os.environ.get("CANDIDATE_PROFILE_SLUG", ""), help="Profile slug")
    args = parser.parse_args()

    import profiles
    profiles.set_active_profile(args.profile)
    import job_store

    # ── Load jobs from store ──────────────────────────────────────────────────
    all_jobs = job_store.all_jobs()
    lever_jobs = [j for j in all_jobs if j.get("ats_type") == "lever"]

    if not lever_jobs:
        print("No Lever jobs in job store. Run fetch_lever.py first.")
        sys.exit(0)

    # ── Filter ────────────────────────────────────────────────────────────────
    if args.lever_job_id:
        lever_jobs = [j for j in lever_jobs if j.get("lever_job_id") == args.lever_job_id]
    elif args.company:
        needle = args.company.lower()
        lever_jobs = [j for j in lever_jobs if needle in j["company"].lower()]

    # Skip already applied in job store
    pending = [j for j in lever_jobs if not j.get("applied_at")]

    # Filter by fit score
    pending = [j for j in pending if (j.get("fit_score") or 0) >= args.min_score]

    # Sort best first
    pending = sorted(pending, key=lambda j: j.get("fit_score", 0), reverse=True)

    # ── Dedup: at most 1 job per (company, base-title) ─────────────────────
    # Prevents applying to multiple "Senior Software Engineer" variants at
    # the same company when they have different Lever UUIDs.
    def _base_title(t: str) -> str:
        t = re.split(r"\s*[-–,(/]", t)[0].strip().lower()
        return re.sub(r"\s+", " ", t)

    _already_applied_pairs = {
        (_base_title(j.get("title", "")), j.get("company", "").lower())
        for j in all_jobs
        if j.get("applied_at")
    }
    deduped: list = []
    seen_pairs: set = set(_already_applied_pairs)
    for j in pending:
        pair = (_base_title(j.get("title", "")), j.get("company", "").lower())
        if pair in seen_pairs:
            logger.info(
                f"  [dedup] Skipping '{j.get('title')}' @ {j.get('company')} "
                f"(already applied to same role)"
            )
            continue
        seen_pairs.add(pair)
        deduped.append(j)
    pending = deduped

    if args.limit:
        pending = pending[:args.limit]

    if not pending:
        print(f"No pending Lever jobs (score >= {args.min_score}) to apply to.")
        sys.exit(0)

    print(f"\nFound {len(pending)} Lever job(s) to apply to:\n")
    for j in pending:
        print(f"  [{j.get('fit_score','?')}/10] {j['title']} @ {j['company']}")
        print(f"    {j['apply_link']}")

    if args.dry_run:
        return

    resume_path = PROFILE["resume_pdf"]
    if not os.path.exists(resume_path):
        print(f"\n[ERROR] Resume not found: {resume_path}")
        sys.exit(1)

    # ── Apply ─────────────────────────────────────────────────────────────────
    from playwright.sync_api import sync_playwright

    applied_count  = 0
    skipped_count  = 0
    error_count    = 0

    with sync_playwright() as pw:
        ctx, page = _launch_browser(pw, headless=args.headless)
        try:
            for job in pending:
                job_id = job["id"]
                result = apply_one_job(page, job, resume_path,
                                       headless=args.headless, dry_run=False)

                if result == "success":
                    job_store.mark_applied(job_id, applied=True)
                    applied_count += 1
                elif result == "already_applied":
                    job_store.mark_applied(job_id, applied=True)
                    skipped_count += 1
                    print(f"  → Marked as applied in job store (was already on portal)")
                else:
                    err_msg = result.replace("error:", "")
                    job_store.mark_applied(job_id, applied=False, error=err_msg)
                    error_count += 1
                    logger.warning(f"  → Error: {err_msg}")

                # Small delay between applications
                if job != pending[-1]:
                    time.sleep(3)

        finally:
            ctx.close()

    # ── Summary ───────────────────────────────────────────────────────────────
    print("\n" + "=" * 60)
    print(f"  Lever apply complete")
    print(f"  Applied:           {applied_count}")
    print(f"  Already applied:   {skipped_count}")
    print(f"  Errors:            {error_count}")
    print("=" * 60)


if __name__ == "__main__":
    main()
