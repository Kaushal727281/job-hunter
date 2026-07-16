"""
scheduler.py
Runs main.py every morning at the configured hour (default 8:00 AM).
Usage: python scheduler.py
Keep this running in a terminal or tmux session.
"""

import json
import logging
import sys
import time
from pathlib import Path

import schedule

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

CONFIG_PATH = Path(__file__).parent / "config.json"


def job():
    from main import run
    logger.info("Scheduler triggered — running job hunt...")
    try:
        run(test_mode=False)
    except Exception as e:
        logger.exception(f"Job hunter run failed: {e}")


def main():
    config = json.loads(CONFIG_PATH.read_text())
    send_hour = config.get("email", {}).get("send_hour", 8)
    run_at = f"{send_hour:02d}:00"

    schedule.every().day.at(run_at).do(job)
    logger.info(f"Scheduler started — job hunt will run every day at {run_at}")
    logger.info("Press Ctrl+C to stop.")

    while True:
        schedule.run_pending()
        time.sleep(30)


if __name__ == "__main__":
    main()
