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
        return json.loads(LAST_FETCH_FILE.read_text(encoding="utf-8")).get("date", "")
    except Exception:
        return ""


def _save_last_fetch_date():
    LAST_FETCH_FILE.parent.mkdir(exist_ok=True)
    LAST_FETCH_FILE.write_text(json.dumps({"date": str(date.today())}), encoding="utf-8")


def _load_config():
    return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))


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


def _bg_tailor(job_id: str, prev_result: dict = None, prev_pdf: str = None):
    """
    Run tailor in background. prev_result/prev_pdf are the values cleared before
    starting so we can restore them if something goes wrong.
    """
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
            tailor_error=None,
        )
        logger.info(f"Tailored: {job['title']} @ {job['company']} — score {result.get('match_score')}/10")

    except Exception as e:
        logger.exception(f"Tailor failed for {job_id}")

        # Build a human-readable error message
        err_str = str(e)
        if "429" in err_str or "rate_limit" in err_str.lower() or "Rate limit" in err_str:
            import re as _re
            m = _re.search(r"try again in ([\w\s.]+)\.", err_str, _re.I)
            retry = m.group(1).strip() if m else "a few minutes"
            error_msg = f"Groq rate limit reached — please try again in {retry}."
        elif "GROQ_API_KEY" in err_str:
            error_msg = "GROQ_API_KEY is not set. Add it to your .env file."
        elif "JSONDecodeError" in type(e).__name__ or "json" in err_str.lower():
            error_msg = "AI returned an unexpected response. Try again."
        else:
            error_msg = f"Tailoring failed: {err_str[:120]}"

        # Restore previous result so the user isn't left with a blank resume
        restore = {}
        if prev_result:
            restore["tailor_result"] = prev_result
        if prev_pdf:
            restore["pdf_path"] = prev_pdf
        restore["tailor_error"] = error_msg
        job_store.update_job(job_id, **restore)

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
    job = job_store.get_job(job_id)
    if not job:
        return jsonify({"ok": False, "message": "Job not found"})
    _tailor_running.add(job_id)
    # Snapshot existing result so we can restore it if the tailor fails
    prev_result = job.get("tailor_result")
    prev_pdf    = job.get("pdf_path")
    # Clear now so polling returns done=False until fresh output arrives
    job_store.update_job(job_id, tailor_result=None, pdf_path=None, tailor_error=None)
    t = threading.Thread(target=_bg_tailor, args=(job_id, prev_result, prev_pdf), daemon=True)
    t.start()
    return jsonify({"ok": True, "message": "Tailoring started"})


@app.route("/tailor-status/<job_id>")
def tailor_status(job_id):
    running = job_id in _tailor_running
    job = job_store.get_job(job_id)
    done  = bool(job and job.get("tailor_result"))
    error = job.get("tailor_error") if job else None
    return jsonify({"running": running, "done": done, "error": error})


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


_VALID_LAYOUTS = {"classic", "modern", "tech", "executive", "compact"}


def _render_layout(job: dict, layout: str) -> str:
    """Parse the tailored resume HTML and render it with the requested layout template."""
    html = job["tailor_result"]["resume_html"]
    soup = BeautifulSoup(html, "html.parser")

    def _txt(el): return el.get_text(" ", strip=True) if el else ""

    # ── Name & role tagline ──────────────────────────────────────────────
    name = _txt(soup.find("h1"))
    role = _txt(soup.find(class_="role"))

    # ── Contact (from .contact-bar) ──────────────────────────────────────
    email = phone = linkedin = github = ""
    mailto = soup.find("a", href=re.compile(r"^mailto:", re.I))
    if mailto:
        email = mailto["href"].replace("mailto:", "").strip()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "linkedin.com" in href and not linkedin:
            linkedin = href
        elif "github.com" in href and not github:
            github = href
    contact_bar = soup.find(class_="contact-bar")
    if contact_bar:
        m = re.search(r"(\+?[\d][\d\s\-().]{8,}[\d])", contact_bar.get_text())
        if m:
            phone = m.group(1).strip()

    # ── Summary — preserve inner HTML so <strong> keywords render bold ──────
    _sum_el = soup.find(class_="summary-text")
    summary = _sum_el.decode_contents() if _sum_el else ""

    # ── Skill groups (new structure: .skill-group with label + .tag chips) ─
    skill_groups = []
    skills_flat  = []
    for sg in soup.find_all(class_="skill-group"):
        label = _txt(sg.find(class_="skill-group-label"))
        tags  = [_txt(t) for t in sg.find_all(class_="tag") if _txt(t)]
        if tags:
            skill_groups.append({"label": label, "tags": tags})
            skills_flat.extend(tags)

    # Fallback: old .skills-text single string
    if not skills_flat:
        skills_el = soup.find(class_="skills-text")
        if skills_el:
            raw = skills_el.get_text(" ", strip=True).replace("&nbsp;", " ").replace("\u00a0", " ")
            skills_flat = [p.strip() for p in re.split(r"\s*·\s*|\s*,\s*", raw) if p.strip()]

    # ── Jobs (.job → .job-title, .job-company, .duration, ul>li) ──────────
    jobs = []
    for job_div in soup.find_all(class_="job"):
        title_el   = job_div.find(class_="job-title")
        company_el = job_div.find(class_="job-company")
        # date: try .duration first, then .job-date, then .job-meta
        date_el    = (job_div.find(class_="duration") or
                      job_div.find(class_="job-date") or
                      job_div.find(class_="job-meta"))
        bullets    = [li.decode_contents() for li in job_div.find_all("li")]
        jobs.append({
            "title":   _txt(title_el),
            "company": _txt(company_el),
            "date":    _txt(date_el),
            "bullets": bullets,
        })

    # ── Projects (.project) ───────────────────────────────────────────────
    projects = []
    for p_div in soup.find_all(class_="project"):
        pname   = _txt(p_div.find(class_="project-name"))
        prole   = _txt(p_div.find(class_="project-role"))
        stack   = [_txt(t) for t in p_div.find_all(class_="tag") if _txt(t)]
        desc_el = p_div.find("p")
        desc    = _txt(desc_el)
        projects.append({"name": pname, "role": prole, "stack": stack, "description": desc})

    # ── Education (.edu-block, .edu-degree, .edu-school, .edu-year/.edu-date) ─
    education = None
    edu_el = soup.find(class_="edu-block") or soup.find(class_="edu-entry")
    if edu_el:
        education = {
            "degree": _txt(edu_el.find(class_="edu-degree")),
            "school": _txt(edu_el.find(class_="edu-school")),
            "date":   _txt(edu_el.find(class_="edu-year") or edu_el.find(class_="edu-date")),
        }

    # ── Soft skills (spans inside "Soft Skills" section) ──────────────────
    soft_skills = []
    for title_el in soup.find_all(class_="section-title"):
        if "soft" in _txt(title_el).lower():
            tags_el = title_el.find_next_sibling(class_="skill-tags")
            if tags_el:
                soft_skills = [s.get_text(strip=True) for s in tags_el.find_all("span") if s.get_text(strip=True)]
            break

    return render_template(
        f"layouts/{layout}.html",
        name=name, role=role,
        email=email, phone=phone, linkedin=linkedin, github=github,
        summary=summary,
        skill_groups=skill_groups, skills=skills_flat,
        jobs=jobs, projects=projects,
        education=education, soft_skills=soft_skills,
    )


@app.route("/pdf/<job_id>/<layout>")
def resume_pdf_layout(job_id, layout):
    if layout not in _VALID_LAYOUTS:
        return f"Unknown layout '{layout}'. Choose from: {', '.join(sorted(_VALID_LAYOUTS))}", 400
    job = job_store.get_job(job_id)
    if not job or not job.get("tailor_result"):
        return "Resume not tailored yet", 404

    rendered_html = _render_layout(job, layout)

    import tempfile
    from pdf_generator import html_to_pdf
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        html_path = tmp_path / f"resume-{layout}.html"
        pdf_path  = tmp_path / f"resume-{layout}.pdf"
        html_path.write_text(rendered_html, encoding="utf-8")
        html_to_pdf(html_path, pdf_path)
        pdf_bytes = pdf_path.read_bytes()

    return Response(
        pdf_bytes,
        mimetype="application/pdf",
        headers={"Content-Disposition": f"attachment; filename=resume-{layout}.pdf"},
    )


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

_DATE_PAT = re.compile(
    r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*[\s.,]*\d{4}"
    r"|\d{1,2}[/\-]\d{4}"
    r"|\d{4}\s*[-–—]\s*(\d{4}|Present|Current|Till\s*date|Now)",
    re.I,
)
# Matches both spaced ("SUMMARY", "PROFESSIONAL SUMMARY") and
# concatenated ("PROFESSIONALSUMMARY") forms produced after normalisation
_SECTION_PAT = re.compile(
    r"^(SUMMARY|PROFILE|OBJECTIVE|PROFESSIONAL\s*SUMMARY|CAREER\s*SUMMARY|ABOUT\s*ME"
    r"|EXPERIENCE|WORK\s*EXPERIENCE|PROFESSIONAL\s*EXPERIENCE|EMPLOYMENT|WORK\s*HISTORY"
    r"|EDUCATION|ACADEMIC|QUALIFICATIONS|SKILLS|TECHNICAL\s*SKILLS|CORE\s*SKILLS"
    r"|CERTIFICATIONS|PROJECTS|ACHIEVEMENTS|INTERESTS|LANGUAGES)$",
    re.I,
)


def _pdf_to_html(pdf_bytes: bytes) -> str:
    """Convert a PDF resume to HTML compatible with the tailoring engine."""
    from pdfminer.high_level import extract_text
    import io, warnings
    warnings.filterwarnings("ignore")

    raw = extract_text(io.BytesIO(pdf_bytes))

    # Normalise spaced-letter headers: "P R O F I L E" → "PROFILE"
    raw = re.sub(r"\b([A-Z])((?:\s+[A-Z]){2,})\b",
                 lambda m: (m.group(1) + m.group(2)).replace(" ", ""), raw)

    lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]

    # ── Name: first plausible name line (not a section header, date, or URL) ─
    name = ""
    for ln in lines[:12]:
        if _SECTION_PAT.match(ln) or _DATE_PAT.search(ln):
            continue
        if re.search(r"http|www|@|\d{5,}", ln):
            continue
        ln_clean = re.sub(r"\s*[–—-]\s*Resume.*", "", ln, flags=re.I).strip()
        if 4 < len(ln_clean) < 50 and re.match(r"[A-Z][a-zA-Z]", ln_clean):
            name = ln_clean
            break

    # ── Email: scan full text, find cleanest match ────────────────────────
    # TLD must be followed by a non-letter (or end of string) to avoid
    # matching "gmail.comlinkedin" artifacts from multi-column PDF merging.
    full_text = " ".join(lines)
    email = ""
    for m in re.finditer(r"[\w.+-]+@[\w-]+\.[a-zA-Z]{2,6}(?=[^a-zA-Z]|$)", full_text):
        candidate = m.group(0)
        # skip if the local-part is glued to preceding alphabetical text
        start = m.start()
        before = full_text[max(0, start-1):start]
        if before.isalpha():
            continue
        email = candidate
        break

    # ── Split lines into sections ─────────────────────────────────────────
    sections: dict[str, list[str]] = {"_preamble": []}
    current = "_preamble"
    for ln in lines:
        norm = re.sub(r"\s+", "", ln).upper()  # strip all spaces for matching
        # check against section pattern (spaces stripped)
        if _SECTION_PAT.match(norm):
            current = norm
            sections.setdefault(current, [])
        else:
            sections.setdefault(current, []).append(ln)

    def _get_section(*keys) -> list[str]:
        for k in keys:
            k_norm = re.sub(r"\s+", "", k).upper()
            for sk in sections:
                if k_norm in sk:
                    return sections[sk]
        return []

    summary_lines = _get_section("SUMMARY", "PROFILE", "OBJECTIVE", "ABOUT")
    exp_lines     = _get_section("EXPERIENCE", "EMPLOYMENT", "WORK HISTORY", "WORKHISTORY")

    # Fallback: if summary section is empty (common with multi-column PDFs),
    # collect long sentence-like lines from all pre-experience sections
    if not summary_lines:
        exp_key = next((k for k in sections if "EXPERIENCE" in k or "EMPLOYMENT" in k), None)
        exp_section_reached = False
        for k, lns in sections.items():
            if k == exp_key:
                break
            for ln in lns:
                if (len(ln) > 60
                        and not _DATE_PAT.search(ln)
                        and not re.match(r"^[\w]+$", ln)   # skip single-word lines
                        and re.search(r"[a-z]{3,}", ln)):   # has lowercase (sentence-like)
                    summary_lines.append(ln)

    # ── Parse experience into job blocks ──────────────────────────────────
    # Structure in most PDFs: title → company → date → location → bullets...
    job_blocks: list[dict] = []
    cur_job: dict | None   = None
    pending_header: list[str] = []   # lines before a date line

    for ln in exp_lines:
        if _DATE_PAT.search(ln):
            # commit pending header lines as title/company of new job
            if cur_job:
                job_blocks.append(cur_job)
            title   = pending_header[0] if pending_header else ln
            company = pending_header[1] if len(pending_header) > 1 else ""
            cur_job = {
                "title":   title,
                "company": company,
                "date":    _DATE_PAT.search(ln).group(0),
                "bullets": [],
            }
            pending_header = []
        elif cur_job is None:
            # still looking for first date — collect as header candidates
            if ln and not re.search(r"http|www", ln, re.I):
                pending_header.append(ln)
        else:
            # inside a job block
            if re.match(r"^[•\-*▸►→]|^\d+\.", ln):
                bullet = re.sub(r"^[•\-*▸►→\d.]\s*", "", ln).strip()
                cur_job["bullets"].append(bullet)
            elif len(ln) > 30 and not _DATE_PAT.search(ln):
                cur_job["bullets"].append(ln)
            else:
                pending_header = [ln]   # short line = likely start of next job title

    if cur_job:
        job_blocks.append(cur_job)

    # Filter out false-positive job blocks (page numbers, timestamps, short junk)
    job_blocks = [
        jb for jb in job_blocks
        if len(jb["title"]) > 5
        and not _DATE_PAT.match(jb["title"])
        and not re.match(r"^\d+/\d+$", jb["title"])   # page number "1/2"
        and not re.match(r"^\d{2}/\d{2}/\d{4}", jb["title"])  # date "16/07/2026"
        and (jb["bullets"] or jb["company"])  # must have content
    ]

    # ── Phone: reject date-like matches and prefer 10-digit Indian numbers ──
    phone_candidates = re.findall(r"(\+?91[\s\-]?[6-9]\d{9}|[6-9]\d{9}|\+?\d[\d\s\-().]{8,14}\d)", full_text)
    phone = ""
    for pc in phone_candidates:
        pc = pc.strip()
        if re.match(r"\d{4}-\d{2}-\d{2}", pc):
            continue
        digits = re.sub(r"\D", "", pc)
        if len(digits) >= 10:
            phone = pc
            break

    # ── Build HTML ────────────────────────────────────────────────────────
    def esc(s: str) -> str:
        return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    jobs_html = ""
    for jb in job_blocks:
        bullets_html = "".join(f"<li>{esc(b)}</li>" for b in jb["bullets"] if b)
        jobs_html += f"""
        <div class="job">
          <div class="job-title">{esc(jb['title'])}</div>
          <div class="job-company">{esc(jb['company'])}</div>
          <div class="job-date">{esc(jb['date'])}</div>
          <ul>{bullets_html}</ul>
        </div>"""

    summary_html = esc(" ".join(summary_lines)) if summary_lines else ""

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"/>
  <title>{esc(name)} – Resume</title>
  <style>
    body{{font-family:'Segoe UI',Arial,sans-serif;max-width:860px;margin:40px auto;padding:0 32px;color:#1e1e1e}}
    h1{{font-size:28px;font-weight:700;margin-bottom:4px}}
    .contact{{font-size:13px;color:#555;margin-bottom:18px}}
    .section-title{{font-size:13px;font-weight:700;text-transform:uppercase;letter-spacing:1px;
                    color:#0d47a1;border-bottom:1.5px solid #0d47a1;padding-bottom:4px;margin:22px 0 10px}}
    .summary-text{{font-size:14px;line-height:1.7;color:#333}}
    .job{{margin-bottom:18px}}
    .job-title{{font-size:15px;font-weight:700}}
    .job-company{{font-size:13px;color:#555}}
    .job-date{{font-size:12px;color:#888;margin-bottom:6px}}
    ul{{margin:6px 0 0 18px;padding:0}}
    li{{font-size:13px;line-height:1.65;margin-bottom:3px}}
  </style>
</head>
<body>
  <h1>{esc(name)}</h1>
  <div class="contact">
    {f'<a href="mailto:{esc(email)}">{esc(email)}</a>' if email else ''}
    {f' &nbsp;·&nbsp; {esc(phone)}' if phone else ''}
  </div>

  <div class="section-title">Professional Summary</div>
  <div class="summary-text">{summary_html}</div>

  <div class="section-title">Professional Experience</div>
  {jobs_html if jobs_html else '<p style="color:#999;font-size:13px">Experience section could not be parsed — please review the imported resume.</p>'}

</body>
</html>"""

    meta = {"name": name, "email": email, "phone": phone}
    return html, meta


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
    """Accept an HTML or PDF resume file, save as base_resume.html, update config."""
    f = request.files.get("resume")
    if not f or not f.filename:
        return jsonify({"ok": False, "message": "No file uploaded"}), 400

    fname = f.filename.lower()
    raw_bytes = f.read()

    pdf_meta = None
    if fname.endswith(".pdf"):
        try:
            html, pdf_meta = _pdf_to_html(raw_bytes)
        except Exception as e:
            logger.exception("PDF conversion failed")
            return jsonify({"ok": False, "message": f"PDF conversion failed: {e}"}), 400
    elif fname.endswith(".html") or fname.endswith(".htm"):
        html = raw_bytes.decode("utf-8", errors="replace")
    else:
        return jsonify({"ok": False, "message": "Only .html or .pdf files are supported"}), 400

    if len(html.strip()) < 200:
        return jsonify({"ok": False, "message": "File seems too small or empty"}), 400

    # Save as new base resume (keep backup of previous)
    backup = BASE_RESUME_PATH.with_suffix(".html.bak")
    if BASE_RESUME_PATH.exists():
        backup.write_bytes(BASE_RESUME_PATH.read_bytes())

    BASE_RESUME_PATH.write_text(html, encoding="utf-8")

    # Use metadata extracted directly from PDF (avoids re-parsing glued HTML artifacts)
    # For HTML uploads, parse metadata from the HTML structure
    meta = pdf_meta if pdf_meta is not None else _extract_resume_meta(html)

    # Update config.json candidate section with extracted info
    cfg = _load_config()
    if meta["name"]:
        cfg["candidate"]["name"] = meta["name"]
    if meta["email"]:
        cfg["candidate"]["email"] = meta["email"]
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2), encoding="utf-8")

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
        CONFIG_FILE.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
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
