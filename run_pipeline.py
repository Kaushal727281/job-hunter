#!/usr/bin/env python3
"""
run_pipeline.py
---------------
Master pipeline: fetch → score → apply

Steps:
  1. fetch   — run all ATS fetchers (workday, eightfold, greenhouse, smartrecruiters)
  2. score   — AI fit-score new/unscored jobs
  3. status  — show top candidates by score + ATS apply coverage
  4. apply   — trigger apply automation for each supported ATS (sorted by score)

Usage:
    python3 run_pipeline.py               # full pipeline
    python3 run_pipeline.py --fetch-only  # just fetch + score
    python3 run_pipeline.py --apply-only  # just apply (skip fetch)
    python3 run_pipeline.py --status      # just show dashboard, no fetch/apply
    python3 run_pipeline.py --min-score 8 # raise apply threshold
    python3 run_pipeline.py --dry-run     # show what would be applied, no submissions
    python3 run_pipeline.py --limit 5     # cap jobs per ATS applier
"""

import argparse
import logging
import os
import subprocess
import sys
import time
from collections import Counter

import dotenv
dotenv.load_dotenv(os.path.join(os.path.dirname(__file__), ".env"), override=False)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

PYTHON = sys.executable
PROFILE = os.environ.get("CANDIDATE_PROFILE_SLUG", "")


# ── Step 1: Fetch ─────────────────────────────────────────────────────────────

def run_fetch():
    fetchers = [
        ["fetch_workday.py",         "--no-score"],
        ["fetch_eightfold.py",       "--no-score"],
        ["fetch_greenhouse.py",      "--no-score"],
        ["fetch_smartrecruiters.py", "--no-score"],
        ["fetch_ashby.py",           "--no-score"],
        ["fetch_phenom.py",          "--no-score"],
        ["fetch_gmail_alerts.py",    "--no-score"],
    ]
    total_new = 0
    for script_args in fetchers:
        script = script_args[0]
        extra  = script_args[1:]
        logger.info(f"Running {script}...")
        result = subprocess.run(
            [PYTHON, script, "--profile", PROFILE] + extra,
            capture_output=False,
        )
        if result.returncode != 0:
            logger.warning(f"{script} exited with code {result.returncode}")
    return total_new


# ── Step 2: Score ─────────────────────────────────────────────────────────────

def run_score():
    import profiles, job_store, job_scorer
    profiles.set_active_profile(PROFILE)
    jobs = job_store.all_jobs()
    unscored_ids = [
        j["id"] for j in jobs
        if not j.get("fit_score") and not j.get("removed")
    ]
    if not unscored_ids:
        logger.info("All jobs already scored.")
        return
    logger.info(f"Scoring {len(unscored_ids)} unscored jobs...")
    job_scorer.score_jobs(unscored_ids)
    logger.info("Scoring complete.")


# ── Step 3: Status dashboard ──────────────────────────────────────────────────

def show_status(min_score: int = 7):
    import profiles, job_store
    profiles.set_active_profile(PROFILE)
    jobs = job_store.all_jobs()

    total     = len(jobs)
    scored    = [j for j in jobs if j.get("fit_score")]
    unscored  = [j for j in jobs if not j.get("fit_score") and not j.get("removed")]
    applied   = [j for j in jobs if j.get("applied_at")]
    high      = [j for j in scored if j.get("fit_score", 0) >= min_score and not j.get("applied_at") and not j.get("removed")]

    print("\n" + "=" * 70)
    print(f"  JOB PIPELINE DASHBOARD")
    print("=" * 70)
    print(f"  Total in store   : {total}")
    print(f"  Scored           : {len(scored)}")
    print(f"  Unscored         : {len(unscored)}")
    print(f"  Already applied  : {len(applied)}")
    print(f"  High-score ({min_score}+)  : {len(high)}  ← ready to apply")

    # By ATS
    print(f"\n  {'ATS':<22} {'Total':>6} {'Scored':>7} {'High({0}+)'.format(min_score):>8} {'Applied':>8} {'Automatable':>12}")
    print("  " + "-" * 65)
    AUTOMATED = {"workday", "greenhouse", "eightfold", "smartrecruiters"}
    by_ats = {}
    for j in jobs:
        ats = j.get("ats_type", "unknown")
        if ats not in by_ats:
            by_ats[ats] = {"total": 0, "scored": 0, "high": 0, "applied": 0}
        by_ats[ats]["total"] += 1
        if j.get("fit_score"):
            by_ats[ats]["scored"] += 1
        if j.get("fit_score", 0) >= min_score and not j.get("applied_at") and not j.get("removed"):
            by_ats[ats]["high"] += 1
        if j.get("applied_at"):
            by_ats[ats]["applied"] += 1

    for ats, s in sorted(by_ats.items(), key=lambda x: -x[1]["total"]):
        auto = "YES (auto)" if ats in AUTOMATED else "manual"
        print(f"  {ats:<22} {s['total']:>6} {s['scored']:>7} {s['high']:>8} {s['applied']:>8} {auto:>12}")

    # Top candidates
    print(f"\n  TOP {min_score}+ SCORE JOBS (sorted by fit score):")
    print("  " + "-" * 65)
    top = sorted(high, key=lambda j: j.get("fit_score", 0), reverse=True)
    for j in top[:30]:
        ats   = j.get("ats_type", "?")
        score = j.get("fit_score", "?")
        title = j.get("title", "?")[:45]
        co    = j.get("company", "?")[:22]
        print(f"  [{score}/10] {title:<45} @ {co:<22} [{ats}]")

    print("\n" + "=" * 70)
    return high


# ── Step 4: Apply ─────────────────────────────────────────────────────────────

DAILY_LIMIT       = 50   # max applications per day across all portals


def run_apply(min_score: int, limit: int | None, dry_run: bool):
    appliers = {
        "workday":        "workday_auto_apply.py",
        "greenhouse":     "apply_greenhouse.py",
        "eightfold":      "apply_eightfold.py",
        "smartrecruiters":"apply_smartrecruiters.py",
    }

    import profiles, job_store
    from datetime import date
    profiles.set_active_profile(PROFILE)
    jobs = job_store.all_jobs()

    # Count how many we've already applied to today across all portals
    today = str(date.today())
    applied_today = sum(
        1 for j in jobs
        if (j.get("applied_at") or "").startswith(today)
    )
    daily_budget = (limit or DAILY_LIMIT) - applied_today
    if daily_budget <= 0:
        logger.info(f"Daily limit of {limit or DAILY_LIMIT} already reached today ({applied_today} applied). Skipping apply step.")
        return

    logger.info(f"Daily budget: {daily_budget} remaining (target={limit or DAILY_LIMIT}, applied today={applied_today})")

    for ats, script in appliers.items():
        if daily_budget <= 0:
            logger.info(f"Daily limit reached — stopping apply.")
            break

        eligible = [
            j for j in jobs
            if j.get("ats_type") == ats
            and j.get("fit_score", 0) >= min_score
            and not j.get("applied_at")
            and not j.get("removed")
        ]
        # Sort by score descending
        eligible.sort(key=lambda j: j.get("fit_score", 0), reverse=True)

        if not eligible:
            logger.info(f"[{ats}] No eligible jobs (score >= {min_score})")
            continue

        ats_limit = min(daily_budget, limit or DAILY_LIMIT)
        logger.info(f"[{ats}] {len(eligible)} eligible — running {script} (limit={ats_limit})")

        cmd = [PYTHON, script, "--profile", PROFILE, f"--min-score={min_score}",
               f"--limit={ats_limit}", "--min-companies=10"]
        if dry_run:
            cmd += ["--dry-run"]

        subprocess.run(cmd)
        daily_budget -= ats_limit
        time.sleep(2)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Job hunt master pipeline")
    parser.add_argument("--fetch-only",  action="store_true", help="Only fetch + score")
    parser.add_argument("--apply-only",  action="store_true", help="Only apply (skip fetch)")
    parser.add_argument("--status",      action="store_true", help="Show dashboard only")
    parser.add_argument("--min-score",   type=int, default=9,  help="Apply threshold (default 9)")
    parser.add_argument("--dry-run",     action="store_true",  help="No submissions")
    parser.add_argument("--limit",       type=int, default=50, help="Daily apply cap across all portals (default: 50)")
    args = parser.parse_args()

    if args.status:
        show_status(args.min_score)
        return

    if not args.apply_only:
        logger.info("=" * 60)
        logger.info("STEP 1/2: FETCH")
        logger.info("=" * 60)
        run_fetch()

        logger.info("=" * 60)
        logger.info("STEP 2/2: SCORE")
        logger.info("=" * 60)
        run_score()

    if not args.fetch_only:
        high = show_status(args.min_score)
        if not high:
            logger.info("No high-score jobs ready to apply. Done.")
            return

        logger.info("=" * 60)
        logger.info(f"STEP 3: APPLY  (min_score={args.min_score}, dry_run={args.dry_run})")
        logger.info("=" * 60)
        run_apply(args.min_score, args.limit, args.dry_run)

    show_status(args.min_score)
    logger.info("Pipeline complete.")


if __name__ == "__main__":
    main()
