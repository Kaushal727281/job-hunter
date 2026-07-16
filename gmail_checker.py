"""
gmail_checker.py
Checks Gmail inbox for responses to jobs you've applied for.
Uses IMAP with your Gmail App Password — no extra API keys needed.
"""

import imaplib
import email
import os
import logging
import re
from email.header import decode_header
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)


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
    """Extract plain-text body snippet from email."""
    try:
        if msg.is_multipart():
            for part in msg.walk():
                if part.get_content_type() == "text/plain":
                    payload = part.get_payload(decode=True)
                    if payload:
                        return payload.decode("utf-8", errors="replace")[:300].strip()
        else:
            payload = msg.get_payload(decode=True)
            if payload:
                return payload.decode("utf-8", errors="replace")[:300].strip()
    except Exception:
        pass
    return ""


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
    addr = os.getenv("GMAIL_ADDRESS")
    pwd  = os.getenv("GMAIL_APP_PASSWORD")
    if not addr or not pwd:
        raise EnvironmentError("GMAIL_ADDRESS / GMAIL_APP_PASSWORD not set in .env")

    if not applied_jobs:
        return {}

    results: dict[str, list] = {}

    try:
        mail = imaplib.IMAP4_SSL("imap.gmail.com")
        mail.login(addr, pwd)
        mail.select("inbox")
        logger.info(f"Gmail IMAP connected — checking {len(applied_jobs)} applied jobs")

        for job in applied_jobs:
            job_id  = job["id"]
            company = job.get("company", "")
            title   = job.get("title", "")
            key     = _company_key(company)
            if not key:
                continue

            seen_ids: set = set()
            responses = []

            # Search by company name in FROM and SUBJECT
            for criterion in [f'FROM "{key}"', f'SUBJECT "{key}"']:
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

                        responses.append({
                            "from":    from_addr,
                            "subject": subject,
                            "snippet": snippet,
                            "date":    date_str,
                            "msg_id":  mid.decode(),
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
