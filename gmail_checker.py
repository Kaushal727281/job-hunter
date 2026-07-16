"""
gmail_checker.py
Checks Gmail inbox for responses to jobs you've applied for.
Uses IMAP with your Gmail App Password — no extra API keys needed.
"""

import imaplib
import email
import json
import os
import logging
import re
from datetime import datetime, timezone
from email.header import decode_header
from email.utils import parsedate_to_datetime
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)
_CONFIG_FILE = Path(__file__).parent / "config.json"


def _get_gmail_address() -> str:
    """Return Gmail address: .env GMAIL_ADDRESS first, then config.json candidate.email."""
    addr = os.getenv("GMAIL_ADDRESS", "")
    if addr:
        return addr
    try:
        return json.loads(_CONFIG_FILE.read_text(encoding="utf-8")).get("candidate", {}).get("email", "")
    except Exception:
        return ""


def _decode_str(raw) -> str:
    if raw is None:
        return ""
    parts = decode_header(raw)
    result = []
    for decoded, charset in parts:
        if isinstance(decoded, bytes):
            result.append(decoded.decode(charset or "utf-8", errors="replace"))
        else:
            result.append(str(decoded))
    return "".join(result)


def _get_body(msg) -> str:
    """Extract plain-text body snippet from email. Falls back to HTML→text."""
    plain, html_part = "", ""
    try:
        if msg.is_multipart():
            for part in msg.walk():
                ct = part.get_content_type()
                payload = part.get_payload(decode=True)
                if not payload:
                    continue
                text = payload.decode("utf-8", errors="replace")
                if ct == "text/plain" and not plain:
                    plain = text[:2000]
                elif ct == "text/html" and not html_part:
                    html_part = text[:100_000]   # parse full HTML; truncate text after extraction
        else:
            payload = msg.get_payload(decode=True)
            if payload:
                plain = payload.decode("utf-8", errors="replace")[:2000]
    except Exception:
        pass

    import html as _html

    def _clean(text: str) -> str:
        """Unescape HTML entities, strip ERB/template artifacts, collapse whitespace."""
        text = _html.unescape(text)
        text = re.sub(r'<%.*?%>', '', text, flags=re.DOTALL)   # closed tags
        text = re.sub(r'<%.*', '', text, flags=re.DOTALL)       # unclosed/truncated tags
        text = re.sub(r'\s+', ' ', text).strip()
        return text

    def _html_to_text(raw: str) -> str:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(raw, "html.parser")
        for tag in soup(["script", "style", "head"]):
            tag.decompose()
        return _clean(soup.get_text(separator=" ", strip=True))

    def _looks_like_html(text: str) -> bool:
        return bool(re.search(r'<[a-zA-Z][^>]{0,50}>', text))

    if plain and not _looks_like_html(plain):
        text = _clean(plain)
        if len(text) > 20:
            return text[:300]

    # Use HTML part (or plain that is actually HTML markup)
    source = html_part or (plain if _looks_like_html(plain) else "")
    if source:
        text = _html_to_text(source)
        return text[:300]

    return ""


def _imap_since(applied_at: str) -> str:
    """Convert applied_at ISO string to IMAP SINCE date string (e.g. '16-Jul-2026')."""
    try:
        dt = datetime.fromisoformat(applied_at)
        return dt.strftime("%d-%b-%Y")
    except Exception:
        return ""


def _email_after_apply(date_str: str, applied_at: str) -> bool:
    """Return True if the email's Date header is on or after the applied_at datetime."""
    if not applied_at:
        return True   # no apply date recorded — allow everything
    try:
        applied_dt = datetime.fromisoformat(applied_at).replace(tzinfo=timezone.utc)
        email_dt   = parsedate_to_datetime(date_str)
        # Make both offset-aware for comparison
        if email_dt.tzinfo is None:
            email_dt = email_dt.replace(tzinfo=timezone.utc)
        return email_dt >= applied_dt
    except Exception:
        return True   # can't parse — allow through


def _company_key(company: str) -> str:
    """First meaningful word of the company name for search."""
    stop = {"private", "limited", "pvt", "ltd", "inc", "corp", "technologies",
            "solutions", "services", "consulting", "india", "the", "and"}
    words = [w for w in re.sub(r"[^a-zA-Z0-9 ]", "", company).split()
             if w.lower() not in stop and len(w) > 2]
    return words[0] if words else company.split()[0] if company.split() else ""


def check_responses(applied_jobs: list[dict]) -> dict:
    """
    Scan Gmail inbox for responses to the given applied jobs.

    Searches for emails where FROM or SUBJECT contains the company name,
    received after the job was marked applied.

    Returns:
        { job_id: [ {from, subject, snippet, date, msg_id}, ... ] }
    """
    addr = _get_gmail_address()
    pwd  = os.getenv("GMAIL_APP_PASSWORD")
    if not addr or not pwd:
        raise EnvironmentError("GMAIL_ADDRESS / GMAIL_APP_PASSWORD not set in .env or config.json")

    if not applied_jobs:
        return {}

    results: dict[str, list] = {}

    try:
        mail = imaplib.IMAP4_SSL("imap.gmail.com")
        mail.login(addr, pwd)
        mail.select("inbox")
        logger.info(f"Gmail IMAP connected — checking {len(applied_jobs)} applied jobs")

        for job in applied_jobs:
            job_id     = job["id"]
            company    = job.get("company", "")
            title      = job.get("title", "")
            applied_at = job.get("applied_at", "")
            key        = _company_key(company)
            if not key:
                continue

            # Only look for emails received on or after the apply date
            since_date = _imap_since(applied_at)
            since_clause = f'SINCE "{since_date}" ' if since_date else ""
            logger.info(f"  Searching for '{company}' responses{f' since {since_date}' if since_date else ''}")

            seen_ids: set = set()
            responses = []

            # Search by company name in FROM and SUBJECT, filtered by apply date
            for criterion in [f'{since_clause}FROM "{key}"', f'{since_clause}SUBJECT "{key}"']:
                try:
                    status, data = mail.search(None, criterion)
                    if status != "OK" or not data[0]:
                        continue
                    for mid in data[0].split()[-15:]:   # last 15 per criterion
                        if mid in seen_ids:
                            continue
                        seen_ids.add(mid)
                        status2, raw = mail.fetch(mid, "(RFC822)")
                        if status2 != "OK":
                            continue
                        msg = email.message_from_bytes(raw[0][1])
                        subject  = _decode_str(msg.get("Subject", ""))
                        from_addr = _decode_str(msg.get("From", ""))
                        date_str = msg.get("Date", "")
                        snippet  = _get_body(msg)

                        # Skip our own sent emails / no-reply
                        if addr.lower() in from_addr.lower():
                            continue

                        # Skip emails received before the job was applied for
                        if not _email_after_apply(date_str, applied_at):
                            logger.debug(f"  Skipping pre-apply email: {date_str[:30]} < {applied_at}")
                            continue

                        # Build a direct Gmail URL using the RFC822 Message-ID header
                        # Format: https://mail.google.com/mail/u/0/#search/rfc822msgid:<id>
                        import urllib.parse
                        # authuser= forces Gmail to open in the correct account
                        gmail_acct = _get_gmail_address()
                        authuser = f"?authuser={urllib.parse.quote(gmail_acct)}" if gmail_acct else ""
                        if subject:
                            gmail_url = (
                                f"https://mail.google.com/mail/u/0{authuser}#search/"
                                + urllib.parse.quote(f'subject:"{subject}"')
                            )
                        else:
                            raw_msg_id = msg.get("Message-ID", "")
                            clean_msg_id = raw_msg_id.strip().strip("<>")
                            gmail_url = (
                                f"https://mail.google.com/mail/u/0{authuser}#search/"
                                + urllib.parse.quote(f"rfc822msgid:{clean_msg_id}")
                            )

                        responses.append({
                            "from":      from_addr,
                            "subject":   subject,
                            "snippet":   snippet,
                            "date":      date_str,
                            "msg_id":    mid.decode(),
                            "gmail_url": gmail_url,
                        })
                except Exception as e:
                    logger.debug(f"IMAP search failed [{criterion}]: {e}")

            if responses:
                logger.info(f"  {company}: {len(responses)} email(s) found")
                results[job_id] = responses

        mail.logout()

    except imaplib.IMAP4.error as e:
        logger.error(f"Gmail IMAP login failed: {e}")
    except Exception as e:
        logger.error(f"Gmail check error: {e}")

    return results
