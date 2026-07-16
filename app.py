"""
app.py — Job Hunter Web Dashboard
Run: python app.py
Open: http://localhost:5000
"""

import json
import logging
import threading
import difflib
import re
import time
from datetime import datetime, date
from pathlib import Path
from flask import Flask, render_template, jsonify, request, Response
from bs4 import BeautifulSoup

import job_store
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

app = Flask(__name__)
CONFIG_FILE  = Path(__file__).parent / "config.json"
LAST_FETCH_FILE = Path(__file__).parent / "output" / "last_fetch.json"

_fetch_status = {"running": False, "message": "Idle", "last_run": None}
_tailor_running: set[str] = set()


def _get_last_fetch_date() -> str:
    """Return the date string of the last completed fetch, or ''."""
    try:
        return json.loads(LAST_FETCH_FILE.read_text()).get("date", "")
    except Exception:
        return ""


def _save_last_fetch_date():
    LAST_FETCH_FILE.parent.mkdir(exist_ok=True)
    LAST_FETCH_FILE.write_text(json.dumps({"date": str(date.today())}))


def _load_config():
    return json.loads(CONFIG_FILE.read_text())


# ── Background workers ───────────────────────────────────────────────────────

def _bg_fetch():
    global _fetch_status
    _fetch_status = {"running": True, "message": "Fetching jobs…", "last_run": None}
    try:
        from job_fetcher import fetch_jobs
        config = _load_config()
        jobs = fetch_jobs(config)
        added = job_store.upsert_jobs(jobs)
        _save_last_fetch_date()
        _fetch_status = {"running": False, "message": f"Done — {added} new jobs added", "last_run": str(date.today())}
        logger.info(f"Fetch complete: {added} new jobs")
    except Exception as e:
        logger.exception("Fetch failed")
        _fetch_status = {"running": False, "message": f"Error: {e}", "last_run": None}


def _bg_tailor(job_id: str):
    try:
        job = job_store.get_job(job_id)
        if not job:
            return
        # Get full JD first
        from job_fetcher import fetch_full_jd
        full_desc = fetch_full_jd(job)
        job_with_desc = {**job, "description": full_desc}

        from resume_tailor import tailor_resume
        result = tailor_resume(job_with_desc)

        # Save tailored resume HTML + cover note
        from pdf_generator import save_and_convert
        safe = (job["company"] + "-" + job["title"]).replace("/", "-").replace(" ", "_")[:50]
        job_dir = Path(__file__).parent / "output" / job["fetched_date"] / safe
        pdf_path = save_and_convert(result["resume_html"], job_dir, "resume")
        (job_dir / "cover_note.txt").write_text(result.get("cover_note", ""), encoding="utf-8")

        job_store.update_job(job_id,
            tailor_result=result,
            pdf_path=str(pdf_path),
            description=full_desc,
        )
        logger.info(f"Tailored: {job['title']} @ {job['company']} — score {result.get('match_score')}/10")
    except Exception as e:
        logger.exception(f"Tailor failed for {job_id}")
    finally:
        _tailor_running.discard(job_id)


# ── Routes ───────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    jobs = job_store.all_jobs()
    def sort_key(j):
        tr = j.get("tailor_result") or {}
        return (tr.get("match_score", 0) if tr else -1, j.get("fetched_date", ""))
    jobs.sort(key=sort_key, reverse=True)
    status = {**_fetch_status, "last_run": _get_last_fetch_date()}
    return render_template("index.html", jobs=jobs, status=status, config=_load_config())


@app.route("/fetch", methods=["POST"])
def fetch():
    if _fetch_status["running"]:
        return jsonify({"ok": False, "message": "Already running"})
    t = threading.Thread(target=_bg_fetch, daemon=True)
    t.start()
    return jsonify({"ok": True, "message": "Fetch started"})


@app.route("/fetch-status")
def fetch_status():
    return jsonify(_fetch_status)


@app.route("/tailor/<job_id>", methods=["POST"])
def tailor(job_id):
    if job_id in _tailor_running:
        return jsonify({"ok": False, "message": "Already tailoring"})
    _tailor_running.add(job_id)
    t = threading.Thread(target=_bg_tailor, args=(job_id,), daemon=True)
    t.start()
    return jsonify({"ok": True, "message": "Tailoring started"})


@app.route("/tailor-status/<job_id>")
def tailor_status(job_id):
    running = job_id in _tailor_running
    job = job_store.get_job(job_id)
    done = bool(job and job.get("tailor_result"))
    return jsonify({"running": running, "done": done})


@app.route("/job/<job_id>")
def job_detail(job_id):
    job = job_store.get_job(job_id)
    if not job:
        return "Job not found", 404
    return render_template("job_detail.html", job=job,
                           tailoring=job_id in _tailor_running)


@app.route("/resume/<job_id>")
def resume_html(job_id):
    job = job_store.get_job(job_id)
    if not job or not job.get("tailor_result"):
        return "Resume not tailored yet", 404
    html = job["tailor_result"]["resume_html"]
    return Response(html, mimetype="text/html")


@app.route("/pdf/<job_id>")
def resume_pdf(job_id):
    job = job_store.get_job(job_id)
    if not job or not job.get("pdf_path"):
        return "PDF not generated yet", 404
    pdf = Path(job["pdf_path"])
    if not pdf.exists():
        return "PDF file missing", 404
    return Response(pdf.read_bytes(), mimetype="application/pdf",
                    headers={"Content-Disposition": f"attachment; filename=resume.pdf"})


@app.route("/diff/<job_id>")
def diff_view(job_id):
    job = job_store.get_job(job_id)
    if not job or not job.get("tailor_result"):
        return "Resume not tailored yet", 404

    base_html = (Path(__file__).parent / "base_resume.html").read_text(encoding="utf-8")

    # Extract text sections from both original and tailored HTML
    def extract(html):
        s = BeautifulSoup(html, "html.parser")
        summary_el = s.find(class_="summary-text")
        summary = summary_el.get_text(" ", strip=True) if summary_el else ""
        jobs = []
        for jdiv in s.find_all(class_="job"):
            title_el   = jdiv.find(class_="job-title")
            company_el = jdiv.find(class_="job-company")
            bullets = [li.get_text(" ", strip=True) for li in jdiv.find_all("li")]
            jobs.append({
                "title":   title_el.get_text(strip=True) if title_el else "",
                "company": company_el.get_text(strip=True) if company_el else "",
                "bullets": bullets,
            })
        return summary, jobs

    orig_summary, orig_jobs = extract(base_html)
    tail_summary, tail_jobs = extract(job["tailor_result"]["resume_html"])

    def word_diff(a, b):
        """Produce inline HTML showing added (green) / removed (red) words."""
        aw = re.split(r"(\s+)", a)
        bw = re.split(r"(\s+)", b)
        sm = difflib.SequenceMatcher(None, aw, bw, autojunk=False)
        out = []
        for op, i1, i2, j1, j2 in sm.get_opcodes():
            if op == "equal":
                out.append("".join(bw[j1:j2]))
            elif op == "insert":
                out.append(f'<ins>{"".join(bw[j1:j2])}</ins>')
            elif op == "delete":
                out.append(f'<del>{"".join(aw[i1:i2])}</del>')
            elif op == "replace":
                out.append(f'<del>{"".join(aw[i1:i2])}</del>'
                           f'<ins>{"".join(bw[j1:j2])}</ins>')
        return "".join(out)

    def bullets_diff(orig, tail):
        """Compare two bullet lists — highlight reworded/reordered bullets."""
        result = []
        sm = difflib.SequenceMatcher(None, orig, tail, autojunk=False)
        for op, i1, i2, j1, j2 in sm.get_opcodes():
            if op == "equal":
                for b in tail[j1:j2]:
                    result.append({"type": "equal", "text": b})
            elif op == "replace":
                for ob, tb in zip(orig[i1:i2], tail[j1:j2]):
                    result.append({"type": "changed", "diff": word_diff(ob, tb)})
                # Extra originals (deleted)
                for ob in orig[i1 + (i2-i1):i2]:
                    result.append({"type": "removed", "text": ob})
                # Extra new (added)
                for tb in tail[j1 + (j2-j1):j2]:
                    result.append({"type": "added", "text": tb})
            elif op == "delete":
                for ob in orig[i1:i2]:
                    result.append({"type": "removed", "text": ob})
            elif op == "insert":
                for tb in tail[j1:j2]:
                    result.append({"type": "added", "text": tb})
        return result

    summary_diff = word_diff(orig_summary, tail_summary)

    jobs_diff = []
    for i, (oj, tj) in enumerate(zip(orig_jobs, tail_jobs)):
        jobs_diff.append({
            "title":   oj["title"],
            "company": oj["company"],
            "bullets": bullets_diff(oj["bullets"], tj["bullets"]),
        })

    return render_template("diff.html",
        job=job,
        summary_diff=summary_diff,
        jobs_diff=jobs_diff,
        orig_summary=orig_summary,
        tail_summary=tail_summary,
        key_matches=job["tailor_result"].get("key_matches", []),
        match_score=job["tailor_result"].get("match_score", 0),
        cover_note=job["tailor_result"].get("cover_note", ""),
    )


@app.route("/apply/<job_id>", methods=["POST"])
def mark_applied(job_id):
    data = request.get_json(silent=True) or {}
    applied = data.get("applied", True)
    job_store.mark_applied(job_id, applied)
    return jsonify({"ok": True, "applied": applied})


_gmail_check_running = False

def _bg_check_responses():
    global _gmail_check_running
    try:
        from gmail_checker import check_responses
        applied = job_store.applied_jobs()
        if not applied:
            return
        logger.info(f"Checking Gmail for {len(applied)} applied job(s)…")
        results = check_responses(applied)
        for job_id, responses in results.items():
            job_store.set_responses(job_id, responses)
        logger.info(f"Gmail check done — {len(results)} job(s) with responses")
    except Exception as e:
        logger.exception(f"Gmail check failed: {e}")
    finally:
        _gmail_check_running = False


@app.route("/check-responses", methods=["POST"])
def check_responses():
    global _gmail_check_running
    if _gmail_check_running:
        return jsonify({"ok": False, "message": "Already checking"})
    _gmail_check_running = True
    t = threading.Thread(target=_bg_check_responses, daemon=True)
    t.start()
    return jsonify({"ok": True, "message": "Checking Gmail inbox…"})


@app.route("/check-responses-status")
def check_responses_status():
    return jsonify({"running": _gmail_check_running})


@app.route("/clear", methods=["POST"])
def clear():
    job_store.clear_all()
    return jsonify({"ok": True})


# ── Resume Import & Settings ──────────────────────────────────────────────

BASE_RESUME_PATH = Path(__file__).parent / "base_resume.html"


def _extract_resume_meta(html: str) -> dict:
    """Extract candidate name, email and phone from resume HTML."""
    soup = BeautifulSoup(html, "html.parser")

    # Name — first <h1> or element with class containing 'name'
    name = ""
    h1 = soup.find("h1")
    if h1:
        name = h1.get_text(strip=True)
    if not name:
        el = soup.find(class_=re.compile(r"\bname\b", re.I))
        if el:
            name = el.get_text(strip=True)

    # Email — mailto: href first, then regex scan
    email = ""
    mailto = soup.find("a", href=re.compile(r"^mailto:", re.I))
    if mailto:
        email = mailto["href"].replace("mailto:", "").strip()
    if not email:
        m = re.search(r"[\w.+-]+@[\w-]+\.[a-zA-Z]{2,}", soup.get_text())
        if m:
            email = m.group(0)

    # Phone — common Indian/international patterns
    phone = ""
    m = re.search(r"(\+?[\d\s\-().]{10,16})", soup.get_text())
    if m:
        phone = m.group(1).strip()

    return {"name": name, "email": email, "phone": phone}


@app.route("/upload-resume", methods=["POST"])
def upload_resume():
    """Accept an HTML resume file, save as base_resume.html, update config."""
    f = request.files.get("resume")
    if not f or not f.filename:
        return jsonify({"ok": False, "message": "No file uploaded"}), 400
    if not f.filename.lower().endswith(".html"):
        return jsonify({"ok": False, "message": "Only .html files are supported"}), 400

    html = f.read().decode("utf-8", errors="replace")

    # Validate it has some content
    if len(html.strip()) < 200:
        return jsonify({"ok": False, "message": "File seems too small or empty"}), 400

    # Save as new base resume (keep backup of previous)
    backup = BASE_RESUME_PATH.with_suffix(".html.bak")
    if BASE_RESUME_PATH.exists():
        backup.write_bytes(BASE_RESUME_PATH.read_bytes())

    BASE_RESUME_PATH.write_text(html, encoding="utf-8")

    # Extract metadata from the HTML
    meta = _extract_resume_meta(html)

    # Update config.json candidate section with extracted info
    cfg = _load_config()
    if meta["name"]:
        cfg["candidate"]["name"] = meta["name"]
    if meta["email"]:
        cfg["candidate"]["email"] = meta["email"]
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2))

    logger.info(f"Resume imported: {meta['name']} <{meta['email']}>")
    return jsonify({
        "ok": True,
        "message": "Resume imported successfully",
        "meta": meta,
        "has_structure": bool(BeautifulSoup(html, "html.parser").find(class_="summary-text")),
    })


@app.route("/resume-base")
def resume_base():
    """Show the current base resume HTML."""
    if not BASE_RESUME_PATH.exists():
        return "No base resume found", 404
    return Response(BASE_RESUME_PATH.read_text(encoding="utf-8"), mimetype="text/html")


@app.route("/settings", methods=["GET", "POST"])
def settings():
    if request.method == "POST":
        data = request.get_json(silent=True) or {}
        cfg = _load_config()
        for field in ("name", "email", "total_experience_years"):
            if field in data:
                val = data[field]
                if field == "total_experience_years":
                    try:
                        val = int(val)
                    except (ValueError, TypeError):
                        continue
                cfg["candidate"][field] = val
        CONFIG_FILE.write_text(json.dumps(cfg, indent=2))
        logger.info(f"Settings updated: {cfg['candidate']}")
        return jsonify({"ok": True, "candidate": cfg["candidate"]})
    cfg = _load_config()
    has_resume = BASE_RESUME_PATH.exists()
    return jsonify({"candidate": cfg["candidate"], "has_resume": has_resume})


def _daily_scheduler():
    """Background thread: trigger a fetch every day at 08:00 local time."""
    while True:
        now = datetime.now()
        # Seconds until next 08:00
        target = now.replace(hour=8, minute=0, second=0, microsecond=0)
        if now >= target:
            target = target.replace(day=target.day + 1)
        wait_secs = (target - now).total_seconds()
        logger.info(f"Daily scheduler: next fetch in {wait_secs/3600:.1f} h (at 08:00)")
        time.sleep(wait_secs)
        if not _fetch_status["running"]:
            logger.info("Daily scheduler: triggering morning fetch")
            t = threading.Thread(target=_bg_fetch, daemon=True)
            t.start()


if __name__ == "__main__":
    # Auto-fetch on startup if not already fetched today
    if _get_last_fetch_date() != str(date.today()):
        logger.info("New day detected — auto-fetching jobs on startup")
        t = threading.Thread(target=_bg_fetch, daemon=True)
        t.start()
    else:
        logger.info(f"Already fetched today ({date.today()}) — skipping startup fetch")

    # Start background daily scheduler (fires at 08:00 every morning)
    sched = threading.Thread(target=_daily_scheduler, daemon=True)
    sched.start()

    app.run(debug=False, host="0.0.0.0", port=5000, use_reloader=False)
