"""
email_verifier.py
Free SMTP-level email verification — no third-party API needed.

Connects to the target domain's mail server and asks (via RCPT TO) whether
it would accept a given address, without ever sending an actual message
(the connection is closed before DATA). Detects catch-all domains — which
accept any address, making verification meaningless — by probing a
deliberately bogus address on the same domain.

Note: relies on outbound port 25 being reachable. Some networks (notably
some corporate/mobile networks) block it; in that case results come back
as "unknown" rather than a false negative.
"""

import logging
import random
import smtplib
import string
from concurrent.futures import ThreadPoolExecutor

import dns.resolver

logger = logging.getLogger(__name__)

_TIMEOUT = 6
_HELO_DOMAIN = "job-hunter.local"
_PROBE_FROM = "verify@job-hunter.local"


def _mx_hosts(domain: str) -> list[str]:
    try:
        answers = dns.resolver.resolve(domain, "MX", lifetime=_TIMEOUT)
        return [str(r.exchange).rstrip(".") for r in sorted(answers, key=lambda r: r.preference)]
    except Exception as e:
        logger.debug(f"  MX lookup failed for {domain}: {e}")
        return []


def _rcpt_check(mx_host: str, rcpt_to: str) -> int | None:
    """Return the SMTP response code for RCPT TO, or None on connection failure."""
    try:
        with smtplib.SMTP(mx_host, 25, timeout=_TIMEOUT) as smtp:
            smtp.helo(_HELO_DOMAIN)
            smtp.mail(_PROBE_FROM)
            code, _ = smtp.rcpt(rcpt_to)
            return code
    except Exception as e:
        logger.debug(f"  RCPT check failed on {mx_host} for {rcpt_to}: {e}")
        return None


def verify_email(address: str) -> dict:
    """
    Check whether `address` is likely deliverable via an SMTP RCPT TO probe.
    Returns: {"status": "valid"|"invalid"|"catch_all"|"unknown", "detail": str}
    """
    if "@" not in address:
        return {"status": "invalid", "detail": "malformed address"}
    domain = address.split("@", 1)[1]

    hosts = _mx_hosts(domain)
    if not hosts:
        return {"status": "unknown", "detail": "no MX records found for domain"}

    for mx_host in hosts:
        real_code = _rcpt_check(mx_host, address)
        if real_code is None:
            continue  # try next MX host

        if real_code >= 500:
            return {"status": "invalid", "detail": f"rejected by mail server (SMTP {real_code})"}

        if real_code < 300:
            # Detect catch-all: does this server accept ANY random address?
            bogus_user = "verify-probe-" + "".join(random.choices(string.ascii_lowercase, k=12))
            bogus_code = _rcpt_check(mx_host, f"{bogus_user}@{domain}")
            if bogus_code is not None and bogus_code < 300:
                return {"status": "catch_all",
                        "detail": "domain accepts any address — can't confirm this one specifically"}
            return {"status": "valid", "detail": f"accepted by mail server (SMTP {real_code})"}

        return {"status": "unknown", "detail": f"ambiguous response (SMTP {real_code})"}

    return {"status": "unknown", "detail": "couldn't reach any mail server (port 25 may be blocked)"}


def verify_emails(addresses: list[str]) -> dict[str, dict]:
    """Verify a list of addresses in parallel. Returns {address: result}."""
    if not addresses:
        return {}
    with ThreadPoolExecutor(max_workers=min(8, len(addresses))) as pool:
        results = list(pool.map(verify_email, addresses))
    return dict(zip(addresses, results))
