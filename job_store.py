"""
job_store.py — Simple JSON-based job store.
All jobs are persisted in output/jobs.json.
"""

import json
import threading
from pathlib import Path
from typing import Optional

STORE_FILE = Path(__file__).parent / "output" / "jobs.json"
_lock = threading.Lock()


def _read() -> list[dict]:
    STORE_FILE.parent.mkdir(exist_ok=True)
    if STORE_FILE.exists():
        try:
            return json.loads(STORE_FILE.read_text())
        except Exception:
            return []
    return []


def _write(jobs: list[dict]):
    STORE_FILE.parent.mkdir(exist_ok=True)
    STORE_FILE.write_text(json.dumps(jobs, indent=2, ensure_ascii=False))


def all_jobs() -> list[dict]:
    with _lock:
        return _read()


def get_job(job_id: str) -> Optional[dict]:
    with _lock:
        return next((j for j in _read() if j["id"] == job_id), None)


def upsert_jobs(new_jobs: list[dict]):
    """Add jobs that don't already exist (by id)."""
    with _lock:
        existing = _read()
        existing_ids = {j["id"] for j in existing}
        added = [j for j in new_jobs if j["id"] not in existing_ids]
        _write(existing + added)
        return len(added)


def update_job(job_id: str, **fields):
    """Update fields on a single job."""
    with _lock:
        jobs = _read()
        for j in jobs:
            if j["id"] == job_id:
                j.update(fields)
        _write(jobs)


def clear_all():
    with _lock:
        _write([])
