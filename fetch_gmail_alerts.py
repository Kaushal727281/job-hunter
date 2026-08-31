#!/usr/bin/env python3
"""
fetch_gmail_alerts.py
---------------------
Reads Gmail job-alert emails (LinkedIn, Indeed, Google Jobs, etc.) and
adds discovered jobs into the active profile's job store for scoring +
auto-apply.

Requires in .env:
    GMAIL_ADDRESS       — your Gmail address
    GMAIL_APP_PASSWORD  — Google App Password (16-char, not your account password)

Usage:
    python3 fetch_gmail_alerts.py                      # process recent alert emails
    python3 fetch_gmail_alerts.py --days 7             # look back 7 days (default 3)
    python3 fetch_gmail_alerts.py --no-score           # skip AI scoring
    python3 fetch_gmail_alerts.py --dry-run            # print jobs, don't save
    python3 fetch_gmail_alerts.py --profile kaushal-kumar-jha
"""

import email as emaillib
import hashlib
import imaplib
import json
import logging
import os
import re
import sys
import time
from datetime import date, datetime, timedelta
from email.header import decode_header
from pathlib import Path

import dotenv
dotenv.load_dotenv(os.path.join(os.path.dirname(__file__), ".env"), override=False)

import requests
import truststore
import urllib3
truststore.inject_into_ssl()
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

from bs4 import BeautifulSoup

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

# ── Known job-alert senders ────────────────────────────────────────────────────
# Confirmed senders from Kaushal's inbox:
#   naukrialerts@naukri.com               — Naukri job alert digests
#   jobalerts-noreply@linkedin.com        — LinkedIn job alerts
#   donotreply@email.careers.microsoft.com — Microsoft Careers recommendations
#   no-reply@indeed.com                   — Indeed alerts (fallback)

ALERT_SENDERS = [
    "naukrialerts@naukri.com",
    "jobalerts-noreply@linkedin.com",
    "donotreply@email.careers.microsoft.com",
    "no-reply@indeed.com",
    "alert@indeed.com",
    "noreply@glassdoor.com",
    "jobs-noreply@google.com",
]

# ── India location keywords ────────────────────────────────────────────────────

_INDIA_KEYS = (
    "india", "bengaluru", "bangalore", "hyderabad", "pune",
    "chennai", "mumbai", "noida", "gurugram", "gurgaon", "remote",
    "blr", "hyd",
)


def _is_india_or_remote(loc: str) -> bool:
    s = (loc or "").lower()
    if not s:
        return True  # No location = keep (might be remote)
    return any(k in s for k in _INDIA_KEYS)


# ── ATS URL detection ──────────────────────────────────────────────────────────

def _detect_ats(url: str) -> str:
    u = url.lower()
    if "myworkdayjobs.com" in u:
        return "workday"
    if "greenhouse.io" in u:
        return "greenhouse"
    if "lever.co" in u:
        return "lever"
    if "smartrecruiters.com" in u:
        return "smartrecruiters"
    if "ashbyhq.com" in u or "jobs.ashbyhq.com" in u:
        return "ashby"
    if "apply.careers.microsoft.com" in u:
        return "eightfold"
    if "account.amazon.jobs" in u or "amazon.jobs" in u:
        return "amazon"
    if "linkedin.com/jobs" in u or "linkedin.com/comm/jobs" in u:
        return "linkedin"
    if "indeed.com" in u:
        return "indeed"
    if "naukri.com" in u:
        return "naukri"
    if "glassdoor.com" in u:
        return "glassdoor"
    return "custom"


def _resolve_redirect(url: str, timeout: int = 5) -> str:
    """Follow HTTP redirects to find the final URL. Returns original on failure."""
    try:
        r = requests.head(url, allow_redirects=True, timeout=timeout,
                          headers={"User-Agent": "Mozilla/5.0"})
        return r.url
    except Exception:
        return url


# ── Email body helpers ─────────────────────────────────────────────────────────

def _decode_str(val: str) -> str:
    parts = decode_header(val or "")
    out = []
    for part, enc in parts:
        if isinstance(part, bytes):
            out.append(part.decode(enc or "utf-8", errors="replace"))
        else:
            out.append(part)
    return "".join(out)


def _get_html_body(msg) -> str:
    """Return the HTML body from an email.Message object."""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                charset = part.get_content_charset() or "utf-8"
                try:
                    return part.get_payload(decode=True).decode(charset, errors="replace")
                except Exception:
                    pass
    else:
        if msg.get_content_type() == "text/html":
            charset = msg.get_content_charset() or "utf-8"
            try:
                return msg.get_payload(decode=True).decode(charset, errors="replace")
            except Exception:
                pass
    return ""


# ── LinkedIn email parser ──────────────────────────────────────────────────────

_LI_JOB_URL_RE = re.compile(
    r'https://(?:www\.)?linkedin\.com/(?:comm/)?jobs/view/(\d+)',
    re.IGNORECASE,
)


def _parse_linkedin_html(html: str) -> list[dict]:
    """
    Extract job listings from a LinkedIn job-alert email HTML body.
    Each result dict has: title, company, location, url, raw_href
    """
    soup = BeautifulSoup(html, "html.parser")
    jobs: list[dict] = []
    seen_ids: set[str] = set()

    for a in soup.find_all("a", href=True):
        href = a["href"]
        m = _LI_JOB_URL_RE.search(href)
        if not m:
            continue
        job_id = m.group(1)
        if job_id in seen_ids:
            continue
        seen_ids.add(job_id)

        title = a.get_text(strip=True)
        # Skip short/CTA text
        if len(title) < 6 or title.lower() in (
            "view", "apply", "see", "more", "view job", "apply now",
            "see more", "view all", "click here",
        ):
            continue

        # Find company + location in surrounding table cell / div
        company, location = "", ""
        container = (
            a.find_parent("td")
            or a.find_parent("li")
            or a.find_parent("div")
            or a.find_parent("tr")
        )
        if container:
            texts = [t.strip() for t in container.stripped_strings if t.strip()]
            others = [t for t in texts if t != title and len(t) > 2
                      and not _LI_JOB_URL_RE.search(t)]
            if others:
                # Often "Company · Location" or separate strings
                first = others[0]
                if "·" in first:
                    parts = first.split("·", 1)
                    company  = parts[0].strip()
                    location = parts[1].strip()
                else:
                    company = first
                    if len(others) > 1:
                        location = others[1]

        jobs.append({
            "title":    title,
            "company":  company or "Unknown",
            "location": location,
            "url":      f"https://www.linkedin.com/jobs/view/{job_id}",
            "raw_href": href,
        })

    return jobs


# ── Indeed email parser ────────────────────────────────────────────────────────

_INDEED_URL_RE = re.compile(
    r'https://(?:www\.)?indeed\.com/(?:viewjob|rc/clk|m/jobs)[^\s"\'<>]*',
    re.IGNORECASE,
)


def _parse_indeed_html(html: str) -> list[dict]:
    """Extract job listings from an Indeed job-alert email HTML body."""
    soup = BeautifulSoup(html, "html.parser")
    jobs: list[dict] = []
    seen: set[str] = set()

    for a in soup.find_all("a", href=True):
        href = a["href"]
        if not _INDEED_URL_RE.match(href):
            continue
        if "jk=" not in href and "viewjob" not in href:
            continue
        # Normalise to a stable key (strip query noise)
        key = re.sub(r'[?&]utm_[^&]+', '', href)
        if key in seen:
            continue
        seen.add(key)

        title = a.get_text(strip=True)
        if len(title) < 6:
            continue

        company, location = "", ""
        container = a.find_parent("td") or a.find_parent("div")
        if container:
            texts = [t.strip() for t in container.stripped_strings if t.strip()]
            others = [t for t in texts if t != title and len(t) > 2]
            if others:
                company = others[0]
            if len(others) > 1:
                location = others[1]

        jobs.append({
            "title":    title,
            "company":  company or "Unknown",
            "location": location,
            "url":      href,
            "raw_href": href,
        })

    return jobs


# ── Naukri email parser ───────────────────────────────────────────────────────

_NAUKRI_JOB_URL_RE = re.compile(
    r'https://www\.naukri\.com/jd/job-listings-[^\s"\'<>&]+',
    re.IGNORECASE,
)


def _parse_naukri_html(html: str) -> list[dict]:
    """
    Extract job listings from a Naukri job-alert email HTML body.

    Each job card has two <a> tags pointing at the same URL:
      1. Combined text: "Job TitleCompany3.4Hybrid - Bengaluru"
      2. Title-only:    "Job Title"

    We extract from the card that has the richest context array
    (title / company / rating / location as separate <td> or <div> strings).
    """
    soup = BeautifulSoup(html, "html.parser")
    jobs: list[dict] = []
    seen_urls: set[str] = set()

    for a in soup.find_all("a", href=True):
        href = a["href"]
        if not _NAUKRI_JOB_URL_RE.match(href):
            continue
        # Normalise URL (strip utm query params)
        base_url = href.split("?")[0].rstrip("/")
        if base_url in seen_urls:
            continue
        seen_urls.add(base_url)

        # Find surrounding container
        container = (
            a.find_parent("td")
            or a.find_parent("tr")
            or a.find_parent("div")
        )
        ctx = [t.strip() for t in (container or a).stripped_strings if t.strip()]

        # ctx usually: ['Job Title', 'Company', '3.4', 'Bengaluru, Hyderabad']
        # or:          ['Job Title', 'Company', 'Hybrid - Bengaluru']
        title   = ctx[0] if ctx else a.get_text(strip=True)
        company = ""
        location = ""
        if len(ctx) >= 2:
            # ctx[1] is company; ctx[2] might be rating (float-like) or location
            company = ctx[1]
        if len(ctx) >= 3:
            # Skip rating-like string (e.g. "3.4", "4.1")
            for part in ctx[2:]:
                if re.match(r"^\d+\.\d+$", part):
                    continue
                location = part
                break

        if not title or len(title) < 5:
            continue

        jobs.append({
            "title":    title,
            "company":  company or "Unknown",
            "location": location,
            "url":      base_url,
            "raw_href": href,
        })

    return jobs


# ── Microsoft Careers email parser ────────────────────────────────────────────

_MS_JOB_URL_RE = re.compile(
    r'https://(?:jobs|careers)\.microsoft\.com/[^\s"\'<>&]+',
    re.IGNORECASE,
)


def _parse_microsoft_html(html: str) -> list[dict]:
    """Extract job links from Microsoft Careers recommendation emails."""
    soup = BeautifulSoup(html, "html.parser")
    jobs: list[dict] = []
    seen: set[str] = set()

    for a in soup.find_all("a", href=True):
        href = a["href"]
        if not _MS_JOB_URL_RE.match(href):
            continue
        base = href.split("?")[0].rstrip("/")
        if base in seen:
            continue
        seen.add(base)

        title = a.get_text(strip=True)
        if len(title) < 5:
            continue

        container = a.find_parent("td") or a.find_parent("div")
        ctx = [t.strip() for t in (container or a).stripped_strings if t.strip()]
        location = ""
        for part in ctx[1:]:
            if part != title and len(part) > 2:
                location = part
                break

        jobs.append({
            "title":    title,
            "company":  "Microsoft",
            "location": location,
            "url":      base,
            "raw_href": href,
        })

    return jobs


# ── Generic URL extractor (fallback for other alert senders) ──────────────────

_GENERIC_JOB_URL_RE = re.compile(
    r'https://[^\s"\'<>]*(myworkdayjobs|greenhouse\.io|lever\.co'
    r'|smartrecruiters|ashbyhq\.com)[^\s"\'<>]*',
    re.IGNORECASE,
)


def _parse_generic_html(html: str) -> list[dict]:
    """
    Last-resort parser: extract any direct ATS URLs from the email HTML.
    Returns list of partial dicts (company/location left blank for scorer to fill).
    """
    soup = BeautifulSoup(html, "html.parser")
    jobs = []
    seen: set[str] = set()

    for a in soup.find_all("a", href=True):
        href = a["href"]
        if not _GENERIC_JOB_URL_RE.search(href):
            continue
        if href in seen:
            continue
        seen.add(href)

        title = a.get_text(strip=True)
        if len(title) < 6:
            title = href.split("/")[-1].replace("-", " ").title()

        jobs.append({
            "title":    title,
            "company":  "Unknown",
            "location": "",
            "url":      href,
            "raw_href": href,
        })

    return jobs


# ── Seen-email UID tracking ────────────────────────────────────────────────────

def _seen_path() -> Path:
    import profiles
    return profiles.jobs_store_path().parent / "gmail_seen_email_ids.json"


def _load_seen() -> set[str]:
    p = _seen_path()
    if p.exists():
        try:
            return set(json.loads(p.read_text(encoding="utf-8")))
        except Exception:
            return set()
    return set()


def _save_seen(ids: set[str]):
    p = _seen_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(sorted(ids)), encoding="utf-8")


# ── Job object builder ─────────────────────────────────────────────────────────

def _make_job(title: str, company: str, location: str,
              url: str, source_sender: str, ats_type: str) -> dict:
    uid = hashlib.md5(f"gmail_{url}_{title}".encode()).hexdigest()
    return {
        "id":           f"gmail_{uid[:12]}",
        "title":        title,
        "company":      company,
        "location":     location,
        "apply_link":   url,
        "ats_type":     ats_type,
        "source":       "gmail_alert",
        "source_detail": source_sender,
        "description":  "",
        "posted_date":  str(date.today()),
    }


# ── Main fetch function ────────────────────────────────────────────────────────

def fetch_from_gmail(days_back: int = 3, dry_run: bool = False) -> list[dict]:
    gmail_user = os.environ.get("GMAIL_ADDRESS", "")
    gmail_pass = os.environ.get("GMAIL_APP_PASSWORD", "")
    if not gmail_user or not gmail_pass:
        logger.error("Set GMAIL_ADDRESS and GMAIL_APP_PASSWORD in .env")
        return []

    since_date = (datetime.now() - timedelta(days=days_back)).strftime("%d-%b-%Y")

    logger.info(f"Connecting to Gmail ({gmail_user})…")
    try:
        mail = imaplib.IMAP4_SSL("imap.gmail.com")
        mail.login(gmail_user, gmail_pass)
        mail.select("INBOX")
    except Exception as exc:
        logger.error(f"Gmail IMAP login failed: {exc}")
        return []

    seen_ids = _load_seen()
    all_jobs: list[dict] = []

    for sender in ALERT_SENDERS:
        criteria = f'(FROM "{sender}" SINCE {since_date})'
        _, data = mail.search(None, criteria)
        msg_nums = (data[0] or b"").split()
        if not msg_nums:
            continue

        logger.info(f"[{sender}] {len(msg_nums)} email(s) found")

        for num in msg_nums:
            uid = num.decode()
            if uid in seen_ids:
                continue

            _, raw = mail.fetch(num, "(RFC822)")
            if not raw or not raw[0]:
                seen_ids.add(uid)
                continue

            msg     = emaillib.message_from_bytes(raw[0][1])
            subject = _decode_str(msg.get("Subject", ""))
            logger.info(f"  → {subject[:90]}")

            html = _get_html_body(msg)
            if not html:
                seen_ids.add(uid)
                continue

            # Choose parser by sender
            raw_jobs: list[dict] = []
            if "linkedin.com" in sender:
                raw_jobs = _parse_linkedin_html(html)
            elif "naukri.com" in sender:
                raw_jobs = _parse_naukri_html(html)
            elif "microsoft.com" in sender:
                raw_jobs = _parse_microsoft_html(html)
            elif "indeed.com" in sender:
                raw_jobs = _parse_indeed_html(html)
            else:
                raw_jobs = (
                    _parse_linkedin_html(html)
                    or _parse_naukri_html(html)
                    or _parse_indeed_html(html)
                    or _parse_generic_html(html)
                )

            logger.info(f"    {len(raw_jobs)} job card(s) extracted")

            for rj in raw_jobs:
                loc = rj.get("location", "")
                if not _is_india_or_remote(loc):
                    continue

                url      = rj["url"]
                raw_href = rj.get("raw_href", url)
                ats      = _detect_ats(url)

                # For LinkedIn URLs, follow the redirect — some link directly to ATS
                if ats == "linkedin" and raw_href != url:
                    try:
                        final = _resolve_redirect(raw_href, timeout=4)
                        final_ats = _detect_ats(final)
                        if final_ats not in ("linkedin", "custom", "glassdoor"):
                            url = final
                            ats = final_ats
                            logger.info(f"      Resolved → [{ats}] {url[:70]}")
                    except Exception:
                        pass

                job = _make_job(
                    title=rj["title"],
                    company=rj["company"],
                    location=loc,
                    url=url,
                    source_sender=sender,
                    ats_type=ats,
                )
                all_jobs.append(job)

            seen_ids.add(uid)
            time.sleep(0.2)

    try:
        mail.logout()
    except Exception:
        pass

    if not dry_run:
        _save_seen(seen_ids)

    logger.info(f"Total: {len(all_jobs)} India/remote jobs found in Gmail alerts")
    return all_jobs


# ── CLI ────────────────────────────────────────────────────────────────────────

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Fetch jobs from Gmail job alert emails")
    parser.add_argument("--profile",  default=os.environ.get("CANDIDATE_PROFILE_SLUG", ""))
    parser.add_argument("--days",     type=int, default=3,
                        help="How many days back to scan (default 3)")
    parser.add_argument("--no-score", action="store_true", help="Skip AI scoring")
    parser.add_argument("--dry-run",  action="store_true", help="Print, don't save")
    args = parser.parse_args()

    import profiles
    profiles.set_active_profile(args.profile)
    import job_store

    jobs = fetch_from_gmail(days_back=args.days, dry_run=args.dry_run)

    if not jobs:
        logger.info("No new jobs found in Gmail alerts.")
        return

    if args.dry_run:
        print(f"\n{'─'*70}")
        for j in jobs:
            print(f"  [{j['ats_type']:15}] {j['title'][:40]:<40}  @ {j['company'][:25]}")
            print(f"    {j['location'] or '(no location)'}  →  {j['apply_link'][:70]}")
        print(f"{'─'*70}\nTotal: {len(jobs)}")
        return

    new_ids = job_store.upsert_jobs_return_ids(jobs)
    logger.info(f"Added {len(new_ids)} new jobs from Gmail alerts")

    if not args.no_score and new_ids:
        import job_scorer
        logger.info(f"Scoring {len(new_ids)} new jobs…")
        job_scorer.score_jobs(new_ids)

    logger.info("Gmail alert fetch complete.")


if __name__ == "__main__":
    main()
