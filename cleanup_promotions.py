"""
cleanup_promotions.py
Finds Promotions/Spam-category emails via Gmail's own IMAP category search
(not a custom spam heuristic) and, when confirmed, moves them to Trash —
never a permanent delete. Uses the same GMAIL_ADDRESS/GMAIL_APP_PASSWORD as
gmail_checker.py.

Usage:
  python cleanup_promotions.py              # dry run — counts + sample senders only
  python cleanup_promotions.py --move       # actually move matches to Trash
"""

import imaplib
import os
import sys
from collections import Counter
from email.header import decode_header
from dotenv import load_dotenv

load_dotenv()

CATEGORIES = ["promotions", "spam"]


def _decode(raw) -> str:
    if not raw:
        return ""
    parts = decode_header(raw)
    out = []
    for text, enc in parts:
        if isinstance(text, bytes):
            text = text.decode(enc or "utf-8", errors="replace")
        out.append(text)
    return "".join(out)


def _connect():
    addr = os.getenv("GMAIL_ADDRESS")
    pwd = os.getenv("GMAIL_APP_PASSWORD")
    if not addr or not pwd:
        raise EnvironmentError("GMAIL_ADDRESS / GMAIL_APP_PASSWORD not set in .env")
    mail = imaplib.IMAP4_SSL("imap.gmail.com")
    mail.login(addr, pwd)
    return mail


def find_matches(mail, category: str) -> list[bytes]:
    """Search Gmail's own category label via the X-GM-RAW IMAP extension —
    this is Gmail's actual Promotions/Spam classification, not a guess."""
    mail.select('"[Gmail]/All Mail"', readonly=True)
    typ, data = mail.uid("SEARCH", "X-GM-RAW", f'"category:{category}"')
    if typ != "OK" or not data or not data[0]:
        return []
    return data[0].split()


def preview(mail, uids: list[bytes], limit: int = 200) -> Counter:
    senders = Counter()
    for uid in uids[:limit]:
        typ, msg_data = mail.uid("FETCH", uid, "(BODY.PEEK[HEADER.FIELDS (FROM)])")
        if typ != "OK" or not msg_data or not msg_data[0]:
            continue
        header = msg_data[0][1].decode("utf-8", errors="replace")
        from_line = header.replace("From:", "").strip()
        senders[_decode(from_line)] += 1
    return senders


def move_to_trash(mail, uids: list[bytes]):
    uid_set = b",".join(uids)
    mail.select('"[Gmail]/All Mail"')
    typ, _ = mail.uid("MOVE", uid_set, '"[Gmail]/Trash"')
    return typ == "OK"


def main():
    do_move = "--move" in sys.argv
    mail = _connect()
    print(f"Connected as {os.getenv('GMAIL_ADDRESS')}\n")

    all_matches: dict[str, list[bytes]] = {}
    for cat in CATEGORIES:
        uids = find_matches(mail, cat)
        all_matches[cat] = uids
        print(f"category:{cat} — {len(uids)} emails")
        if uids:
            senders = preview(mail, uids)
            for sender, count in senders.most_common(10):
                print(f"    {count:4d}  {sender}")
        print()

    total = sum(len(v) for v in all_matches.values())
    if not do_move:
        print(f"DRY RUN — {total} emails would move to Trash. Re-run with --move to actually do it.")
    else:
        for cat, uids in all_matches.items():
            if not uids:
                continue
            ok = move_to_trash(mail, uids)
            print(f"category:{cat} — moved {len(uids)} emails to Trash: {'OK' if ok else 'FAILED'}")
        print(f"\nDone — {total} emails moved to Trash (recoverable there for 30 days).")

    mail.logout()


if __name__ == "__main__":
    main()
