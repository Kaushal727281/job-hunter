"""
app.py — Job Hunter Web Dashboard
Run: python app.py
Open: http://localhost:5000
"""

import json
import logging
import os
import threading
import difflib
import re
import time
from datetime import datetime, date
from pathlib import Path
from flask import Flask, render_template, jsonify, request, Response, session, redirect, url_for
from bs4 import BeautifulSoup

import job_store
import profiles
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

app = Flask(__name__)

# Stable across restarts so profile-picker sessions survive a server restart —
# generated once on first run, not regenerated (which would log everyone out).
_SECRET_KEY_FILE = Path(__file__).parent / ".flask_secret_key"
if not _SECRET_KEY_FILE.exists():
    _SECRET_KEY_FILE.write_text(os.urandom(32).hex(), encoding="utf-8")
app.secret_key = _SECRET_KEY_FILE.read_text(encoding="utf-8").strip()


@app.template_filter("expmin")
def _expmin_filter(exp: str) -> str:
    """Extract the leading number from an experience string (e.g. '5+ Yrs' or '5–8 Yrs' -> '5')."""
    m = re.search(r"\d+", exp or "")
    return m.group() if m else ""

# Keyed by profile name — each person's fetch progress is independent, so
# one profile's fetch doesn't show up as another's status. Mutated in place
# (never reassigned wholesale) since _fs() hands back a live reference.
_fetch_status: dict[str, dict] = {}
_career_fetch_status: dict[str, dict] = {}
_tailor_running: set[str] = set()


def _fs(profile: str = None) -> dict:
    profile = profile or profiles.get_active_profile()
    return _fetch_status.setdefault(profile, {"running": False, "message": "Idle", "last_run": None})


def _cfs(profile: str = None) -> dict:
    """Return (and lazily create) the career-fetch status dict for a profile."""
    profile = profile or profiles.get_active_profile()
    return _career_fetch_status.setdefault(profile, {"running": False, "message": "Idle"})


def _get_last_fetch_date() -> str:
    """Return the date string of the last completed fetch, or ''."""
    try:
        path = profiles.output_dir() / "last_fetch.json"
        return json.loads(path.read_text(encoding="utf-8")).get("date", "")
    except Exception:
        return ""


def _save_last_fetch_date():
    path = profiles.output_dir() / "last_fetch.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"date": str(date.today())}), encoding="utf-8")


def _load_config():
    return json.loads(profiles.config_path().read_text(encoding="utf-8"))


# ── Background workers ───────────────────────────────────────────────────────
# Each of these runs in a spawned thread, which doesn't inherit Flask's
# session — the caller captures profiles.get_active_profile() before
# spawning and passes it in; the first line here sets it for this thread.

def _bg_score(job_ids: list[str], profile: str):
    """Score newly fetched jobs with Ollama in the background."""
    profiles.set_active_profile(profile)
    def _status(msg):
        _fs(profile)["message"] = msg
    try:
        from job_scorer import score_jobs
        score_jobs(job_ids, status_cb=_status)
        _fs(profile)["message"] = f"Done — {len(job_ids)} new jobs scored"
    except Exception as e:
        logger.warning(f"Fit-scoring failed: {e}")
        _fs(profile)["message"] = f"Done — scoring skipped ({e})"


def _bg_fetch(profile: str):
    profiles.set_active_profile(profile)
    st = _fs(profile)
    st.update(running=True, message="Fetching jobs…", last_run=None)
    try:
        from job_fetcher import fetch_jobs
        config = _load_config()
        jobs = fetch_jobs(config)
        new_ids = job_store.upsert_jobs_return_ids(jobs)
        added = len(new_ids)
        _save_last_fetch_date()
        st.update(running=False, message=f"Done — {added} new jobs added", last_run=str(date.today()))
        logger.info(f"[{profile}] Fetch complete: {added} new jobs")
        if new_ids:
            t = threading.Thread(target=_bg_score, args=(new_ids, profile), daemon=True)
            t.start()
    except Exception as e:
        logger.exception(f"[{profile}] Fetch failed")
        st.update(running=False, message=f"Error: {e}", last_run=None)


def _bg_fetch_careers(profile: str, tier_filter, type_filter, role: str = None, location: str = None):
    """Background worker: scrape company career pages and upsert results."""
    profiles.set_active_profile(profile)
    st = _cfs(profile)
    tier_label = f"T{'/'.join(str(t) for t in tier_filter)}" if tier_filter else "All"
    type_label = type_filter or "all"
    role_label = role or "(from profile)"
    loc_label = location or "(from profile)"
    st.update(running=True, message=f"Scraping {tier_label} {type_label} — role: {role_label}, loc: {loc_label}…")

    # If the user provided role/location via the modal, persist them in config for next time
    if role or location:
        try:
            cfg = _load_config()
            js = cfg.setdefault("job_search", {})
            if role:
                js["target_role"] = role.strip()
            if location:
                locs = js.setdefault("locations", [])
                loc = location.strip()
                if loc not in locs:
                    locs.insert(0, loc)
                elif locs[0] != loc:
                    locs.remove(loc)
                    locs.insert(0, loc)
            profiles.config_path().write_text(json.dumps(cfg, indent=2), encoding="utf-8")
        except Exception as _e:
            logger.warning(f"Could not persist career preferences: {_e}")

    try:
        from job_fetcher import fetch_career_sites
        config = _load_config()
        jobs = fetch_career_sites(config, tier_filter=tier_filter, type_filter=type_filter,
                                  role=role, location=location)
        # career_scraper uses "job_id" key; job_store expects "id"
        # also backfill apply_link, fetched_date, and decode HTML entities in description
        from html import unescape
        from bs4 import BeautifulSoup as _BS
        today = str(date.today())
        for j in jobs:
            if "job_id" in j and "id" not in j:
                j["id"] = j.pop("job_id")
            if j.get("url") and not j.get("apply_link"):
                j["apply_link"] = j["url"]
            if not j.get("fetched_date"):
                j["fetched_date"] = today
            if j.get("description"):
                j["description"] = _BS(unescape(j["description"]), "html.parser").get_text(" ").strip()
        new_ids = job_store.upsert_jobs_return_ids(jobs)
        added = len(new_ids)
        _save_last_fetch_date()
        st.update(running=False, message=f"Done — {added} new jobs from career sites")
        logger.info(f"[{profile}] Career-site fetch complete: {added} new jobs")
        if new_ids:
            t = threading.Thread(target=_bg_score, args=(new_ids, profile), daemon=True)
            t.start()
    except Exception as e:
        logger.exception(f"[{profile}] Career-site fetch failed")
        st.update(running=False, message=f"Error: {e}")


def _suggest_layout(job: dict) -> tuple[str, str]:
    """
    Return (layout_name, reason) — the best PDF layout for this job/company.
    Rule-based: no extra LLM call needed.
    """
    company   = (job.get("company") or "").lower()
    title     = (job.get("title")   or "").lower()
    desc      = (job.get("description") or "").lower()
    cotype    = (job.get("company_type") or "").lower()
    combined  = company + " " + title + " " + desc[:500]

    _BANKS    = ("jpmorgan","goldman","barclays","bnp","hsbc","morgan stanley","wells fargo",
                 "citibank","deutsche bank","bank of america","credit suisse","ubs","nomura",
                 "macquarie","rbc","td bank","visa","mastercard","american express","fidelity")
    _FAANG    = ("google","amazon","meta","apple","microsoft","netflix","uber","airbnb","stripe",
                 "shopify","atlassian","salesforce","twilio","datadog","snowflake","mongodb",
                 "confluent","elastic","hashicorp","gitlab","github","dropbox","figma","notion")
    _STARTUPS = ("startup","series a","series b","seed","y combinator","ycombinator","techstars")

    if any(k in combined for k in _BANKS):
        return ("classic", "Finance/banking roles — ATS-safe Classic B&W preferred by bank HR systems")
    if any(k in combined for k in _FAANG):
        return ("tech", "FAANG/tech product company — minimal Tech layout preferred by engineering teams")
    if any(k in combined for k in _STARTUPS):
        return ("modern", "Startup — Modern Sidebar stands out with visual hierarchy")
    if cotype == "service":
        return ("compact", "Consulting/service firm — Compact 2-column fits more detail per page")
    # Default for product companies
    return ("modern", "Product company — Modern Sidebar balances visual appeal with structure")


def _verify_tips(tips: list[str], resume_html: str) -> list[str]:
    """
    Auto-verify each improvement tip against the actual tailored resume HTML.
    If the key tech/action terms from the tip are found in the resume text,
    mark it as ✓ Implemented regardless of what the LLM self-reported.
    Tips already starting with ✓ are left unchanged.
    """
    import re as _re
    from bs4 import BeautifulSoup as _BS
    resume_text = _BS(resume_html, "html.parser").get_text(" ").lower()

    verified = []
    for tip in tips:
        if tip.startswith("✓"):
            verified.append(tip)
            continue

        # Extract quoted phrases first, then CamelCase/ALL-CAPS tokens, then slash-separated terms
        candidates = _re.findall(r'"([^"]+)"', tip)
        candidates += _re.findall(r'[A-Z][a-zA-Z0-9]*(?:[A-Z][a-zA-Z0-9]+)+', tip)   # CamelCase
        candidates += _re.findall(r'[A-Z]{2,}(?:/[A-Z]{2,})+', tip)                   # AWS/GCP/Azure style
        candidates += _re.findall(r'\b[A-Z]{2,}\b', tip)                               # acronyms

        # Also extract key lowercase phrases after "with", "in", "for", "and"
        phrase_matches = _re.findall(r'(?:with|in|for|and)\s+([\w\s\-/]{3,30}?)(?:,|\.|\s+and|\s+or|$)', tip, _re.I)
        candidates += [p.strip() for p in phrase_matches if len(p.strip()) > 3]

        # Deduplicate, filter noise words
        _STOP = {"the", "and", "for", "with", "that", "this", "your", "you", "are", "has",
                 "have", "been", "more", "than", "from", "its", "can", "such", "any"}
        seen_terms = []
        for c in candidates:
            cl = c.strip().lower()
            if cl and cl not in _STOP and len(cl) > 2:
                seen_terms.append(cl)

        if not seen_terms:
            verified.append(tip)
            continue

        # A tip is considered addressed if ANY key term appears in the resume
        found = [t for t in seen_terms if t in resume_text]
        if found:
            verified.append(f"✓ Implemented: {tip}")
            logger.debug(f"  Tip auto-verified (found: {found[:3]}): {tip[:60]}")
        else:
            verified.append(tip)

    return verified


def _bg_tailor(job_id: str, profile: str, prev_result: dict = None, prev_pdf: str = None, custom_instructions: str = None):
    """
    Run tailor in background. Retries up to 3× until match_score >= 8.
    prev_result/prev_pdf are the values cleared before starting so we can restore if something goes wrong.
    """
    profiles.set_active_profile(profile)
    MAX_ATTEMPTS = 3
    TARGET_SCORE = 8

    try:
        job = job_store.get_job(job_id)
        if not job:
            return
        # Get full JD once
        from job_fetcher import fetch_full_jd, extract_experience
        full_desc = fetch_full_jd(job)
        job_with_desc = {**job, "description": full_desc}

        # Most sources (LinkedIn, Indeed, Glassdoor, RemoteOK, WWR, HNJobs) only
        # get their full JD text here, lazily — backfill experience from it now.
        exp_update = {}
        if not job.get("experience"):
            extracted_exp = extract_experience(full_desc)
            if extracted_exp:
                exp_update["experience"] = extracted_exp

        from resume_tailor import tailor_resume
        result = None
        # Seed tips from the previous tailor run so the first re-tailor attempt
        # already addresses the known gaps rather than discovering them again.
        prev_tips = (prev_result or {}).get("improvement_tips") or None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            if attempt > 1:
                score_prev = result.get("match_score", 0) or 0
                prev_tips  = result.get("improvement_tips") or []
                logger.info(f"  Retry {attempt}/{MAX_ATTEMPTS} — score was {score_prev}/10, passing {len(prev_tips)} tips")
                job_store.update_job(job_id, tailor_error=f"Score {score_prev}/10 — retrying (attempt {attempt}/{MAX_ATTEMPTS})…")
            result = tailor_resume(job_with_desc, prev_tips=prev_tips, custom_instructions=custom_instructions)
            score = result.get("match_score", 0) or 0
            logger.info(f"  Attempt {attempt}: match_score={score}/10")
            if score >= TARGET_SCORE:
                break

        # Auto-verify improvement tips against the actual tailored resume HTML
        if result.get("improvement_tips") and result.get("resume_html"):
            result["improvement_tips"] = _verify_tips(
                result["improvement_tips"], result["resume_html"]
            )

        # Attach layout suggestion
        layout, layout_reason = _suggest_layout(job)
        result["layout_suggestion"] = layout
        result["layout_reason"]     = layout_reason

        # Save tailored resume HTML + cover note
        from pdf_generator import save_and_convert
        safe = (job["company"] + "-" + job["title"]).replace("/", "-").replace(" ", "_")[:50]
        job_dir = profiles.output_dir() / (job.get("fetched_date") or job.get("date_posted", str(date.today()))[:10]) / safe
        pdf_path = save_and_convert(result["resume_html"], job_dir, "resume")
        (job_dir / "cover_note.txt").write_text(result.get("cover_note", ""), encoding="utf-8")

        job_store.update_job(job_id,
            tailor_result=result,
            pdf_path=str(pdf_path),
            description=full_desc,
            tailor_error=None,
            **exp_update,
        )
        logger.info(f"Tailored: {job['title']} @ {job['company']} — score {result.get('match_score')}/10 | layout: {layout}")

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

@app.before_request
def _select_profile():
    """Every request must have an active profile before hitting a view —
    redirect to the picker if none is chosen yet (or none exist at all).
    No password: this is a shared family computer, not internet-facing."""
    if request.endpoint in ("profiles_page", "static") or request.path.startswith("/static"):
        return
    available = profiles.list_profiles()
    if not available:
        return redirect(url_for("profiles_page"))
    name = session.get("profile")
    if not name or name not in available:
        return redirect(url_for("profiles_page"))
    profiles.set_active_profile(name)


@app.route("/profiles", methods=["GET", "POST"])
def profiles_page():
    if request.method == "POST":
        new_name = request.form.get("new_name", "").strip()
        if new_name:
            slug = profiles.create_profile(new_name)
            session["profile"] = slug
            return redirect(url_for("index"))
        chosen = request.form.get("profile", "").strip()
        if chosen in profiles.list_profiles():
            session["profile"] = chosen
            return redirect(url_for("index"))
    return render_template("profiles.html", available=profiles.list_profiles())


@app.route("/")
def index():
    jobs = job_store.all_jobs()
    def sort_key(j):
        tr = j.get("tailor_result") or {}
        return (j.get("fetched_date", ""), tr.get("match_score", 0) if tr else -1)
    jobs.sort(key=sort_key, reverse=True)
    status = {**_fs(), "last_run": _get_last_fetch_date()}
    career_status = _cfs()
    has_resume = profiles.base_resume_path().exists()
    return render_template("index.html", jobs=jobs, status=status, career_status=career_status, config=_load_config(), has_resume=has_resume)


@app.route("/fetch", methods=["POST"])
def fetch():
    if _fs()["running"]:
        return jsonify({"ok": False, "message": "Already running"})
    t = threading.Thread(target=_bg_fetch, args=(profiles.get_active_profile(),), daemon=True)
    t.start()
    return jsonify({"ok": True, "message": "Fetch started"})


@app.route("/fetch-status")
def fetch_status():
    return jsonify(_fs())


@app.route("/fetch-careers", methods=["POST"])
def fetch_careers():
    if _cfs()["running"]:
        return jsonify({"ok": False, "message": "Career fetch already running"})
    data = request.get_json(silent=True) or {}
    tier_filter = data.get("tier_filter")   # list[int] or None
    type_filter = data.get("type_filter")   # str or None
    role     = (data.get("role") or "").strip() or None
    location = (data.get("location") or "").strip() or None

    # Normalise tier_filter
    if isinstance(tier_filter, list):
        tier_filter = [int(t) for t in tier_filter]

    # If neither role nor location was supplied, check the profile config.
    # If the profile also has nothing set, ask the UI to show the preferences modal.
    if not role and not location:
        cfg = _load_config()
        js  = cfg.get("job_search", {})
        has_role = bool(js.get("target_role") or js.get("queries"))
        has_loc  = bool(js.get("locations"))
        if not has_role or not has_loc:
            return jsonify({
                "ok": False,
                "needs_prefs": True,
                "current_role": js.get("target_role") or (js.get("queries") or [""])[0],
                "current_location": (js.get("locations") or [""])[0],
                "message": "Please set your target role and preferred location first.",
            })

    profile = profiles.get_active_profile()
    t = threading.Thread(
        target=_bg_fetch_careers,
        args=(profile, tier_filter, type_filter, role, location),
        daemon=True,
    )
    t.start()
    return jsonify({"ok": True, "message": "Career-site scrape started"})


@app.route("/fetch-careers-status")
def fetch_careers_status():
    return jsonify(_cfs())


@app.route("/tailor/<job_id>", methods=["POST"])
def tailor(job_id):
    if job_id in _tailor_running:
        return jsonify({"ok": False, "message": "Already tailoring"})
    job = job_store.get_job(job_id)
    if not job:
        return jsonify({"ok": False, "message": "Job not found"})
    _tailor_running.add(job_id)
    # Read optional custom instructions from request body
    data = request.get_json(silent=True) or {}
    custom_instructions = (data.get("custom_instructions") or "").strip() or None
    # Snapshot existing result so we can restore it if the tailor fails
    prev_result = job.get("tailor_result")
    prev_pdf    = job.get("pdf_path")
    # Clear now so polling returns done=False until fresh output arrives
    job_store.update_job(job_id, tailor_result=None, pdf_path=None, tailor_error=None)
    t = threading.Thread(target=_bg_tailor, args=(job_id, profiles.get_active_profile(), prev_result, prev_pdf, custom_instructions), daemon=True)
    t.start()
    return jsonify({"ok": True, "message": "Tailoring started"})


@app.route("/tailor-status/<job_id>")
def tailor_status(job_id):
    running = job_id in _tailor_running
    job = job_store.get_job(job_id)
    done  = bool(job and job.get("tailor_result"))
    error = job.get("tailor_error") if job else None
    # Surface retry message during polling (tailor_error is set to retry notice mid-run)
    msg = error if (running and error and "retrying" in (error or "")) else None
    return jsonify({"running": running, "done": done, "error": None if done else error, "message": msg})


@app.route("/score/<job_id>", methods=["POST"])
def score_job(job_id):
    """Score a single job on demand."""
    job = job_store.get_job(job_id)
    if not job:
        return jsonify({"ok": False, "message": "Job not found"})
    try:
        from job_scorer import score_job as _score_job, _candidate_profile
        score, reason = _score_job(job, _candidate_profile())
        job_store.update_job(job_id, fit_score=score, fit_reason=reason)
        return jsonify({"ok": True, "fit_score": score, "fit_reason": reason})
    except Exception as e:
        return jsonify({"ok": False, "message": str(e)})


@app.route("/job/<job_id>")
def job_detail(job_id):
    job = job_store.get_job(job_id)
    if not job:
        return "Job not found", 404
    from ats_advisor import get_company_ats_profile, run_ats_postmortem
    ats_profile = get_company_ats_profile(job)
    tr = job.get("tailor_result")
    ats_report = run_ats_postmortem(job, tr["resume_html"]) if tr and tr.get("resume_html") else None
    return render_template("job_detail.html", job=job,
                           tailoring=job_id in _tailor_running,
                           ats_profile=ats_profile,
                           ats_report=ats_report)


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
                    headers={"Content-Disposition": f"attachment; filename={_pdf_filename(job)}"})


_VALID_LAYOUTS = {"classic", "modern", "tech", "executive", "compact"}


def _pdf_filename(job: dict, suffix: str = "") -> str:
    """Build a clean PDF filename: CandidateName_JobTitle[_suffix].pdf"""
    try:
        cfg = _load_config()
        name = cfg.get("candidate", {}).get("name", "Resume")
    except Exception:
        name = "Resume"
    title = job.get("title", "")
    # Combine name + title, replace spaces/special chars with underscores
    raw = f"{name}_{title}{'_' + suffix if suffix else ''}"
    clean = re.sub(r"[^A-Za-z0-9]+", "_", raw).strip("_")
    return f"{clean}.pdf"


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

    def _dedupe_bullets(bullets: list) -> list:
        """Remove near-duplicate bullets that share the same opening words (LLM artifact)."""
        def _plain(html):
            return re.sub(r'\s+', ' ', BeautifulSoup(html, "html.parser").get_text(' ', strip=True)).lower()
        def _words(text):
            return re.findall(r'\b\w+\b', text)
        kept, plains = [], []
        for b in bullets:
            p = _plain(b)
            pw = _words(p)
            dup = False
            for i, kp in enumerate(plains):
                kw = _words(kp)
                # Same bullet if first 6 meaningful words match
                if len(pw) >= 6 and len(kw) >= 6 and pw[:6] == kw[:6]:
                    if len(p) > len(kp):   # keep the more detailed version
                        kept[i] = b
                        plains[i] = p
                    dup = True
                    break
            if not dup:
                kept.append(b)
                plains.append(p)
        return kept

    # ── Jobs (.job → .job-title, .job-company, .duration, ul>li) ──────────
    jobs = []
    for job_div in soup.find_all(class_="job"):
        title_el   = job_div.find(class_="job-title")
        company_el = job_div.find(class_="job-company")
        # date: try .duration first, then .job-date, then .job-meta
        date_el    = (job_div.find(class_="duration") or
                      job_div.find(class_="job-date") or
                      job_div.find(class_="job-meta"))
        raw_bullets = [li.decode_contents() for li in job_div.find_all("li")][:8]
        bullets = _dedupe_bullets(raw_bullets)[:6]
        jobs.append({
            "title":   _txt(title_el),
            "company": _txt(company_el),
            "date":    _txt(date_el),
            "bullets": bullets,
        })

    # ── Projects (.project) ───────────────────────────────────────────────
    # Skip the FICO Authoring Module project — it was removed from Key Projects
    # in all layouts to keep the resume to one page.
    _SKIP_PROJECTS = {"fico decision management platform"}
    projects = []
    for p_div in soup.find_all(class_="project"):
        pname   = _txt(p_div.find(class_="project-name"))
        prole   = _txt(p_div.find(class_="project-role"))
        stack   = [_txt(t) for t in p_div.find_all(class_="tag") if _txt(t)]
        desc_el = p_div.find("p")
        desc    = _txt(desc_el)
        if any(s in pname.lower() for s in _SKIP_PROJECTS):
            continue
        # Truncate long descriptions to keep layout to one page
        if desc and len(desc) > 220:
            desc = desc[:220].rsplit(" ", 1)[0] + "…"
        projects.append({"name": pname, "role": prole, "stack": stack, "description": desc})

    # ── Education (.edu-block, .edu-degree, .edu-school, .edu-year/.edu-date) ─
    education = None
    edu_el = soup.find(class_="edu-block") or soup.find(class_="edu-entry")
    if edu_el:
        edu_date = _txt(edu_el.find(class_="edu-year") or edu_el.find(class_="edu-date"))
        # Correct LLM-hallucinated graduation years — canonical date is 2016–2020
        if edu_date and "2020" not in edu_date:
            edu_date = "2016 – 2020"
        education = {
            "degree": _txt(edu_el.find(class_="edu-degree")),
            "school": _txt(edu_el.find(class_="edu-school")),
            "date":   edu_date,
        }

    # ── Soft skills (spans inside "Soft Skills" section) ──────────────────
    soft_skills = []
    for title_el in soup.find_all(class_="section-title"):
        if "soft" in _txt(title_el).lower():
            tags_el = title_el.find_next_sibling(class_="skill-tags")
            if tags_el:
                soft_skills = [s.get_text(strip=True) for s in tags_el.find_all("span") if s.get_text(strip=True)]
            break

    # ── Certifications ─────────────────────────────────────────────────────
    certifications = []
    for title_el in soup.find_all(class_="section-title"):
        if "cert" in _txt(title_el).lower():
            sib = title_el.find_next_sibling()
            while sib and "section-title" not in (sib.get("class") or []):
                for item in sib.find_all(class_="cert-item"):
                    t = _txt(item)
                    if t:
                        certifications.append(t)
                sib = sib.find_next_sibling()
            break

    return render_template(
        f"layouts/{layout}.html",
        name=name, role=role,
        email=email, phone=phone, linkedin=linkedin, github=github,
        summary=summary,
        skill_groups=skill_groups, skills=skills_flat,
        jobs=jobs, projects=projects,
        education=education, soft_skills=soft_skills,
        certifications=certifications,
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
        headers={"Content-Disposition": f"attachment; filename={_pdf_filename(job, layout)}"},
    )


@app.route("/diff/<job_id>")
def diff_view(job_id):
    job = job_store.get_job(job_id)
    if not job or not job.get("tailor_result"):
        return "Resume not tailored yet", 404

    base_html = profiles.base_resume_path().read_text(encoding="utf-8")

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


def _get_cover_letter(tr: dict) -> str:
    """Extract cover letter body — greeting and sign-off stripped (template owns those)."""
    import re as _re
    raw = tr.get("cover_letter") or tr.get("cover_note", "")
    if isinstance(raw, list):
        raw = "\n\n".join(raw)
    if not raw:
        return ""
    clean = []
    for ln in raw.splitlines():
        s = ln.strip()
        if _re.match(r"dear\s+hiring\s+manager", s, _re.I):
            continue
        if _re.match(r"(sincerely|regards|best\s+regards|warm\s+regards)[,.]?\s*$", s, _re.I):
            continue
        clean.append(ln)
    # Also drop trailing blank lines followed by a lone name line
    text = "\n".join(clean).strip()
    # Remove trailing "Kaushal Kumar Jha" style sign-off if it ends the letter
    config = _load_config()
    name = config.get("candidate", {}).get("name", "")
    if name:
        text = _re.sub(r"\n+" + _re.escape(name) + r"\s*$", "", text, flags=_re.I).strip()
    return text


@app.route("/cover/<job_id>")
def cover_letter_html(job_id):
    job = job_store.get_job(job_id)
    if not job or not job.get("tailor_result"):
        return "Resume not tailored yet — tailor first to generate a cover letter.", 404
    tr = job["tailor_result"]
    letter = _get_cover_letter(tr)
    if not letter:
        return "No cover letter found. Re-tailor this job to generate one.", 404
    config = _load_config()
    candidate = config.get("candidate", {})
    return render_template("cover_letter.html",
        job=job,
        letter=letter,
        candidate_name=candidate.get("name", ""),
        candidate_email=candidate.get("email", ""),
    )


@app.route("/cover/<job_id>/pdf")
def cover_letter_pdf(job_id):
    job = job_store.get_job(job_id)
    if not job or not job.get("tailor_result"):
        return "Resume not tailored yet", 404
    tr = job["tailor_result"]
    letter = _get_cover_letter(tr)
    if not letter:
        return "No cover letter found. Re-tailor this job to generate one.", 404
    config = _load_config()
    candidate = config.get("candidate", {})
    rendered_html = render_template("cover_letter.html",
        job=job,
        letter=letter,
        candidate_name=candidate.get("name", ""),
        candidate_email=candidate.get("email", ""),
    )
    import tempfile
    from pdf_generator import html_to_pdf
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        html_path = tmp_path / "cover_letter.html"
        pdf_path  = tmp_path / "cover_letter.pdf"
        html_path.write_text(rendered_html, encoding="utf-8")
        html_to_pdf(html_path, pdf_path)
        pdf_bytes = pdf_path.read_bytes()
    safe = (job["company"] + "-" + job["title"]).replace("/", "-").replace(" ", "_")[:40]
    return Response(
        pdf_bytes,
        mimetype="application/pdf",
        headers={"Content-Disposition": f"attachment; filename={_pdf_filename(job, 'CoverLetter')}"},
    )


@app.route("/outreach/<job_id>")
def outreach(job_id):
    """Return outreach materials for a tailored job: LinkedIn InMail, cold email, HR email guesses."""
    job = job_store.get_job(job_id)
    if not job or not job.get("tailor_result"):
        return jsonify({"error": "Job not tailored yet"}), 400

    tr             = job["tailor_result"]
    company        = job.get("company", "Unknown Company")
    title          = job.get("title", "Software Engineer")
    key_matches    = tr.get("key_matches", [])
    cover_letter   = (tr.get("cover_letter") or "").strip()
    match_score    = tr.get("match_score", 0)

    cfg            = _load_config()
    candidate      = cfg.get("candidate", {})
    cand_name      = candidate.get("name", "")
    cand_email     = candidate.get("email", "")
    exp_years      = candidate.get("total_experience_years", 0)

    # ── Resolve company domain ───────────────────────────────────────────────
    _job_boards = {
        "linkedin.com", "glassdoor.com", "naukri.com", "shine.com",
        "indeed.com", "remoteok.com", "workatastartup.com", "instahyre.com",
        "hirist.com", "foundit.in", "monster.com", "wellfound.com",
        "myworkday.com", "greenhouse.io", "lever.co", "workable.com",
        "smartrecruiters.com", "icims.com", "taleo.net", "successfactors.com",
    }
    domain = ""
    apply_link = job.get("apply_link", "")
    apply_link_is_company_site = False
    try:
        from urllib.parse import urlparse
        netloc = urlparse(apply_link).netloc.lower()
        for prefix in ("www.", "in.", "jobs.", "careers."):
            netloc = netloc.removeprefix(prefix)
        if netloc and not any(jb in netloc for jb in _job_boards):
            domain = netloc
            apply_link_is_company_site = True
    except Exception:
        pass
    # Fallback: slug company name → domain guess
    if not domain and company:
        slug = re.sub(r"[^a-z0-9]", "", company.lower().split()[0])
        domain = f"{slug}.com" if slug else ""

    # ── Job URL for sharing with referral contacts ───────────────────────────
    # Prefer the company's own site over a LinkedIn/Indeed/etc. listing — those
    # require login and expire, which makes them a poor thing to hand a referrer.
    if apply_link_is_company_site:
        job_url = apply_link
    elif domain:
        from urllib.parse import quote as _qj
        job_url = f"https://www.google.com/search?q={_qj(f'site:{domain} {title}')}"
    else:
        job_url = ""

    # ── Hunter.io domain search (real verified emails) ──────────────────────
    hunter_key = os.getenv("HUNTER_API_KEY", "")
    hr_emails: list[str] = []

    if hunter_key and domain:
        try:
            import urllib.request as _ur
            hunter_url = (
                f"https://api.hunter.io/v2/domain-search"
                f"?domain={domain}&type=personal&limit=10&api_key={hunter_key}"
            )
            with _ur.urlopen(hunter_url, timeout=6) as resp:
                hunter_data = json.loads(resp.read().decode())
            emails_found = hunter_data.get("data", {}).get("emails", [])
            # Prefer HR / talent / recruiting / careers roles
            _HR_ROLES = {"hr", "human resources", "talent", "recruit", "hiring",
                         "career", "people", "workforce", "staffing"}
            hr_hits   = [e["value"] for e in emails_found
                         if any(r in (e.get("department") or "").lower() or
                                r in (e.get("position") or "").lower() or
                                r in e["value"].split("@")[0].lower()
                                for r in _HR_ROLES)]
            other     = [e["value"] for e in emails_found if e["value"] not in hr_hits]
            hr_emails = (hr_hits + other)[:6]
            if hr_emails:
                logger.info(f"  Hunter.io found {len(hr_emails)} email(s) for {domain}")
        except Exception as e:
            logger.debug(f"  Hunter.io lookup failed ({e}) — falling back to guesses")

    # Fallback: common HR address patterns (clearly labelled as guesses in UI)
    if not hr_emails and domain:
        hr_emails = [f"careers@{domain}", f"hr@{domain}", f"talent@{domain}",
                     f"recruiting@{domain}", f"jobs@{domain}"]

    # ── Referral contacts from your LinkedIn Connections export ─────────────
    from linkedin_contacts import find_connections_at_company
    referral_contacts = find_connections_at_company(company)

    # ── Decision-maker search links (LinkedIn people search, no scraping) ───
    # These just prefill LinkedIn's own search — you browse and message manually
    # from your logged-in session, so there's no scraping/ban risk involved.
    from urllib.parse import quote as _qd
    skill_hint = key_matches[0] if key_matches else ""
    _DECISION_TITLES = [
        "Engineering Manager", "Director of Engineering", "VP Engineering",
        "Head of Engineering", "Technical Lead", "CTO",
    ]
    decision_maker_links = []
    for role_title in _DECISION_TITLES:
        kw = f"{role_title} {skill_hint} {company}".strip()
        decision_maker_links.append({
            "label": role_title,
            "url": f"https://www.linkedin.com/search/results/people/?keywords={_qd(kw)}&origin=GLOBAL_SEARCH",
        })

    # ── LinkedIn InMail (connection request note ≤ 300 chars) ──────────────
    skills_str = ", ".join(key_matches[:3]) if key_matches else "Java & backend technologies"
    inmail = (
        f"Hi! I'm a {exp_years}+ yrs Java engineer specialising in {skills_str}. "
        f"Very interested in the {title} role at {company} — my profile is a strong "
        f"{match_score}/10 match. Would love to connect!"
    )
    if len(inmail) > 295:
        inmail = inmail[:292] + "…"

    # ── Cold email ─────────────────────────────────────────────────────────
    subject = f"Application for {title} – {company}"
    if cover_letter:
        body = cover_letter
        sig = f"\n\nBest regards,\n{cand_name}" + (f"\n{cand_email}" if cand_email else "")
        if cand_name and cand_name.lower() not in body.lower()[-80:]:
            body += sig
    else:
        body = (
            f"Dear Hiring Manager,\n\n"
            f"I am writing to express my strong interest in the {title} position at {company}.\n\n"
            f"With {exp_years}+ years of experience and expertise in "
            f"{', '.join(key_matches[:4]) or 'software engineering'}, I believe I am a "
            f"strong fit for this role.\n\n"
            f"I would welcome the opportunity to discuss how my background aligns with your needs.\n\n"
            f"Best regards,\n{cand_name}" + (f"\n{cand_email}" if cand_email else "")
        )

    from urllib.parse import quote as _q
    # Gmail compose URL — encode each email separately, comma separator must NOT be encoded
    # or Gmail treats the whole string as one address
    to_param = ",".join(_q(e) for e in hr_emails)
    mailto = (
        f"https://mail.google.com/mail/?view=cm&to={to_param}&su={_q(subject)}&body={_q(body)}"
        if hr_emails else ""
    )

    return jsonify({
        "company":            company,
        "title":              title,
        "job_url":            job_url,
        "job_url_is_company_site": apply_link_is_company_site,
        "linkedin_inmail":    inmail,
        "cold_email_subject": subject,
        "cold_email_body":    body,
        "hr_emails":          hr_emails,
        "hr_verified":        bool(hunter_key and hr_emails and not hr_emails[0].startswith("careers@")),
        "mailto":             mailto,
        "referral_contacts":  referral_contacts,
        "decision_maker_links": decision_maker_links,
    })


@app.route("/verify-emails", methods=["POST"])
def verify_emails_route():
    """SMTP-level check (RCPT TO probe, no message sent) for a list of guessed HR emails."""
    data = request.get_json(silent=True) or {}
    emails = data.get("emails", [])[:10]
    if not emails:
        return jsonify({"ok": False, "message": "No emails provided"}), 400
    from email_verifier import verify_emails
    try:
        results = verify_emails(emails)
        return jsonify({"ok": True, "results": results})
    except Exception as e:
        logger.warning(f"Email verification failed: {e}")
        return jsonify({"ok": False, "message": str(e)}), 500


@app.route("/apply/<job_id>", methods=["POST"])
def mark_applied(job_id):
    data = request.get_json(silent=True) or {}
    applied = data.get("applied", True)
    job_store.mark_applied(job_id, applied)
    return jsonify({"ok": True, "applied": applied})


@app.route("/job-status/<job_id>", methods=["POST"])
def set_job_status(job_id):
    data = request.get_json(silent=True) or {}
    status = data.get("status", "")   # "rejected" | "interview" | ""
    job_store.update_job(job_id, job_status=status)
    return jsonify({"ok": True, "job_status": status})


_gmail_check_running: set[str] = set()  # profile names currently checking

def _bg_check_responses(profile: str):
    profiles.set_active_profile(profile)
    try:
        from gmail_checker import check_responses
        applied = job_store.applied_jobs()
        if not applied:
            return
        logger.info(f"[{profile}] Checking Gmail for {len(applied)} applied job(s)…")
        results = check_responses(applied)
        for job_id, responses in results.items():
            job_store.set_responses(job_id, responses)
        logger.info(f"[{profile}] Gmail check done — {len(results)} job(s) with responses")
    except Exception as e:
        logger.exception(f"[{profile}] Gmail check failed: {e}")
    finally:
        _gmail_check_running.discard(profile)


@app.route("/check-responses", methods=["POST"])
def check_responses():
    profile = profiles.get_active_profile()
    if profile in _gmail_check_running:
        return jsonify({"ok": False, "message": "Already checking"})
    _gmail_check_running.add(profile)
    t = threading.Thread(target=_bg_check_responses, args=(profile,), daemon=True)
    t.start()
    return jsonify({"ok": True, "message": "Checking Gmail inbox…"})


@app.route("/check-responses-status")
def check_responses_status():
    return jsonify({"running": profiles.get_active_profile() in _gmail_check_running})


@app.route("/remove/<job_id>", methods=["POST"])
def remove_job(job_id):
    job_store.remove_job(job_id)
    return jsonify({"ok": True})


# ── Automation Board ───────────────────────────────────────────────────────────

@app.route("/automation-board")
def automation_board():
    _select_profile()
    import automation_log as _al
    runs    = _al.all_runs()
    counts  = _al.run_counts()
    pending = _al.pending_jobs()
    return render_template(
        "automation_board.html",
        runs=runs,
        counts=counts,
        pending=pending,
    )


@app.route("/api/automation-board")
def api_automation_board():
    """JSON feed for automation board — used for live refresh."""
    _select_profile()
    import automation_log as _al
    return jsonify({
        "runs":    _al.all_runs(limit=200),
        "counts":  _al.run_counts(),
        "pending": _al.pending_jobs(),
    })


@app.route("/clear", methods=["POST"])
def clear():
    job_store.clear_all()
    return jsonify({"ok": True})


# ── Resume Import & Settings ──────────────────────────────────────────────

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
    r"|CERTIFICATIONS|(?:KEY\s*|PERSONAL\s*|NOTABLE\s*|SIDE\s*)?PROJECTS"
    r"|ACHIEVEMENTS|INTERESTS|LANGUAGES)$",
    re.I,
)
# Explicit bullet markers — a new bullet starts ONLY on one of these (or the first
# line of a block). Any other line is a PDF line-wrap continuation of the bullet
# in progress, not a new bullet — merging them back avoids splitting one sentence
# into multiple fragments (the artifact that produced e.g. "...as full legacy" /
# "modernization of the monolithic..." as two separate bullets).
_BULLET_MARKER_PAT = re.compile(r"^[•\-*▸►→]|^\d+[.)]\s")
_BULLET_STRIP_PAT  = re.compile(r"^[•\-*▸►→]\s*|^\d+[.)]\s*")


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

    summary_lines  = _get_section("SUMMARY", "PROFILE", "OBJECTIVE", "ABOUT")
    exp_lines      = _get_section("EXPERIENCE", "EMPLOYMENT", "WORK HISTORY", "WORKHISTORY")
    project_lines  = _get_section("PROJECTS", "KEYPROJECTS", "PERSONALPROJECTS",
                                   "NOTABLEPROJECTS", "SIDEPROJECTS")
    skills_lines   = _get_section("SKILLS", "TECHNICALSKILLS", "CORESKILLS")
    edu_lines      = _get_section("EDUCATION", "ACADEMIC", "QUALIFICATIONS")

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

    for i, ln in enumerate(exp_lines):
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
            if _BULLET_MARKER_PAT.match(ln):
                bullet = _BULLET_STRIP_PAT.sub("", ln).strip()
                cur_job["bullets"].append(bullet)
            else:
                # Is this line the next job's title/company, or bullet content?
                # A header line is followed within 2 lines by a date; content
                # never is. (A fixed length threshold doesn't work here — a
                # PDF line-wrap tail like "support and REST API layer." is
                # short but is still bullet content, not a job header.)
                upcoming = exp_lines[i + 1:i + 3]
                if len(ln) <= 70 and any(_DATE_PAT.search(u) for u in upcoming):
                    pending_header.append(ln)
                else:
                    # No marker — CSS-only list bullets (the common case for
                    # resumes rendered via Chrome print-to-PDF) leave no glyph
                    # in the text layer at all, so marker presence can't tell
                    # wrap-continuation apart from a genuine new bullet.
                    # Sentence-terminal punctuation can: if the previous bullet
                    # already ended a sentence, this line starts a new one;
                    # otherwise it's a wrapped continuation.
                    prev = cur_job["bullets"][-1] if cur_job["bullets"] else ""
                    if prev and not prev.rstrip().endswith((".", "!", "?", ":")):
                        cur_job["bullets"][-1] = f"{prev} {ln}".strip()
                    else:
                        # First line of this job's bullets, plain-paragraph resume
                        cur_job["bullets"].append(ln)

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

    # ── Parse Projects: "Name [role]" title line, then a merged description ──
    # (best-effort — arbitrary resume layouts vary a lot; this covers the common
    # "short title line, then wrapped prose" shape)
    project_blocks: list[dict] = []
    cur_proj: dict | None = None
    for ln in project_lines:
        looks_like_title = len(ln) <= 70 and not ln.rstrip().endswith((".", ",", ";"))
        starts_new = cur_proj is None or (
            looks_like_title and (not cur_proj["desc"] or cur_proj["desc"][-1].endswith((".", "!", "?")))
        )
        if starts_new:
            if cur_proj:
                project_blocks.append(cur_proj)
            cur_proj = {"name": ln, "desc": []}
        elif len(ln) > 30 and cur_proj["desc"]:
            # PDF line-wrap continuation — merge into the running description
            cur_proj["desc"][-1] = f"{cur_proj['desc'][-1]} {ln}".strip()
        else:
            cur_proj["desc"].append(ln)
    if cur_proj:
        project_blocks.append(cur_proj)
    project_blocks = [p for p in project_blocks if len(p["name"]) > 3 and p["desc"]]

    # ── Parse Skills: split on common separators into flat tag list ──────────
    skill_tags: list[str] = []
    for ln in skills_lines:
        skill_tags.extend(t.strip() for t in re.split(r"[•·,|]", ln) if t.strip())

    # ── Parse Education: anchor on the date line, take preceding lines as
    #    degree/school (best-effort — falls back to first two lines if no date) ─
    education = None
    for i, ln in enumerate(edu_lines):
        dm = _DATE_PAT.search(ln)
        if dm:
            preceding = [l for l in edu_lines[:i] if not _DATE_PAT.search(l)]
            education = {
                "degree": preceding[0] if preceding else "",
                "school": preceding[1] if len(preceding) > 1 else "",
                "year":   dm.group(0),
            }
            break
    if education is None and edu_lines:
        education = {
            "degree": edu_lines[0],
            "school": edu_lines[1] if len(edu_lines) > 1 else "",
            "year":   "",
        }

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

    projects_html = ""
    for pb in project_blocks:
        desc = " ".join(pb["desc"])
        projects_html += f"""
        <div class="project">
          <div class="project-name">{esc(pb['name'])}</div>
          <p>{esc(desc)}</p>
        </div>"""

    skills_html = ""
    if skill_tags:
        tags_html = "".join(f'<span class="tag">{esc(t)}</span>' for t in skill_tags)
        skills_html = f"""
        <div class="skill-group">
          <div class="skill-tags">{tags_html}</div>
        </div>"""

    edu_html = ""
    if education:
        edu_html = f"""
        <div class="edu-block">
          <div class="edu-degree">{esc(education['degree'])}</div>
          <div class="edu-school">{esc(education['school'])}</div>
          <div class="edu-year">{esc(education['year'])}</div>
        </div>"""

    summary_html = esc(" ".join(summary_lines)) if summary_lines else ""

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"/>
  <title>{esc(name)} – Resume</title>
  <style>
    /* Disable ligature substitution (fi/ffi -> single glyph) so PDF text
       extraction (ATS parsers) reads plain "fi"/"ffi", not a ligature glyph
       most keyword matching won't recognize. */
    body{{font-family:'Segoe UI',Arial,sans-serif;max-width:860px;margin:40px auto;padding:0 32px;color:#1e1e1e;
         font-feature-settings:"liga" 0,"clig" 0,"dlig" 0;-webkit-font-feature-settings:"liga" 0}}
    h1{{font-size:28px;font-weight:700;margin-bottom:4px}}
    .contact{{font-size:13px;color:#555;margin-bottom:18px}}
    /* No letter-spacing — Chrome's PDF text layer bakes letter-spacing in as
       literal space characters between every letter ("C O R E"), which breaks
       ATS section-header matching. */
    .section-title{{font-size:13px;font-weight:700;text-transform:uppercase;
                    color:#0d47a1;border-bottom:1.5px solid #0d47a1;padding-bottom:4px;margin:22px 0 10px}}
    .summary-text{{font-size:14px;line-height:1.7;color:#333}}
    .job,.project{{margin-bottom:18px}}
    .job-title,.project-name{{font-size:15px;font-weight:700}}
    .job-company{{font-size:13px;color:#555}}
    .job-date{{font-size:12px;color:#888;margin-bottom:6px}}
    ul{{margin:6px 0 0 18px;padding:0}}
    li{{font-size:13px;line-height:1.65;margin-bottom:3px}}
    .project p{{font-size:13px;line-height:1.6;color:#333;margin-top:4px}}
    .skill-tags{{display:flex;flex-wrap:wrap;gap:4px 14px;font-size:13px}}
    .tag{{color:#0d47a1}}
    .tag:not(:last-child)::after{{content:", ";color:#1e1e1e}}
    .edu-degree{{font-weight:700;font-size:13px}}
    .edu-school{{color:#0d47a1;font-size:13px}}
    .edu-year{{color:#888;font-size:12px}}
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
{f'''
  <div class="section-title">Key Projects</div>
  {projects_html}''' if projects_html else ''}
{f'''
  <div class="section-title">Core Skills</div>
  {skills_html}''' if skills_html else ''}
{f'''
  <div class="section-title">Education</div>
  {edu_html}''' if edu_html else ''}
</body>
</html>"""

    meta = {
        "name": name, "email": email, "phone": phone,
        "counts": {
            "jobs": len(job_blocks), "projects": len(project_blocks),
            "skills": len(skill_tags), "education": 1 if education else 0,
        },
    }
    return html, meta


def _extract_resume_meta(html: str) -> dict:
    """Extract candidate name, email, phone, and job title/role from resume HTML."""
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

    # Job title / target role — first job-title element, then h2 subtitle, then summary
    title = ""
    # 1. First element with class containing 'job-title', 'position', 'role-title' (not summary)
    for el in soup.find_all(class_=re.compile(r"\bjob.?title\b|\bposition.?title\b|\brole.?title\b", re.I)):
        text = el.get_text(strip=True)
        if 3 < len(text) < 80:
            title = text
            break
    # 2. <h2> right after <h1> name (common single-page resume pattern)
    if not title and h1:
        sib = h1.find_next_sibling()
        if sib and sib.name in ("h2", "h3", "p"):
            text = sib.get_text(strip=True)
            if 3 < len(text) < 80 and "@" not in text and "summary" not in text.lower():
                title = text
    # 3. First <h2>/<h3> that looks like a job title (not a section header)
    if not title:
        for el in soup.find_all(["h2", "h3"]):
            text = el.get_text(strip=True)
            if 3 < len(text) < 80 and "summary" not in text.lower() and "experience" not in text.lower():
                title = text
                break

    return {"name": name, "email": email, "phone": phone, "title": title}


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
    resume_path = profiles.base_resume_path()
    backup = resume_path.with_suffix(".html.bak")
    if resume_path.exists():
        backup.write_bytes(resume_path.read_bytes())

    resume_path.parent.mkdir(parents=True, exist_ok=True)
    resume_path.write_text(html, encoding="utf-8")

    # Use metadata extracted directly from PDF (avoids re-parsing glued HTML artifacts)
    # For HTML uploads, parse metadata from the HTML structure
    meta = pdf_meta if pdf_meta is not None else _extract_resume_meta(html)

    # Update config.json candidate section with extracted info
    cfg = _load_config()
    if meta["name"]:
        cfg["candidate"]["name"] = meta["name"]
    if meta["email"]:
        cfg["candidate"]["email"] = meta["email"]
    # Save extracted job title as the target role for career-site fetching
    if meta.get("title"):
        cfg.setdefault("job_search", {})["target_role"] = meta["title"]
    profiles.config_path().write_text(json.dumps(cfg, indent=2), encoding="utf-8")

    logger.info(f"Resume imported: {meta['name']} <{meta['email']}>")

    # A new resume means a new set of relevant jobs — dashboard entries from
    # the previous resume that were never applied to are just noise now
    # (they were fetched/scored against someone else's role and skills).
    # Anything with real application history is untouched regardless.
    from job_fetcher import release_seen_ids
    cleared_ids = job_store.clear_untracked_jobs()
    if cleared_ids:
        release_seen_ids(set(cleared_ids))
        logger.info(f"Cleared {len(cleared_ids)} untracked jobs from the previous resume")

    # PDF extraction is best-effort — warn rather than silently ship a resume
    # missing sections a human would notice (skills feed the job-relevance
    # filter in job_fetcher.py, so a thin extraction there also weakens fetch).
    warning = None
    counts = meta.get("counts")
    if counts is not None:
        thin = [label for label, key in
                (("experience", "jobs"), ("skills", "skills"), ("education", "education"))
                if not counts.get(key)]
        if thin:
            warning = (f"Heads up: couldn't find a {'/'.join(thin)} section in this PDF — "
                        f"review base_resume.html and fill in what's missing.")

    return jsonify({
        "ok": True,
        "message": "Resume imported successfully",
        "meta": meta,
        "warning": warning,
        "cleared_jobs": len(cleared_ids),
        "has_structure": bool(BeautifulSoup(html, "html.parser").find(class_="summary-text")),
    })


@app.route("/resume-base")
def resume_base():
    """Show the current base resume HTML."""
    resume_path = profiles.base_resume_path()
    if not resume_path.exists():
        return "No base resume found", 404
    return Response(resume_path.read_text(encoding="utf-8"), mimetype="text/html")


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
        profiles.config_path().write_text(json.dumps(cfg, indent=2), encoding="utf-8")
        logger.info(f"Settings updated: {cfg['candidate']}")
        return jsonify({"ok": True, "candidate": cfg["candidate"]})
    cfg = _load_config()
    has_resume = profiles.base_resume_path().exists()
    return jsonify({"candidate": cfg["candidate"], "has_resume": has_resume})


def _startup_fetch_check():
    """Auto-fetch on startup for every profile not already fetched today.
    Runs in its own thread so the server starts serving immediately rather
    than waiting for every profile's fetch to finish; profiles are still
    fetched one at a time within this thread, not in parallel, to avoid
    hammering job boards from every profile at once."""
    for name in profiles.list_profiles():
        profiles.set_active_profile(name)
        if _get_last_fetch_date() != str(date.today()):
            logger.info(f"[{name}] New day detected — auto-fetching jobs on startup")
            _bg_fetch(name)
        else:
            logger.info(f"[{name}] Already fetched today ({date.today()}) — skipping startup fetch")


def _daily_scheduler():
    """Background thread: trigger a fetch every day at 08:00 local time,
    once per profile, sequentially."""
    while True:
        now = datetime.now()
        # Seconds until next 08:00
        target = now.replace(hour=8, minute=0, second=0, microsecond=0)
        if now >= target:
            target = target.replace(day=target.day + 1)
        wait_secs = (target - now).total_seconds()
        logger.info(f"Daily scheduler: next fetch in {wait_secs/3600:.1f} h (at 08:00)")
        time.sleep(wait_secs)
        logger.info("Daily scheduler: triggering morning fetch for all profiles")
        for name in profiles.list_profiles():
            if not _fs(name)["running"]:
                _bg_fetch(name)


if __name__ == "__main__":
    profiles.migrate_legacy_layout()

    startup = threading.Thread(target=_startup_fetch_check, daemon=True)
    startup.start()

    # Start background daily scheduler (fires at 08:00 every morning)
    sched = threading.Thread(target=_daily_scheduler, daemon=True)
    sched.start()

    port = int(os.environ.get("PORT", 5000))
    app.run(debug=False, host="0.0.0.0", port=port, use_reloader=False)
