"""
automation_log.py — Persistent run log for every automation attempt.

Stored at: profiles/{slug}/output/automation_run_log.json

Each record:
  run_id, job_id, title, company, ats, score,
  status, error, started_at, finished_at, apply_link

Statuses:
  running  — attempt started, not yet finished
  success  — application submitted successfully
  failed   — known failure (oauth wall, dedup skip, form error)
  error    — unhandled exception
  stuck    — running for > STUCK_MINUTES with no finish (process died)
  skipped  — dedup / daily-limit skip
"""

import json
import threading
import uuid
from datetime import datetime
from pathlib import Path

import profiles

_lock = threading.Lock()
STUCK_MINUTES = 15   # running > 15 min with no finish → stuck


# ── Internal helpers ─────────────────────────────────────────────────────────

def _log_file() -> Path:
    f = profiles.output_dir() / "automation_run_log.json"
    f.parent.mkdir(parents=True, exist_ok=True)
    return f


def _read() -> list[dict]:
    f = _log_file()
    if f.exists():
        try:
            return json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            return []
    return []


def _write(records: list[dict]):
    _log_file().write_text(
        json.dumps(records, indent=2, ensure_ascii=False), encoding="utf-8"
    )


# ── Public API ────────────────────────────────────────────────────────────────

def log_start(job: dict) -> str:
    """Record that automation started on this job. Returns run_id."""
    run_id = uuid.uuid4().hex[:12]
    record = {
        "run_id":      run_id,
        "job_id":      job.get("id", ""),
        "title":       job.get("title", ""),
        "company":     job.get("company", ""),
        "ats":         job.get("ats_type", ""),
        "score":       job.get("fit_score", 0),
        "status":      "running",
        "error":       None,
        "started_at":  datetime.now().isoformat(timespec="seconds"),
        "finished_at": None,
        "apply_link":  job.get("apply_link", ""),
    }
    with _lock:
        records = _read()
        records.append(record)
        _write(records)
    return run_id


def log_finish(run_id: str, status: str, error: str = None):
    """Update an existing run record with its final status.

    status: success | failed | error | skipped
    """
    with _lock:
        records = _read()
        for r in records:
            if r["run_id"] == run_id:
                r["status"]      = status
                r["error"]       = error or None
                r["finished_at"] = datetime.now().isoformat(timespec="seconds")
                break
        _write(records)


def all_runs(limit: int = 1000) -> list[dict]:
    """Return run records newest-first, with stuck detection applied."""
    records = _read()
    now = datetime.now()
    for r in records:
        if r.get("status") == "running" and r.get("started_at"):
            try:
                started = datetime.fromisoformat(r["started_at"])
                if (now - started).total_seconds() > STUCK_MINUTES * 60:
                    r["status"] = "stuck"
            except Exception:
                pass
    # newest first, cap at limit
    return list(reversed(records[-limit:]))


def run_counts() -> dict:
    """Fast summary counts for all statuses."""
    runs = all_runs()
    counts = {"running": 0, "success": 0, "failed": 0, "error": 0, "stuck": 0, "skipped": 0}
    for r in runs:
        s = r.get("status", "")
        if s in counts:
            counts[s] += 1
    counts["total"] = len(runs)
    return counts


def pending_jobs(min_score: int = 8) -> list[dict]:
    """Jobs eligible for automation that haven't been attempted yet."""
    import job_store
    AUTOMATABLE = {"workday", "greenhouse", "eightfold", "smartrecruiters"}
    jobs = job_store.all_jobs()
    return sorted(
        [j for j in jobs
         if j.get("ats_type") in AUTOMATABLE
         and j.get("fit_score", 0) >= min_score
         and not j.get("applied_at")
         and j.get("apply_status") != "failed"],
        key=lambda j: j.get("fit_score", 0),
        reverse=True,
    )
