"""
linkedin_contacts.py
Finds possible referral contacts from your LinkedIn Connections export.

Drop your official LinkedIn export at output/linkedin_connections.csv
(Settings & Privacy -> Get a copy of your data -> Connections).
No scraping involved — this is your own sanctioned data export.
"""

import csv
import logging
import re

import profiles

logger = logging.getLogger(__name__)

_STOP_WORDS = {"private", "limited", "pvt", "ltd", "inc", "corp", "technologies",
               "solutions", "services", "consulting", "india", "the", "and", "co"}


def _company_key(company: str) -> str:
    """First meaningful word of a company name, splitting concatenated/camelCase names."""
    cleaned = re.sub(r"[^a-zA-Z0-9]", " ", company or "")
    cleaned = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", cleaned)
    words = [w for w in cleaned.split() if w.lower() not in _STOP_WORDS and len(w) > 2]
    return words[0].lower() if words else (company or "").strip().lower()


def _load_connections() -> list[dict]:
    """Parse LinkedIn's Connections.csv, skipping the notes preamble LinkedIn adds above the header."""
    connections_file = profiles.linkedin_csv_path()
    if not connections_file.exists():
        return []
    try:
        with connections_file.open(encoding="utf-8-sig", newline="") as f:
            lines = f.readlines()
        header_idx = next(
            (i for i, line in enumerate(lines) if line.strip().startswith("First Name")),
            None,
        )
        if header_idx is None:
            logger.warning("linkedin_connections.csv: couldn't find header row")
            return []
        reader = csv.DictReader(lines[header_idx:])
        return [row for row in reader if row.get("First Name")]
    except Exception as e:
        logger.warning(f"Failed to read linkedin_connections.csv: {e}")
        return []


def find_connections_at_company(company: str) -> list[dict]:
    """Return connections whose 'Company' field matches the given company name."""
    if not company:
        return []
    key = _company_key(company)
    if not key:
        return []

    matches = []
    for row in _load_connections():
        conn_company = row.get("Company", "")
        if key in _company_key(conn_company):
            matches.append({
                "name":         f"{row.get('First Name', '').strip()} {row.get('Last Name', '').strip()}".strip(),
                "position":     row.get("Position", "").strip(),
                "company":      conn_company.strip(),
                "email":        row.get("Email Address", "").strip(),
                "connected_on": row.get("Connected On", "").strip(),
                "profile_url":  row.get("URL", "").strip(),
            })
    return matches
