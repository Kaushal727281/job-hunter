"""
job_store.py — Simple JSON-based job store.
All jobs are persisted in output/jobs.json.
"""

import json
import threading
from typing import Optional

import profiles

_lock = threading.Lock()


def _store_file():
    return profiles.jobs_store_path()


def _read() -> list[dict]:
    store_file = _store_file()
    store_file.parent.mkdir(parents=True, exist_ok=True)
    if store_file.exists():
        try:
            return json.loads(store_file.read_text(encoding="utf-8"))
        except Exception:
            return []
    return []


def _write(jobs: list[dict]):
    store_file = _store_file()
    store_file.parent.mkdir(parents=True, exist_ok=True)
    store_file.write_text(json.dumps(jobs, indent=2, ensure_ascii=False), encoding="utf-8")


def all_jobs() -> list[dict]:
    with _lock:
        return [j for j in _read() if not j.get("removed")]


def get_job(job_id: str) -> Optional[dict]:
    with _lock:
        return next((j for j in _read() if j["id"] == job_id), None)


def upsert_jobs(new_jobs: list[dict]):
    """Add jobs that don't already exist (by id). Returns count added."""
    return len(upsert_jobs_return_ids(new_jobs))


def upsert_jobs_return_ids(new_jobs: list[dict]) -> list[str]:
    """Add jobs that don't already exist (by id). Returns list of new job IDs."""
    with _lock:
        existing = _read()
        existing_ids = {j["id"] for j in existing}
        added = [j for j in new_jobs if j["id"] not in existing_ids]
        _write(existing + added)
        return [j["id"] for j in added]


def update_job(job_id: str, **fields):
    """Update fields on a single job."""
    with _lock:
        jobs = _read()
        for j in jobs:
            if j["id"] == job_id:
                j.update(fields)
        _write(jobs)


def mark_applied(job_id: str, applied: bool = True, error: str = None):
    """
    Toggle the applied state for a job.
    If applied=True  → sets applied_at, apply_status="success"
    If applied=False → clears applied_at; if error given, sets apply_status="failed" + apply_error
    """
    from datetime import datetime
    with _lock:
        jobs = _read()
        for j in jobs:
            if j["id"] == job_id:
                if applied:
                    j["applied_at"]    = datetime.now().isoformat(timespec="seconds")
                    j["apply_status"]  = "success"
                    j.pop("apply_error", None)
                else:
                    j.pop("applied_at", None)
                    j.pop("email_responses", None)
                    if error:
                        j["apply_status"] = "failed"
                        j["apply_error"]  = error
                    else:
                        j.pop("apply_status", None)
                        j.pop("apply_error",  None)
        _write(jobs)


def set_responses(job_id: str, responses: list[dict]):
    """Store Gmail response emails for an applied job."""
    with _lock:
        jobs = _read()
        for j in jobs:
            if j["id"] == job_id:
                j["email_responses"] = responses
        _write(jobs)


def applied_jobs() -> list[dict]:
    """Return all jobs that have been marked as applied."""
    with _lock:
        return [j for j in _read() if j.get("applied_at")]


def remove_job(job_id: str):
    """Soft-delete a job so it never reappears after re-fetch."""
    with _lock:
        jobs = _read()
        for j in jobs:
            if j["id"] == job_id:
                j["removed"] = True
        _write(jobs)


def clear_all():
    with _lock:
        _write([])


def clear_untracked_jobs() -> list[str]:
    """Remove every job with no application history (no job_status, no
    applied_at) — the browse-only results of whatever resume/queries were
    active before. Jobs you've actually applied to or set a status on are
    never touched, regardless of resume changes. Returns the removed IDs so
    the caller can free them in job_fetcher's seen-jobs set for re-fetching."""
    with _lock:
        jobs = _read()
        tracked = [j for j in jobs if j.get("job_status") or j.get("applied_at")]
        removed_ids = [j["id"] for j in jobs if not (j.get("job_status") or j.get("applied_at"))]
        _write(tracked)
        return removed_ids
