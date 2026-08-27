#!/usr/bin/env python3
"""
apply_greenhouse.py
-------------------
Applies to Greenhouse-ATS jobs via the public Greenhouse Application API.
No browser / Playwright needed for standard Greenhouse boards.

API flow:
  1. GET  https://boards-api.greenhouse.io/v1/boards/{board}/jobs/{job_id}?questions=true
         -> returns job metadata + list of required/optional questions
  2. POST https://boards-api.greenhouse.io/v1/applications?token={job_id}
         -> multipart/form-data: first_name, last_name, email, phone, resume, answers
  3. 200 -> applied; 4xx -> log and skip

Jobs are read from the profile's job store (ats_type=greenhouse),
sorted by fit_score descending, and skipped if already applied.

Usage:
    python3 apply_greenhouse.py                          # all 7+ score greenhouse jobs
    python3 apply_greenhouse.py --min-score 8
    python3 apply_greenhouse.py --company "Stripe"
    python3 apply_greenhouse.py --job-id 8031833 --board stripe
    python3 apply_greenhouse.py --dry-run
    python3 apply_greenhouse.py --limit 5
"""

import argparse
import email as emaillib
import imaplib
import logging
import os
import re
import shutil
import sys
import tempfile
import time
from email.header import decode_header

import truststore
import requests
import urllib3
from playwright.sync_api import sync_playwright
from playwright_stealth import Stealth

truststore.inject_into_ssl()
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

# ── Candidate profile (loaded from .env) ─────────────────────────────────────

import dotenv as _dotenv
_dotenv.load_dotenv(os.path.join(os.path.dirname(__file__), ".env"), override=False)

def _e(key: str, default: str = "") -> str:
    return os.environ.get(key, default)

PROFILE = {
    "first_name": f"{_e('CANDIDATE_FIRST_NAME')} {_e('CANDIDATE_MIDDLE_NAME')}".strip(),
    "last_name":  _e("CANDIDATE_LAST_NAME"),
    "email":      _e("CANDIDATE_EMAIL"),
    "phone":      _e("CANDIDATE_PHONE"),
    "phone_e164": _e("CANDIDATE_PHONE_E164"),
    "linkedin":   _e("CANDIDATE_LINKEDIN"),
    "website":    _e("CANDIDATE_GITHUB"),
    # Default resume PDF (overridden per-job if tailored PDF exists)
    "resume_pdf": os.path.expanduser(_e("CANDIDATE_RESUME_PDF")),
    # EEO / compliance defaults
    "gender":            "1",   # 1=Male
    "race":              "2",   # 2=Decline to identify
    "veteran":           "3",   # 3=Not a protected veteran (US), skip if not asked
    "disability":        "2",   # 2=Decline to answer
}

# Keywords that trigger a "Yes" answer on screening questions
_YES_KEYWORDS = (
    "legally authorized", "authorized to work", "eligible to work",
    "right to work", "sponsorship not required",
    "citizen or permanent resident",
    "legal right to work",
    # Willingness / office attendance
    "willing to work", "able to work", "comfortable working",
    "open to working", "available to work",
    # Technical skill Yes/No questions
    "apache flink", "kafka connect", "apache beam",
    "database fundamentals", "data modeling", "query optimization", "olap",
    "strong understanding of database",
)
# Keywords that trigger a "No" answer
_NO_KEYWORDS = (
    "require sponsorship", "need sponsorship", "visa sponsorship required",
    "not authorized", "will you now or in the future require",
    "require a work permit", "visa or additional right",
    # UK-specific
    "right to work in the uk", "right to work in uk",
    # Employment restrictions (GitLab etc.)
    "employment agreement", "post-employment restriction",
    "subject to any employment",
)

# Long-form experience descriptions keyed by topic keyword
_EXPERIENCE_DESCRIPTIONS = {
    "flink": (
        "I have solid technical knowledge of Apache Flink internals: checkpointing "
        "(async barrier snapshotting), state backends (RocksDB/heap), watermarks and "
        "event-time windowing, JobGraph execution model, and operator chaining. "
        "I understand Kafka Connect's connector/task lifecycle, offset management, "
        "rebalancing, and SMT (Single Message Transforms). For Apache Beam I understand "
        "the PCollection/PTransform model, runner portability framework (Flink/Dataflow/"
        "Spark), and cross-runner considerations for latency vs throughput."
    ),
    "kafka": (
        "I have solid technical knowledge of Apache Kafka internals including the "
        "producer/consumer model, partition leadership, ISR, compaction, and offset "
        "management. For Kafka Connect I understand the connector/task lifecycle, "
        "rebalancing, SMTs, and exactly-once semantics. Performance considerations "
        "include partition count tuning, batch size, compression codec selection, and "
        "consumer group lag monitoring."
    ),
    "beam": (
        "I have experience with Apache Beam's PCollection/PTransform model, windowing "
        "strategies (fixed, sliding, session), triggers, and the portability framework "
        "across Flink, Dataflow, and Spark runners. Performance considerations include "
        "bundle size, shuffle minimization, and runner-specific optimizations."
    ),
}

# Per-job answer overrides: job_id -> {label_substring_lower -> answer}
JOB_SPECIFIC_ANSWERS: dict[str, dict[str, str]] = {
    "6000803004": {  # ClickHouse
        "have you worked with the internals": "Yes",
        "apache flink, kafka connect or apache beam": "Yes",
        "describe your experience": _EXPERIENCE_DESCRIPTIONS["flink"],
        "database fundamentals": "Yes",
        "strong understanding of database": "Yes",
        "consenting to the use of ai": "Yes",
        "ai for evaluating my candidacy": "Yes",
    },
}

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, */*",
}

RATE_SLEEP = 2.0
TIMEOUT    = 20


# ── Greenhouse API helpers ───────────────────────────────────────────────────

def _gh_job_details(board: str, job_id: str, sess: requests.Session) -> dict:
    """Fetch job details + questions from Greenhouse boards API (read-only, public)."""
    url = (
        f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs/{job_id}"
        "?questions=true"
    )
    r = sess.get(url, timeout=TIMEOUT)
    if r.status_code != 200:
        logger.warning(f"  Job details {board}/{job_id}: HTTP {r.status_code}")
        return {}
    return r.json()


def _settle(page, ms: int = 2000):
    try:
        page.wait_for_load_state("networkidle", timeout=ms)
    except Exception:
        pass
    time.sleep(0.5)


def _fill_by_label(page, label_text: str, value: str, timeout: int = 4000):
    """Fill an input field whose visible label contains label_text."""
    try:
        # Try: label element -> associated input via for/id
        label = page.locator(f'label:has-text("{label_text}")').first
        for_id = label.get_attribute("for")
        if for_id:
            page.locator(f"#{for_id}").fill(value)
            return True
    except Exception:
        pass
    try:
        # Fallback: input immediately after label in DOM
        page.locator(
            f'label:has-text("{label_text}") ~ input, '
            f'label:has-text("{label_text}") + input'
        ).first.fill(value)
        return True
    except Exception:
        pass
    try:
        # input[name=...] by common field name derived from label
        fname = label_text.lower().replace(" ", "_").replace("/", "_")
        page.locator(f'input[name="{fname}"], input[id="{fname}"]').first.fill(value)
        return True
    except Exception:
        pass
    return False


def _select_option_robust(sel_element, answer: str) -> bool:
    """Try to select an option using multiple matching strategies."""
    for kwargs in [
        {"label": answer},
        {"value": answer},
        {"label": answer.lower()},
        {"value": answer.lower()},
        {"label": answer.capitalize()},
    ]:
        try:
            sel_element.select_option(**kwargs)
            return True
        except Exception:
            pass
    # Last resort: find option whose text contains the answer
    try:
        options = sel_element.locator("option").all()
        for opt in options:
            opt_text = (opt.inner_text() or "").strip()
            opt_val  = (opt.get_attribute("value") or "").strip()
            if opt_text.lower() == answer.lower() or opt_val.lower() == answer.lower():
                sel_element.select_option(value=opt_val or opt_text)
                return True
    except Exception:
        pass
    return False


def _click_react_select(page, field_name: str, answer: str) -> bool:
    """
    Interact with a Greenhouse React Select combobox:
      1. Click .select__control inside the container that has label[for=field_name]
      2. Wait for the option list to appear
      3. Click the option whose text matches answer
    """
    try:
        # Find the .select__control inside the right container
        # Greenhouse structure: div.select__container > label[for=id] + div > div.select__control
        container_sel = (
            f'.select__container:has(label[for="{field_name}"]) .select__control, '
            f'.select__container:has(label[id="{field_name}-label"]) .select__control'
        )
        ctrl = page.locator(container_sel).first
        if not ctrl.is_visible(timeout=3000):
            # Fallback: find the input, get its parent control
            inp = page.locator(f'#{field_name}[role="combobox"]').first
            if not inp.is_visible(timeout=2000):
                return False
            # Click the parent .select__control via JS
            page.evaluate(f"""() => {{
                var inp = document.getElementById('{field_name}');
                if (!inp) return;
                var node = inp.parentElement;
                for (var i = 0; i < 5; i++) {{
                    if (!node) break;
                    if (node.classList && node.classList.contains('select__control')) {{
                        node.click();
                        return;
                    }}
                    node = node.parentElement;
                }}
                // Fall back: click the input itself
                inp.click();
            }}""")
        else:
            ctrl.scroll_into_view_if_needed()
            ctrl.click()

        time.sleep(0.5)

        # Wait for option list — React Select uses id pattern react-select-{id}-option-{n}
        # or class select__option / select__menu
        opt_list_sel = (
            f'[id^="react-select-{field_name}-option"],'
            f' .select__option,'
            f' [id*="{field_name}-listbox"] [role="option"]'
        )
        try:
            page.locator(opt_list_sel).first.wait_for(state="visible", timeout=4000)
        except Exception:
            pass

        # Click matching option by text
        for opt_sel in [
            f'[id^="react-select-{field_name}-option"]:has-text("{answer}")',
            f'.select__option:has-text("{answer}")',
            f'[role="option"]:has-text("{answer}")',
        ]:
            try:
                opt = page.locator(opt_sel).first
                if opt.is_visible(timeout=2000):
                    opt.click()
                    time.sleep(0.3)
                    return True
            except Exception:
                pass

        # If exact match failed, try nth option based on fvalues ordering
    except Exception as exc:
        logger.debug(f"  React Select click failed for {field_name}: {exc}")
    return False


def _fill_selects_on_page(page, questions_map: dict[str, str]):
    """
    Scan every visible <select> on the page. For each one find the nearest
    label / surrounding text and look up the answer from questions_map
    (label_substring_lower -> answer).  Much more robust than CSS siblings.
    """
    try:
        selects = page.locator("select:visible").all()
    except Exception:
        return

    for sel in selects:
        try:
            sel_id   = sel.get_attribute("id")   or ""
            sel_name = sel.get_attribute("name")  or ""
            # Skip already-selected (non-placeholder) options
            try:
                current = sel.input_value()
                if current and current != "":
                    # Already has a value — check it's not the placeholder
                    pass  # still try to fill in case placeholder has value ""
            except Exception:
                pass

            # Collect nearby text: explicit <label for="id">, aria-label, placeholder
            nearby_texts = []
            if sel_id:
                try:
                    lbl = page.locator(f'label[for="{sel_id}"]').first
                    nearby_texts.append(lbl.inner_text().lower())
                except Exception:
                    pass
            try:
                aria = sel.get_attribute("aria-label") or ""
                if aria:
                    nearby_texts.append(aria.lower())
            except Exception:
                pass
            # Walk up DOM via evaluate to get container text
            try:
                container_text = page.evaluate("""(el) => {
                    let node = el.parentElement;
                    for (let i = 0; i < 4; i++) {
                        if (!node) break;
                        const t = node.innerText || '';
                        if (t.length > 5 && t.length < 600) return t.toLowerCase();
                        node = node.parentElement;
                    }
                    return '';
                }""", sel.element_handle())
                if container_text:
                    nearby_texts.append(container_text)
            except Exception:
                pass

            combined_text = " ".join(nearby_texts)
            if not combined_text.strip():
                continue

            # Find which question matches
            matched_answer = None
            for q_label_lower, ans in questions_map.items():
                if q_label_lower in combined_text:
                    matched_answer = ans
                    break

            if matched_answer:
                if _select_option_robust(sel, matched_answer):
                    print(f"    [SELECT] Filled select ({sel_id or sel_name}): {matched_answer!r}")
                else:
                    print(f"    [SELECT WARN] Could not select {matched_answer!r} in {sel_id or sel_name}")
        except Exception as exc:
            logger.debug(f"    select scan error: {exc}")


def _fill_react_selects_on_page(page, questions_map: dict[str, str]):
    """
    Scan every visible React Select .select__control that is still unfilled.
    For each, get the associated label text and fill from questions_map.
    Complements _fill_selects_on_page which only handles native <select>.
    """
    try:
        controls = page.locator(".select__control:visible").all()
    except Exception:
        return

    for ctrl in controls:
        try:
            # Already has a value? Skip.
            try:
                current_val = page.evaluate(
                    "(ctrl) => { const sv = ctrl.querySelector('.select__single-value'); "
                    "return sv ? sv.innerText.trim() : ''; }",
                    ctrl.element_handle(),
                )
                if current_val:
                    continue
            except Exception:
                pass

            # Get field_name from label[for=X] inside the .select__container ancestor
            field_name = page.evaluate("""(ctrl) => {
                let node = ctrl;
                for (let i = 0; i < 8; i++) {
                    node = node.parentElement;
                    if (!node) break;
                    if (node.classList && node.classList.contains('select__container')) {
                        const lbl = node.querySelector('label[for]');
                        if (lbl) return lbl.getAttribute('for');
                        break;
                    }
                }
                const inp = ctrl.querySelector('input[id]');
                return inp ? inp.id : '';
            }""", ctrl.element_handle())

            # Get readable label text
            label_text = ""
            if field_name:
                try:
                    label_text = page.locator(f'label[for="{field_name}"]').first.inner_text().lower()
                except Exception:
                    pass
            if not label_text:
                try:
                    label_text = page.evaluate("""(ctrl) => {
                        let node = ctrl;
                        for (let i = 0; i < 8; i++) {
                            node = node.parentElement;
                            if (!node) break;
                            const lbl = node.querySelector('label');
                            if (lbl && lbl.innerText.trim().length > 2)
                                return lbl.innerText.trim().toLowerCase();
                        }
                        return '';
                    }""", ctrl.element_handle())
                except Exception:
                    pass

            if not label_text:
                continue

            # Match against questions_map
            matched_answer = None
            for q_label_lower, ans in questions_map.items():
                if q_label_lower in label_text:
                    matched_answer = ans
                    break

            if not matched_answer:
                continue

            filled = False
            if field_name:
                filled = _click_react_select(page, field_name, matched_answer)

            if not filled:
                # Fallback: click the ctrl directly, then pick matching option
                try:
                    ctrl.scroll_into_view_if_needed()
                    ctrl.click()
                    time.sleep(0.5)
                    for opt_sel in [
                        f'.select__option:has-text("{matched_answer}")',
                        f'[role="option"]:has-text("{matched_answer}")',
                    ]:
                        try:
                            opt = page.locator(opt_sel).first
                            if opt.is_visible(timeout=2000):
                                opt.click()
                                filled = True
                                break
                        except Exception:
                            pass
                    if not filled:
                        # Try partial match on first option with "decline" or "prefer"
                        for fallback_text in ("I don't wish", "Decline", "Prefer", "prefer", "decline"):
                            try:
                                opt = page.locator(f'.select__option:has-text("{fallback_text}")').first
                                if opt.is_visible(timeout=1000):
                                    opt.click()
                                    filled = True
                                    break
                            except Exception:
                                pass
                    # For year-range dropdowns: try multiple numeric values
                    if not filled and any(k in label_text for k in ("years of", "experience")):
                        for yr in ("7", "7+", "6", "6+", "5+", "6-8", "More than 5",
                                   "8+", "5-7", "6-10", "Over 5"):
                            try:
                                opt = page.locator(f'.select__option:has-text("{yr}")').first
                                if opt.is_visible(timeout=800):
                                    opt.click()
                                    filled = True
                                    break
                            except Exception:
                                pass
                    # For location/city questions mentioning "bangalore"
                    if not filled and any(k in label_text for k in ("bangalore", "bengaluru", "location", "city", "based in")):
                        for city in ("Bangalore", "Bengaluru", "bangalore", "bengaluru"):
                            try:
                                opt = page.locator(f'.select__option:has-text("{city}")').first
                                if opt.is_visible(timeout=800):
                                    opt.click()
                                    filled = True
                                    break
                            except Exception:
                                pass
                    # Last resort for unfilled required fields: pick first non-empty option
                    if not filled:
                        try:
                            all_opts = page.locator('.select__option:visible').all()
                            if all_opts:
                                # Log available options for debugging
                                available = []
                                for ao in all_opts[:8]:
                                    try:
                                        available.append(ao.inner_text().strip())
                                    except Exception:
                                        pass
                                logger.debug(f"    [REACT-SEL OPTS] {label_text[:40]!r}: {available}")
                                # Click first option that's not a placeholder
                                for ao in all_opts:
                                    try:
                                        txt = ao.inner_text().strip()
                                        if txt and txt.lower() not in ("select...", "-- select --", "", "select"):
                                            ao.click()
                                            filled = True
                                            print(f"    [REACT-SEL FIRST] {label_text[:40]!r} -> {txt!r}")
                                            break
                                    except Exception:
                                        pass
                        except Exception:
                            pass
                except Exception as exc:
                    logger.debug(f"    direct ctrl click failed: {exc}")

            if filled:
                print(f"    [REACT-SEL] {label_text[:55]!r} -> {matched_answer!r}")
            else:
                logger.debug(f"    [REACT-SEL MISS] {label_text[:40]!r} -> {matched_answer!r}")
        except Exception as exc:
            logger.debug(f"    react select sweep error: {exc}")


def _answer_label_on_page(page, label_text: str, answer: str):
    """
    Given a question label, find its input/select/checkbox/radio on the page
    and fill it with the answer string.
    """
    label_lower = label_text.lower()

    # Try radio / checkbox group (Yes/No or any single-select)
    try:
        # Find label for the answer value near the question
        radio = page.locator(
            f'label:has-text("{answer}"):near(:text("{label_text[:40]}"))'
        ).first
        radio.click()
        return
    except Exception:
        pass
    try:
        # radiogroup under the question label
        container = page.locator(
            f'[data-field-label*="{label_text[:30]}"], '
            f'*:has(> label:has-text("{label_text[:30]}"))'
        ).first
        container.locator(f'label:has-text("{answer}"), input[value="{answer}"]').first.click()
        return
    except Exception:
        pass

    # Text / textarea fill
    try:
        _fill_by_label(page, label_text[:40], answer)
        return
    except Exception:
        pass

    # Select dropdown
    try:
        sel = page.locator(
            f'label:has-text("{label_text[:40]}") ~ select, '
            f'label:has-text("{label_text[:40]}") + select'
        ).first
        _select_option_robust(sel, answer)
    except Exception:
        pass


def _answer_for_question(q: dict, job_id: str = "") -> str | None:
    """
    Return an answer value for a Greenhouse application question.
    Returns None for fields handled at the top-level (first_name/last_name/email/resume).
    """
    label  = (q.get("label") or "").lower()
    q_type = q.get("type", "")

    # Per-job override table — check first
    if job_id and job_id in JOB_SPECIFIC_ANSWERS:
        for key, ans in JOB_SPECIFIC_ANSWERS[job_id].items():
            if key in label:
                return ans

    # Top-level fields already set in form_data — skip here
    if label in ("first name", "last name", "email", "resume/cv", "resume",
                 "cover letter", "phone"):
        return None
    # Preferred/interview name
    if label in ("preferred first name", "preferred name") or \
       ("prefer" in label and "name" in label):
        return "Kaushal"

    # Phone (alternate labels)
    if "phone" in label and "number" in label:
        return PROFILE["phone_e164"]
    # LinkedIn
    if "linkedin" in label:
        return PROFILE["linkedin"]
    # Website / portfolio / GitHub
    if any(k in label for k in ("website", "portfolio", "github", "url", "blog")):
        return PROFILE["website"]
    # Sponsorship / work permit — check FIRST (these overlap with "right to work" pattern)
    if any(kw in label for kw in _NO_KEYWORDS):
        return "No"
    # Work authorization — Yes
    if any(kw in label for kw in _YES_KEYWORDS):
        return "Yes"
    # Current employer / company
    if any(k in label for k in ("current company", "current employer", "employer")):
        return "FICO (Fair Isaac Corporation)"
    # Current title / role
    if any(k in label for k in ("current title", "current role", "job title", "current position")):
        return "Lead Software Engineer"
    # Location / city / address
    if any(k in label for k in ("city", "home address", "location", "city and state")):
        return "Bengaluru, Karnataka, India"
    # Education / degree
    if any(k in label for k in ("degree", "highest education", "highest qualification",
                                 "education level", "academic qualification")):
        return "Bachelor"
    # Years of experience (dropdown style)
    if any(k in label for k in ("years of exp", "years exp", "years of experience",
                                 "years of professional", "total experience", "work experience")):
        return "7"
    # Career stage / seniority level
    if any(k in label for k in ("career stage", "career level", "seniority", "current level",
                                 "job level", "experience level")):
        return "Senior"
    # Security / vulnerability domain experience
    if any(k in label for k in ("security/vulnerability", "security domain", "vulnerability domain",
                                 "describes you.*security", "security.*describes you")):
        return "None"
    # Current compensation
    if any(k in label for k in ("current ctc", "current compensation", "current salary",
                                 "current package")):
        return "Open to discussion"
    # Open-ended "describe your work" / role questions
    if any(k in label for k in ("day-to-day", "day to day", "coding output",
                                 "currently working as", "primary responsibilities",
                                 "describe your role", "your current role",
                                 "what does your work", "describe your work")):
        return (
            "I am currently working as a Lead Software Engineer at FICO (Fair Isaac Corporation). "
            "My day-to-day involves primarily writing application code — designing and implementing "
            "Java/Spring Boot microservices with REST APIs, integrating messaging systems like Kafka "
            "for real-time data pipelines, and building full-stack features with React on the frontend. "
            "I spend the majority of my time on business logic and feature implementation rather than "
            "DevOps/infrastructure configuration."
        )
    # Open-ended "most complex / biggest project" questions
    if any(k in label for k in ("most complex", "biggest project", "complex tool",
                                 "complex system", "shipped", "personally built")):
        return (
            "The most complex system I built was a real-time decision management platform using "
            "Java/Spring Boot microservices, processing millions of rule evaluations per day via "
            "Kafka streams. I designed the distributed architecture, implemented REST APIs for "
            "the rules engine, and built a React-based authoring UI for non-technical business users. "
            "The system handles high-throughput credit risk scoring with sub-100ms p99 latency."
        )
    # Notice period
    if "notice" in label:
        return "30 days"
    # Salary/compensation
    if any(k in label for k in ("salary", "compensation", "expected", "current ctc", "pay")):
        return "Open to discussion"
    # How did you hear about us
    if any(k in label for k in ("hear about", "source", "referred", "learn about", "learn about this job")):
        return "LinkedIn"
    # Years of experience
    if any(k in label for k in ("years of experience", "years experience")):
        return "6"
    # Previously worked here? No.
    if any(k in label for k in ("previously worked", "worked for", "employed by", "former employee")):
        return "No"
    # Conflict of interest / outside activity / family member — No
    if any(k in label for k in ("conflict", "outside business", "family member", "relatives",
                                 "procurement", "government employee")):
        return "No"
    # Privacy/consent/acknowledgement checkboxes — agree
    if any(k in label for k in ("privacy policy", "consent", "acknowledge", "agree", "i agree",
                                 "confidential information")):
        return "Yes"
    # EEO / voluntary self-identification demographics
    if any(k in label for k in ("gender", "sex", "pronouns")):
        return "I don't wish to answer"
    if any(k in label for k in ("race", "ethnicity", "hispanic", "latino")):
        return "Decline to Self Identify"
    if any(k in label for k in ("veteran", "protected veteran", "military")):
        return "I am not a protected veteran"
    if any(k in label for k in ("disability", "disabled")):
        return "I do not have a disability"
    # Generic boolean/checkbox → No (safe default for unknown Yes/No questions)
    if q_type == "boolean":
        return "No"
    return None


def _fetch_greenhouse_otp(timeout_sec: int = 120) -> str | None:
    """
    Poll Gmail inbox for a Greenhouse security code email and return the 8-char code.
    Polls every 5 seconds for up to timeout_sec seconds.
    Reads credentials from .env in the same directory as this script.
    """
    import dotenv
    env_path = os.path.join(os.path.dirname(__file__), ".env")
    dotenv.load_dotenv(env_path, override=False)
    gmail_user = os.environ.get("GMAIL_ADDRESS", "")
    gmail_pass = os.environ.get("GMAIL_APP_PASSWORD", "")
    if not gmail_user or not gmail_pass:
        print("  [OTP] No Gmail credentials in .env — cannot auto-fetch code")
        return None

    deadline = time.time() + timeout_sec
    last_seen_uid = None
    print(f"  [OTP] Polling Gmail for Greenhouse security code (up to {timeout_sec}s)…")
    while time.time() < deadline:
        try:
            mail = imaplib.IMAP4_SSL("imap.gmail.com")
            mail.login(gmail_user, gmail_pass)
            mail.select("INBOX")
            # Search UNSEEN first; fallback to recent (last 3 min) if none found
            _, msgs = mail.search(None, 'FROM "greenhouse-mail.io" UNSEEN')
            ids = msgs[0].split()
            if not ids:
                # Also check recently received (may have been auto-read)
                from datetime import datetime, timedelta, timezone
                since_dt = (datetime.now(timezone.utc) - timedelta(minutes=5))
                since_str = since_dt.strftime("%d-%b-%Y")
                _, msgs2 = mail.search(None, f'FROM "greenhouse-mail.io" SINCE "{since_str}"')
                ids = msgs2[0].split()
            for uid in reversed(ids[-3:]):
                if uid == last_seen_uid:
                    continue
                _, data = mail.fetch(uid, "(RFC822)")
                msg = emaillib.message_from_bytes(data[0][1])
                subj_parts = decode_header(msg.get("Subject", ""))
                subj = "".join(
                    p.decode(enc or "utf-8") if isinstance(p, bytes) else str(p)
                    for p, enc in subj_parts
                )
                if "security code" not in subj.lower():
                    continue
                # Extract body
                body = ""
                if msg.is_multipart():
                    for part in msg.walk():
                        ct = part.get_content_type()
                        if ct in ("text/plain", "text/html"):
                            raw = part.get_payload(decode=True).decode("utf-8", errors="replace")
                            if ct == "text/html":
                                # strip tags to get plain text
                                raw = re.sub(r'<[^>]+>', ' ', raw)
                                raw = re.sub(r'\s+', ' ', raw)
                            body += raw + "\n"
                else:
                    raw = msg.get_payload(decode=True).decode("utf-8", errors="replace")
                    if msg.get_content_type() == "text/html":
                        raw = re.sub(r'<[^>]+>', ' ', raw)
                        raw = re.sub(r'\s+', ' ', raw)
                    body = raw
                # Greenhouse code: 8 alphanumeric chars after "application:" or
                # standalone block of exactly 8 mixed-case alnum chars
                m = (
                    re.search(r'application[:\s]+([A-Za-z0-9]{8})\b', body, re.I)
                    or re.search(r':\s+([A-Za-z][A-Za-z0-9]{7})\b', body)
                    or re.search(r'\b([A-Za-z][A-Za-z0-9]{7})\b(?!\s*\w)', body)
                )
                if m:
                    code = m.group(1)
                    print(f"  [OTP] Got Greenhouse security code: {code}")
                    mail.store(uid, "+FLAGS", "\\Seen")
                    mail.logout()
                    return code
                last_seen_uid = uid
            mail.logout()
        except Exception as exc:
            logger.debug(f"  [OTP] Gmail poll error: {exc}")
        time.sleep(5)

    print("  [OTP] Timed out waiting for Greenhouse security code")
    return None


def _enter_security_code(page, code: str) -> bool:
    """
    Enter an 8-char Greenhouse security code into the verification form.
    Handles both individual character boxes and a single input field.
    """
    try:
        # Check for individual character input boxes (Greenhouse uses divs/inputs per char)
        char_inputs = page.locator(
            'input[maxlength="1"], '
            '[class*="security"] input, '
            '[class*="verification"] input'
        ).all()
        visible_chars = [c for c in char_inputs if c.is_visible()]

        if len(visible_chars) == len(code):
            for i, ch in enumerate(code):
                visible_chars[i].click()
                visible_chars[i].fill(ch)
                time.sleep(0.05)
            print(f"  [OTP] Entered code char-by-char: {code}")
            return True

        # Single input for security code
        for sel in [
            'input[name="security_code"]',
            'input[placeholder*="code"]',
            'input[aria-label*="security"]',
            'input[type="text"][maxlength="8"]',
        ]:
            try:
                inp = page.locator(sel).first
                if inp.is_visible(timeout=1000):
                    inp.fill(code)
                    print(f"  [OTP] Entered code in single input: {code}")
                    return True
            except Exception:
                pass

        # Last resort: type into whatever is focused / any visible input in the OTP section
        try:
            section = page.locator(
                ':has-text("Security code"), :has-text("verification code")'
            ).last
            inp = section.locator("input").first
            inp.fill(code)
            print(f"  [OTP] Entered code via section fallback: {code}")
            return True
        except Exception:
            pass

    except Exception as exc:
        logger.debug(f"  [OTP] Enter security code error: {exc}")
    return False


def _resume_for_job(job: dict) -> str:
    pdf = job.get("pdf_path")
    if pdf and os.path.isfile(pdf):
        return pdf
    return PROFILE["resume_pdf"]


# ── Apply to one Greenhouse job (Playwright) ─────────────────────────────────

def apply_to_job(job: dict, ctx, sess: requests.Session, dry_run: bool = False) -> bool:
    """
    Apply to one Greenhouse job via browser automation (job-boards.greenhouse.io).
    ctx = Playwright browser context.
    Returns True on success / reaching confirmation.
    """
    board      = job.get("gh_board", "")
    job_id     = job.get("gh_job_id", "")
    apply_link = job.get("apply_link", "")
    resume_pdf = _resume_for_job(job)

    if not board or not job_id:
        logger.warning(f"  [{job.get('company')}] Missing gh_board/gh_job_id — skipping")
        return False
    if not os.path.isfile(resume_pdf):
        logger.warning(f"  [{job.get('company')}] Resume PDF not found: {resume_pdf}")
        return False

    # Prefer job-boards.greenhouse.io canonical URL
    gh_url = f"https://job-boards.greenhouse.io/{board}/jobs/{job_id}"

    print(f"\n{'='*60}")
    print(f"  JOB  : {job['title']} @ {job['company']}")
    print(f"  SCORE: {job.get('fit_score','?')}/10  {job.get('fit_reason','')}")
    print(f"  URL  : {gh_url}")
    print(f"{'='*60}")

    # Fetch question list for dry-run preview
    details   = _gh_job_details(board, job_id, sess)
    questions = details.get("questions", []) if details else []

    if dry_run:
        for q in questions:
            ans = _answer_for_question(q, job_id=job_id)
            print(f"    Q: {q.get('label')!r:55s}  -> {ans!r}")
        return True

    page = ctx.new_page()
    Stealth().apply_stealth_sync(page)

    try:
        # ── Step 1: Load job page ──────────────────────────────────────────
        print("[1] Loading job page…")
        page.goto(gh_url, wait_until="domcontentloaded", timeout=30000)
        _settle(page, 3000)

        # Guard: if we ended up on a jobs listing page (not the specific job),
        # the job was likely removed/expired. Bail out early.
        final_url = page.url
        if f"/jobs/{job_id}" not in final_url and "apply" not in final_url.lower():
            # Check if we're on a listing page (multiple job rows visible)
            job_rows = page.locator('a[href*="/jobs/"]').count()
            if job_rows > 3:
                logger.warning(f"  Redirected to listings page (job expired?) — skipping {board}/{job_id}")
                import job_store as _js
                _js.mark_applied(job["id"], applied=False, error="expired — job listing redirected")
                return False

        # Dismiss cookie consent banners (ThoughtWorks, etc.)
        for cookie_sel in [
            'button:has-text("Accept optional cookies")',
            'button:has-text("Accept all cookies")',
            'button:has-text("Accept all")',
            'button:has-text("Accept")',
            'button:has-text("Decline optional cookies")',
            'button:has-text("Decline")',
            'button[id*="cookie"][id*="accept"]',
            'button[class*="cookie"][class*="accept"]',
            '[data-testid*="cookie"] button',
            '#onetrust-accept-btn-handler',
        ]:
            try:
                btn = page.locator(cookie_sel).first
                if btn.is_visible(timeout=1500):
                    btn.click()
                    time.sleep(0.5)
                    break
            except Exception:
                pass

        # ── Step 2: Click Apply button ─────────────────────────────────────
        print("[2] Clicking Apply…")
        for sel in [
            'a:has-text("Apply for this Job")',
            'button:has-text("Apply for this Job")',
            'a:has-text("Apply Now")',
            'button:has-text("Apply")',
            '[data-qa="btn-apply"]',
        ]:
            try:
                btn = page.locator(sel).first
                if btn.is_visible(timeout=4000):
                    btn.click()
                    _settle(page, 3000)
                    break
            except Exception:
                pass

        # ── Step 3: Fill standard contact fields ──────────────────────────
        print("[3] Filling contact info…")
        # By name attribute (most common in Greenhouse)
        for field, value in [
            ("first_name", PROFILE["first_name"]),
            ("last_name",  PROFILE["last_name"]),
            ("email",      PROFILE["email"]),
            ("phone",      PROFILE["phone_e164"]),
        ]:
            try:
                loc = page.locator(f'input[name="{field}"], input[id="{field}"]').first
                if loc.is_visible(timeout=3000):
                    loc.fill(value)
            except Exception:
                pass
        # Phone country code dropdown — select +91 (India)
        for sel in [
            'select[name="phone_country_code"]',
            'select[id="phone_country_code"]',
            'select[name="phone[country_code]"]',
            'select:near(input[name="phone"])',
        ]:
            try:
                dd = page.locator(sel).first
                if dd.is_visible(timeout=2000):
                    # Try by value "+91" or "IN" or label "India"
                    for opt in ("+91", "IN", "India", "91"):
                        try:
                            dd.select_option(value=opt)
                            break
                        except Exception:
                            try:
                                dd.select_option(label=opt)
                                break
                            except Exception:
                                pass
                    break
            except Exception:
                pass
        # Location / city — Greenhouse "candidate-location" React Select (search-driven)
        # The field id is "candidate-location" (React Select combobox). Must type to search.
        loc_filled = False
        try:
            loc_inp = page.locator('#candidate-location').first
            if loc_inp.is_visible(timeout=2000):
                # Click the parent .select__control to focus
                page.evaluate("""() => {
                    const inp = document.getElementById('candidate-location');
                    if (!inp) return;
                    let n = inp.parentElement;
                    for (let i = 0; i < 5; i++) {
                        if (!n) break;
                        if (n.classList && n.classList.contains('select__control')) { n.click(); return; }
                        n = n.parentElement;
                    }
                    inp.focus();
                }""")
                time.sleep(0.4)
                # Type search query
                loc_inp.type("Bangalore", delay=50)
                time.sleep(1.5)
                # Click first option that appears
                for opt_sel in [
                    f'[id^="react-select-candidate-location-option"]',
                    '.select__option:has-text("Bangalore")',
                    '[role="option"]:has-text("Bangalore")',
                    '.select__option:first-child',
                    '[role="option"]:first-child',
                ]:
                    try:
                        opt = page.locator(opt_sel).first
                        if opt.is_visible(timeout=2000):
                            opt.click()
                            time.sleep(0.3)
                            loc_filled = True
                            print("    [LOCATION] Bangalore (React Select selected)")
                            break
                    except Exception:
                        pass
        except Exception:
            pass

        # LinkedIn / website by common ids
        for field, value in [
            ("linkedin_profile_url", PROFILE["linkedin"]),
            ("website",              PROFILE["website"]),
        ]:
            try:
                page.locator(
                    f'input[id="{field}"], input[name="{field}"]'
                ).first.fill(value)
            except Exception:
                pass

        # Employment history section — Greenhouse built-in work history fields
        try:
            # Company name field in employment section
            emp_company_sels = [
                'input[name*="employment"][name*="company"]',
                'input[id*="employment"][id*="company"]',
                'input[placeholder*="company name" i]',
            ]
            for sel in emp_company_sels:
                try:
                    els = page.locator(sel).all()
                    for el in els:
                        if el.is_visible(timeout=1000) and not el.input_value():
                            el.fill("FICO (Fair Isaac Corporation)")
                except Exception:
                    pass

            # Title field in employment section
            emp_title_sels = [
                'input[name*="employment"][name*="title"]',
                'input[id*="employment"][id*="title"]',
            ]
            for sel in emp_title_sels:
                try:
                    els = page.locator(sel).all()
                    for el in els:
                        if el.is_visible(timeout=1000) and not el.input_value():
                            el.fill("Lead Software Engineer")
                except Exception:
                    pass

            # Start year field
            for sel in ['input[name*="start_date_year"]', 'input[id*="start_date_year"]',
                        'input[placeholder*="start" i][placeholder*="year" i]']:
                try:
                    els = page.locator(sel).all()
                    for el in els:
                        if el.is_visible(timeout=1000) and not el.input_value():
                            el.fill("2019")
                except Exception:
                    pass

            # Start month dropdown
            for sel in ['select[name*="start_date_month"]', 'select[id*="start_date_month"]']:
                try:
                    els = page.locator(sel).all()
                    for el in els:
                        if el.is_visible(timeout=1000):
                            try:
                                el.select_option(value="1")
                            except Exception:
                                try:
                                    el.select_option(label="January")
                                except Exception:
                                    pass
                except Exception:
                    pass

            # Mark as current role checkbox
            for sel in ['input[name*="current"][type="checkbox"]',
                        'input[id*="current"][type="checkbox"]']:
                try:
                    els = page.locator(sel).all()
                    for el in els:
                        if el.is_visible(timeout=1000) and not el.is_checked():
                            el.click()
                except Exception:
                    pass
        except Exception as exc:
            logger.debug(f"    Employment history fill error: {exc}")

        # Education section — Greenhouse built-in education fields
        try:
            for sel in ['input[name*="education"][name*="school"]',
                        'input[id*="education"][id*="school"]',
                        'input[placeholder*="school name" i]',
                        'input[placeholder*="university" i]']:
                try:
                    els = page.locator(sel).all()
                    for el in els:
                        if el.is_visible(timeout=1000) and not el.input_value():
                            el.fill("National Institute of Technology")
                except Exception:
                    pass

            for sel in ['input[name*="education"][name*="discipline"]',
                        'input[id*="education"][id*="discipline"]',
                        'input[placeholder*="field of study" i]',
                        'input[placeholder*="discipline" i]']:
                try:
                    els = page.locator(sel).all()
                    for el in els:
                        if el.is_visible(timeout=1000) and not el.input_value():
                            el.fill("Computer Science and Engineering")
                except Exception:
                    pass

            # Education degree dropdown (native select)
            for sel in ['select[name*="education"][name*="degree"]',
                        'select[id*="education"][id*="degree"]']:
                try:
                    els = page.locator(sel).all()
                    for el in els:
                        if el.is_visible(timeout=1000):
                            for opt in ("Bachelor", "B.Tech", "B.E.", "Bachelors",
                                        "undergraduate", "4"):
                                try:
                                    el.select_option(label=opt)
                                    break
                                except Exception:
                                    try:
                                        el.select_option(value=opt)
                                        break
                                    except Exception:
                                        pass
                except Exception:
                    pass

            # Education start/end year
            for sel in ['input[name*="education"][name*="start_year"]',
                        'input[id*="education"][id*="start_year"]']:
                try:
                    els = page.locator(sel).all()
                    for el in els:
                        if el.is_visible(timeout=1000) and not el.input_value():
                            el.fill("2015")
                except Exception:
                    pass

            for sel in ['input[name*="education"][name*="end_year"]',
                        'input[id*="education"][id*="end_year"]']:
                try:
                    els = page.locator(sel).all()
                    for el in els:
                        if el.is_visible(timeout=1000) and not el.input_value():
                            el.fill("2019")
                except Exception:
                    pass
        except Exception as exc:
            logger.debug(f"    Education fill error: {exc}")

        # ── Step 4: Upload resume ──────────────────────────────────────────
        print("[4] Uploading resume…")
        try:
            fi = page.locator('input[type="file"]').first
            fi.wait_for(state="attached", timeout=10000)
            fi.set_input_files(resume_pdf)
            _settle(page, 4000)
            print("    Resume uploaded.")
        except Exception as exc:
            print(f"    [WARN] Resume upload: {exc}")

        # ── Step 5: Answer custom questions ───────────────────────────────
        print("[5] Answering questions…")
        for q in questions:
            label = q.get("label", "")
            answer = _answer_for_question(q, job_id=job_id)
            if answer is None or not label:
                continue
            # Note: q.get("type") is often "" in GH API; true type is in fields[0]["type"]
            fields = q.get("fields", [])
            field_name = fields[0].get("name", "") if fields else ""
            ftype     = fields[0].get("type", "") if fields else q.get("type", "")
            fvalues   = fields[0].get("values", []) if fields else []

            try:
                if ftype == "multi_value_single_select":
                    # React Select combobox — click control, then click option
                    filled = False
                    if field_name:
                        filled = _click_react_select(page, field_name, answer)

                    if filled:
                        print(f"    [SELECT] {label[:50]!r} -> {answer!r}")
                    else:
                        logger.debug(f"    [SELECT MISS] {label[:50]!r} -> {answer!r}")
                elif ftype == "boolean" or ftype == "input_file":
                    pass  # handled elsewhere (file upload / EEO)
                elif ftype == "checkbox":
                    cb = page.locator(
                        f'input[type="checkbox"][name="{field_name}"], '
                        f'input[type="checkbox"][id="{field_name}"]'
                    ).first
                    if answer in ("Yes", "1", "true") and not cb.is_checked():
                        cb.click()
                else:
                    # input_text / textarea / unknown
                    filled = False
                    if field_name:
                        try:
                            el = page.locator(
                                f'input[id="{field_name}"], input[name="{field_name}"], '
                                f'textarea[id="{field_name}"], textarea[name="{field_name}"]'
                            ).first
                            if el.is_visible(timeout=2000):
                                el.fill(answer)
                                filled = True
                                print(f"    [TEXT] {label[:50]!r} -> {answer[:60]!r}")
                        except Exception:
                            pass
                    if not filled:
                        _fill_by_label(page, label[:40], answer)
                time.sleep(0.15)
            except Exception as exc:
                logger.debug(f"    Q skip [{label[:30]}]: {exc}")

        # Comprehensive SELECT sweep — catches any dropdowns missed above
        print("[5b] SELECT sweep…")
        _select_map: dict[str, str] = {}
        for q in questions:
            lbl = q.get("label", "")
            ans = _answer_for_question(q, job_id=job_id)
            if ans is not None and lbl:
                _select_map[lbl.lower()] = ans
        if job_id in JOB_SPECIFIC_ANSWERS:
            for k, v in JOB_SPECIFIC_ANSWERS[job_id].items():
                _select_map[k] = v
        for kw in _NO_KEYWORDS:
            _select_map[kw] = "No"
        for kw in _YES_KEYWORDS:
            _select_map.setdefault(kw, "Yes")
        # EEO / voluntary self-identification — always add as fallback
        _select_map.setdefault("gender", "I don't wish to answer")
        _select_map.setdefault("sex", "I don't wish to answer")
        _select_map.setdefault("race", "Decline to Self Identify")
        _select_map.setdefault("ethnicity", "Decline to Self Identify")
        _select_map.setdefault("hispanic", "No")
        _select_map.setdefault("veteran", "I am not a protected veteran")
        _select_map.setdefault("disability", "I do not have a disability")
        # Country / location dropdowns (GitLab, etc.)
        _select_map.setdefault("country of residence", "India")
        _select_map.setdefault("country in which you will be located", "India")
        _select_map.setdefault("located if hired", "India")
        _select_map.setdefault("country", "India")
        # Location city React Select (candidate-location on some GH forms)
        _select_map.setdefault("location (city)", "Bangalore")
        _select_map.setdefault("candidate-location", "Bangalore")
        # Education / career stage (PhonePe, etc.)
        _select_map.setdefault("degree", "Bachelor")
        _select_map.setdefault("highest education", "Bachelor")
        _select_map.setdefault("years of exp", "7")
        _select_map.setdefault("years expereince", "7")    # PhonePe typo
        _select_map.setdefault("years of professional", "7")
        _select_map.setdefault("career stage", "Senior")
        _select_map.setdefault("career level", "Senior")
        _select_map.setdefault("seniority", "Senior")
        # Willingness dropdowns (InMobi, etc.)
        _select_map.setdefault("willing to work", "Yes")
        _select_map.setdefault("5 days a week", "Yes")
        # Veeam / sponsorship / outside business (long label variants)
        _select_map.setdefault("sponsor a visa", "No")
        _select_map.setdefault("sponsor a work permit", "No")
        _select_map.setdefault("require the company to sponsor", "No")
        _select_map.setdefault("talent community", "Yes")
        _select_map.setdefault("own, operate", "No")
        _select_map.setdefault("consultancy, freelance", "No")
        _select_map.setdefault("board membership", "No")
        _select_map.setdefault("outside business", "No")
        _select_map.setdefault("freelance", "No")
        _fill_selects_on_page(page, _select_map)
        _fill_react_selects_on_page(page, _select_map)
        _settle(page, 1000)

        # ── Step 6: EEO checkboxes / consent ──────────────────────────────
        print("[6] Handling consent / EEO…")
        # Scroll to bottom first so EEO section and consent checkboxes are reachable
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        time.sleep(0.8)
        # Second React Select sweep for EEO dropdowns (sex, gender, etc.) now in view
        _fill_react_selects_on_page(page, _select_map)
        try:
            for cb in page.locator('input[type="checkbox"]').all():
                try:
                    aria  = (cb.get_attribute("aria-label") or "").lower()
                    name  = (cb.get_attribute("name")       or "").lower()
                    cb_id = (cb.get_attribute("id")         or "").lower()
                    # Get surrounding label text — check label[for=id] and parent label
                    label_text = ""
                    cb_raw_id = cb.get_attribute("id") or ""
                    if cb_raw_id:
                        try:
                            label_text = page.locator(
                                f'label[for="{cb_raw_id}"]'
                            ).first.inner_text().lower()
                        except Exception:
                            pass
                    if not label_text:
                        try:
                            # checkbox may be inside a <label>
                            label_text = page.evaluate(
                                "(el) => { let n = el.parentElement; "
                                "for(let i=0;i<4;i++){ if(!n) break; "
                                "if(n.tagName==='LABEL') return n.innerText.toLowerCase(); "
                                "n=n.parentElement; } return ''; }",
                                cb.element_handle()
                            )
                        except Exception:
                            pass
                    combined = aria + " " + name + " " + cb_id + " " + label_text
                    if any(k in combined for k in (
                        "consent", "agree", "policy", "acknowledge",
                        "confidential", "privacy", "i agree",
                    )):
                        if not cb.is_checked():
                            try:
                                cb.scroll_into_view_if_needed()
                                cb.check()
                                print(f"    [CB] Checked: {(label_text or cb_id)[:50]!r}")
                            except Exception:
                                try:
                                    cb.click()
                                except Exception:
                                    pass
                            time.sleep(0.1)
                except Exception:
                    pass

            # Also click any "I Agree" label directly (catches label-wrapped checkboxes)
            for lbl in page.locator('label:has-text("I Agree")').all():
                try:
                    cb = lbl.locator('input[type="checkbox"]').first
                    if cb.count() and not cb.is_checked():
                        cb.scroll_into_view_if_needed()
                        cb.check()
                        print(f"    [CB-LBL] Checked 'I Agree' label checkbox")
                        time.sleep(0.1)
                except Exception:
                    try:
                        lbl.scroll_into_view_if_needed()
                        lbl.click()
                        time.sleep(0.1)
                    except Exception:
                        pass
        except Exception:
            pass

        # ── Step 7: Submit ─────────────────────────────────────────────────
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        time.sleep(1)

        # Try auto-submit
        submitted = False
        for sel in [
            'button[type="submit"]:has-text("Submit application")',
            'button[type="submit"]:has-text("Submit")',
            'button:has-text("Submit Application")',
            '[data-qa="btn-submit"]',
            'input[type="submit"]',
        ]:
            try:
                btn = page.locator(sel).first
                if btn.is_visible(timeout=3000):
                    btn.click()
                    _settle(page, 5000)
                    submitted = True
                    print(f"  Auto-submit clicked: {sel}")
                    break
            except Exception:
                pass

        if not submitted:
            print("[WARN] No submit button found — skipping (batch mode)")

        # ── Step 7b: Handle Greenhouse security code (email OTP) ──────────────
        if submitted:
            time.sleep(3)
            try:
                body_text = page.inner_text("body")
                if "verification code" in body_text.lower() or "security code" in body_text.lower():
                    print("[7b] Security code required — fetching from Gmail…")
                    otp = _fetch_greenhouse_otp(timeout_sec=120)
                    if otp:
                        _enter_security_code(page, otp)
                        time.sleep(1)
                        # Click Submit again
                        for sel in [
                            'button[type="submit"]:has-text("Submit")',
                            'button:has-text("Submit application")',
                            'button:has-text("Submit")',
                            'input[type="submit"]',
                        ]:
                            try:
                                btn = page.locator(sel).first
                                if btn.is_visible(timeout=2000):
                                    btn.click()
                                    _settle(page, 5000)
                                    print(f"  OTP re-submit clicked: {sel}")
                                    break
                            except Exception:
                                pass
                    else:
                        print("  [OTP] Could not get code automatically — waiting 3 min for manual entry")
                        time.sleep(180)
            except Exception as exc:
                logger.debug(f"  [OTP] Security code step error: {exc}")

        # After submit, give the page a short moment to redirect
        if submitted:
            time.sleep(3)
            # If already left greenhouse.io, no need to wait for confirmation URL
            if "greenhouse.io" not in page.url:
                pass  # success check below handles it
            else:
                # Still on greenhouse — wait up to 15s for a redirect
                try:
                    page.wait_for_url(
                        re.compile(r"(confirmation|thank|submitted|success)", re.I),
                        timeout=15_000,
                    )
                except Exception:
                    pass
        else:
            # No submit attempt — wait up to 90s for auto-redirect
            try:
                page.wait_for_url(
                    re.compile(r"(confirmation|thank|submitted|success)", re.I),
                    timeout=90_000,
                )
            except Exception:
                pass

        final_url = page.url
        print(f"[RESULT] {final_url}")

        if any(k in final_url.lower() for k in ("confirm", "thank", "submit", "success")):
            print("[SUCCESS] Application submitted!")
            page.close()
            return True
        # If URL left greenhouse.io after submit was attempted, treat as success
        # (Okta, Airbnb etc. redirect to their own careers page post-apply)
        if submitted and "greenhouse.io" not in final_url and gh_url not in final_url:
            print("[SUCCESS] Redirected away from Greenhouse — application likely submitted.")
            page.close()
            return True
        # Check for success text on page
        try:
            body = page.inner_text("body")[:500]
            if any(k in body.lower() for k in ("thank you", "application received", "successfully", "your application")):
                print("[SUCCESS] Confirmation text detected.")
                page.close()
                return True
        except Exception:
            pass

        page.screenshot(path=f"/tmp/gh_apply_{board}_{job_id}.png")
        print(f"[WARN] Not confirmed — screenshot at /tmp/gh_apply_{board}_{job_id}.png")
        page.close()
        return False

    except Exception as exc:
        print(f"[ERROR] {exc}")
        try:
            page.screenshot(path=f"/tmp/gh_error_{board}_{job_id}.png")
        except Exception:
            pass
        page.close()
        return False


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Apply to Greenhouse jobs from job store")
    parser.add_argument("--min-score", type=int, default=7,
                        help="Minimum fit_score to apply (default: 7)")
    parser.add_argument("--company",  help="Filter to a single company (substring match)")
    parser.add_argument("--job-id",   help="Apply to a specific Greenhouse job ID")
    parser.add_argument("--board",    help="Board slug (required with --job-id)")
    parser.add_argument("--limit",         type=int, default=50, help="Max jobs to apply per run (default: 50)")
    parser.add_argument("--min-companies", type=int, default=10, help="Min distinct companies in selection (default: 10)")
    parser.add_argument("--dry-run",  action="store_true", help="Print questions but don't submit")
    parser.add_argument("--profile",  default=os.environ.get("CANDIDATE_PROFILE_SLUG", ""), help="Profile slug")
    args = parser.parse_args()

    import profiles
    profiles.set_active_profile(args.profile)
    import job_store

    if args.job_id:
        if not args.board:
            print("--board is required with --job-id")
            sys.exit(1)
        jobs = [{
            "id":          f"gh_{args.job_id}",
            "title":       "Job from CLI",
            "company":     args.board,
            "apply_link":  f"https://boards.greenhouse.io/{args.board}/jobs/{args.job_id}",
            "fit_score":   10,
            "fit_reason":  "Manually specified",
            "gh_board":    args.board,
            "gh_job_id":   args.job_id,
            "pdf_path":    None,
        }]
    else:
        # Companies with custom apply flows that redirect away from GH form
        # (have their own apply scripts or need manual handling)
        _CUSTOM_FLOW_COMPANIES = {
            "databricks", "okta", "stripe", "datadog", "mongodb", "dropbox",
            "airbnb", "druva", "fivetran", "salesloft",
        }

        # Location filter: only India or truly global remote
        _INDIA_KEYWORDS = (
            "india", "bangalore", "bengaluru", "hyderabad", "chennai",
            "pune", "mumbai", "delhi", "noida", "gurugram", "gurgaon",
            "kolkata", "india remote", "remote, india", "remote - india",
        )
        # Non-India countries that disqualify a job even if "remote" appears
        _BLOCKED_GEO = (
            "united states", "us-remote", "us remote", "remote - us",
            "remote, us", "remote - usa", "remote, usa", "remote - california",
            "remote - new york", "canada", "united kingdom", "uk remote",
            "remote - uk", "poland", "netherlands", "germany", "france",
            "spain", "italy", "portugal", "ireland", "switzerland",
            "singapore", "australia", "brazil", "mexico", "amsterdam",
        )

        def _is_india_or_remote(job: dict) -> bool:
            loc = (job.get("location") or "").lower()
            if not loc:
                return True   # unknown location — allow (fetched for India)
            # Explicitly India → allow
            if any(kw in loc for kw in _INDIA_KEYWORDS):
                return True
            # Blocked geography → reject
            if any(kw in loc for kw in _BLOCKED_GEO):
                return False
            # Remaining "remote" / "worldwide" / "global" → allow
            if any(kw in loc for kw in ("remote", "worldwide", "global", "anywhere")):
                return True
            return False   # unknown country — skip to be safe

        all_jobs = job_store.all_jobs()
        jobs = [
            j for j in all_jobs
            if j.get("ats_type") == "greenhouse"
            and j.get("fit_score", 0) >= args.min_score
            and not j.get("applied_at")
            and _is_india_or_remote(j)
            and j.get("apply_status") != "failed"
            and not j.get("removed")
            and j.get("gh_board")
            and j.get("gh_job_id")
            and j.get("company", "").lower() not in _CUSTOM_FLOW_COMPANIES
        ]
        if args.company:
            needle = args.company.lower()
            jobs = [j for j in jobs if needle in j.get("company", "").lower()]

        # Sort by fit_score descending (highest first)
        jobs.sort(key=lambda j: j.get("fit_score", 0), reverse=True)

        # ── Per-company monthly cap — prevents policy violations ─────────────
        # Some companies limit applications per rolling 30-day period.
        _COMPANY_MONTHLY_CAPS: dict[str, int] = {
            "zscaler": 3,
        }
        from datetime import date as _date, timedelta as _td
        _30days_ago = (_date.today() - _td(days=30)).isoformat()
        # Count how many we've applied per company in last 30 days
        _applied_last30_by_co: dict[str, int] = {}
        for j in all_jobs:
            if j.get("applied_at") and j.get("applied_at", "") >= _30days_ago:
                co_key = j.get("company", "").lower()
                _applied_last30_by_co[co_key] = _applied_last30_by_co.get(co_key, 0) + 1

        jobs_before_cap = jobs
        jobs = []
        for j in jobs_before_cap:
            co_key = j.get("company", "").lower()
            cap = next((v for k, v in _COMPANY_MONTHLY_CAPS.items() if k in co_key), None)
            if cap is not None:
                already = _applied_last30_by_co.get(co_key, 0)
                if already >= cap:
                    logger.info(f"  [CAP] {j.get('company')} already at {already}/{cap} apps this month — skipping")
                    continue
            jobs.append(j)

        # ── Dedup: at most 1 job per (company, base-title) ──────────────────
        # Strips suffixes like " - Salesforce", " (Core Frontier)", ", Customer Dev Tools"
        # so we only apply to the best-scoring variant of the same role at a company.
        def _base_title(t: str) -> str:
            t = re.split(r"\s*[-–,(/]", t)[0].strip().lower()
            return re.sub(r"\s+", " ", t)

        # Build set of already-applied (company, base-title) from store
        _already_applied_pairs = {
            (_base_title(j.get("title", "")), j.get("company", "").lower())
            for j in all_jobs
            if j.get("applied_at")
        }

        deduped = []
        seen_pairs: set = set(_already_applied_pairs)
        for j in jobs:
            pair = (_base_title(j.get("title", "")), j.get("company", "").lower())
            if pair in seen_pairs:
                logger.info(
                    f"  [dedup] Skipping '{j.get('title')}' @ {j.get('company')} "
                    f"(already applied to same role)"
                )
                continue
            seen_pairs.add(pair)
            deduped.append(j)
        jobs = deduped

        # ── Total limit ───────────────────────────────────────────────────
        limit = args.limit or len(jobs)
        top = jobs[:limit]

        # ── Diversity: ensure at least min_companies distinct companies ───
        # Take top-scoring jobs first; if fewer than min_companies are
        # represented, swap in the best job from each missing company
        # (replacing the lowest-scoring duplicate from an over-represented one).
        min_co = args.min_companies
        if min_co and not args.company:
            # Index remaining jobs (beyond top slice) by company for fast lookup
            remaining = jobs[limit:]
            # Map company -> best available job not already in top
            best_by_co: dict = {}
            for j in remaining:
                co = j.get("company", "").lower()
                if co not in best_by_co:
                    best_by_co[co] = j  # already sorted desc, first is best

            distinct = {j.get("company", "").lower() for j in top}
            missing_cos = [co for co in best_by_co if co not in distinct]

            for co in missing_cos:
                if len(distinct) >= min_co:
                    break
                # Find the lowest-scoring job in top that belongs to
                # the most-represented company
                from collections import Counter
                co_freq = Counter(j.get("company", "").lower() for j in top)
                most_common_co, most_count = co_freq.most_common(1)[0]
                if most_count <= 1:
                    break  # can't reduce further without dropping diversity
                # Remove the lowest-scoring job from that company
                for idx in range(len(top) - 1, -1, -1):
                    if top[idx].get("company", "").lower() == most_common_co:
                        top.pop(idx)
                        break
                # Add best job from the new company
                top.append(best_by_co[co])
                top.sort(key=lambda j: j.get("fit_score", 0), reverse=True)
                distinct.add(co)

        jobs = top

    if not jobs:
        print(f"No Greenhouse jobs with fit_score >= {args.min_score} ready to apply.")
        print("Run:  python3 fetch_greenhouse.py  to fetch jobs first.")
        sys.exit(0)

    print(f"\nFound {len(jobs)} Greenhouse job(s) to apply to (sorted by fit score):\n")
    for j in jobs:
        print(f"  [{j.get('fit_score','?')}/10] {j.get('title','?')[:55]} @ {j.get('company','?')}")
        print(f"    board={j.get('gh_board')}  id={j.get('gh_job_id')}")
    print()

    sess = requests.Session()
    sess.headers.update(_HEADERS)
    sess.verify = False

    if args.dry_run:
        for j in jobs:
            apply_to_job(j, None, sess, dry_run=True)
        return

    # ── Pre-generate tailored resumes ──────────────────────────────────────
    # For each job, tailor the base resume to maximize ATS keyword match.
    # Tailored PDF is stored and referenced via job["pdf_path"].
    # Falls back to default resume PDF if tailoring fails.
    if not args.job_id:  # Skip tailoring for manual CLI --job-id runs
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
                # Fetch description if not stored
                if not j.get("description"):
                    try:
                        _det = _gh_job_details(j.get("gh_board",""), j.get("gh_job_id",""), sess)
                        _desc = _det.get("content") or _det.get("description") or ""
                        if _desc:
                            j["description"] = _desc
                            job_store.update_job(j["id"], description=_desc)
                    except Exception:
                        pass
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
                    # Persist pdf_path to job store so future restarts skip retailoring
                    job_store.update_job(j["id"], pdf_path=str(pdf_path))
                except Exception as tailor_exc:
                    logger.debug(f"  [TAILOR SKIP] {j.get('company')}: {tailor_exc}")
            print("[TAILOR] Done.\n")
        except ImportError as imp_err:
            logger.debug(f"  [TAILOR] resume_tailor not available: {imp_err}")

    # ── Launch Playwright browser with Chrome cookies ──────────────────────
    chrome_src = os.path.expanduser("~/Library/Application Support/Google/Chrome/Default")
    tmp = tempfile.mkdtemp(prefix="chrome_gh_apply_")
    dst = os.path.join(tmp, "Default")
    os.makedirs(dst, exist_ok=True)
    for f in ("Cookies", "Cookies-journal"):
        s = os.path.join(chrome_src, f)
        if os.path.isfile(s):
            shutil.copy2(s, os.path.join(dst, f))

    applied_ids = []
    skipped     = []

    with sync_playwright() as pw:
        try:
            ctx = pw.chromium.launch_persistent_context(
                tmp,
                channel="chrome",
                headless=False,
                args=["--disable-blink-features=AutomationControlled"],
                viewport={"width": 1280, "height": 900},
            )
        except Exception:
            ctx = pw.chromium.launch_persistent_context(
                tmp,
                headless=False,
                args=["--disable-blink-features=AutomationControlled"],
                viewport={"width": 1280, "height": 900},
            )

        for j in jobs:
            try:
                ok = apply_to_job(j, ctx, sess)
                if ok:
                    job_store.mark_applied(j["id"], applied=True)
                    applied_ids.append(j["id"])
                    print(f"  Marked applied: {j['id']}")
                else:
                    job_store.mark_applied(j["id"], applied=False,
                                           error="Form not confirmed — manual review needed")
                    skipped.append(j)
            except Exception as exc:
                err = str(exc)[:200]
                logger.warning(f"  [{j.get('company')}] Unexpected error: {err}")
                job_store.mark_applied(j["id"], applied=False, error=err)
                skipped.append(j)
            time.sleep(RATE_SLEEP)

        ctx.close()

    shutil.rmtree(tmp, ignore_errors=True)

    print("\n" + "=" * 60)
    print(f"  Greenhouse apply complete")
    print(f"  Applied:  {len(applied_ids)}")
    print(f"  Skipped/failed: {len(skipped)}")
    print("=" * 60)

    if skipped:
        print("\nNeeds manual attention:")
        for j in skipped:
            print(f"  {j.get('title')} @ {j.get('company')}")
            print(f"    {j.get('apply_link')}")


if __name__ == "__main__":
    main()
