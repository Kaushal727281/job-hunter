"""
main.py — Job Hunter Orchestrator
Run manually: python main.py
Test mode:    python main.py --test   (fetches 3 jobs, skips email send)
"""

import argparse
import json
import logging
import sys
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(Path(__file__).parent / "job_hunter.log"),
    ],
)
logger = logging.getLogger(__name__)

CONFIG_PATH = Path(__file__).parent / "config.json"


def load_config() -> dict:
    return json.loads(CONFIG_PATH.read_text())


def run(test_mode: bool = False):
    config = load_config()
    today = str(date.today())
    output_base = Path(__file__).parent / config["output_dir"] / today

    logger.info(f"=== Job Hunter starting — {today} {'[TEST MODE]' if test_mode else ''} ===")

    # 1. Fetch jobs
    from job_fetcher import fetch_jobs
    limit = 3 if test_mode else None
    jobs = fetch_jobs(config, limit=limit)

    if not jobs:
        logger.info("No new jobs found today.")
        if not test_mode:
            from email_sender import send_digest
            send_digest([], config)
        return

    # 2. Tailor resumes + generate PDFs (only for top_n_with_pdf jobs by default)
    from resume_tailor import tailor_resume
    from pdf_generator import save_and_convert
    from email_sender import send_digest

    email_cfg = config.get("email", {})
    top_n = email_cfg.get("top_n_with_pdf", 5)

    jobs_with_results = []

    for i, job in enumerate(jobs):
        item: dict = {"job": job, "tailor_result": None, "pdf_path": None}

        # Tailor resume for all jobs (needed for match score in email table)
        try:
            result = tailor_resume(job)
            item["tailor_result"] = result
        except Exception as e:
            logger.warning(f"Resume tailoring failed for '{job['title']}': {e}")

        jobs_with_results.append(item)

    # Sort by match score descending
    jobs_with_results.sort(
        key=lambda x: x.get("tailor_result", {}).get("match_score", 0) if x.get("tailor_result") else 0,
        reverse=True,
    )

    # Generate PDFs for top N
    for item in jobs_with_results[:top_n]:
        if not item.get("tailor_result"):
            continue
        job = item["job"]
        safe_company = job["company"].replace("/", "-").replace(" ", "_")[:30]
        safe_title = job["title"].replace("/", "-").replace(" ", "_")[:30]
        folder_name = f"{safe_company}-{safe_title}"
        job_dir = output_base / folder_name

        try:
            pdf_path = save_and_convert(
                html_content=item["tailor_result"]["resume_html"],
                output_dir=job_dir,
                filename_stem="resume",
            )
            item["pdf_path"] = str(pdf_path)

            # Save cover note
            cover_path = job_dir / "cover_note.txt"
            cover_path.write_text(item["tailor_result"].get("cover_note", ""), encoding="utf-8")

            logger.info(f"  Saved: {pdf_path}")
        except Exception as e:
            logger.warning(f"PDF generation failed for '{job['title']}': {e}")

    # 3. Send digest
    if test_mode:
        logger.info("[TEST MODE] Skipping email send. Jobs processed:")
        for item in jobs_with_results:
            j = item["job"]
            score = item.get("tailor_result", {}).get("match_score", "—") if item.get("tailor_result") else "—"
            logger.info(f"  [{score}/10] {j['title']} @ {j['company']} — {j.get('apply_link','')[:60]}")
    else:
        try:
            send_digest(jobs_with_results, config)
        except Exception as e:
            logger.error(f"Email send failed: {e}")

    logger.info(f"=== Done. Output saved to: {output_base} ===")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Job Hunter — daily resume tailoring tool")
    parser.add_argument("--test", action="store_true", help="Test mode: fetch 3 jobs, skip email")
    args = parser.parse_args()
    run(test_mode=args.test)
