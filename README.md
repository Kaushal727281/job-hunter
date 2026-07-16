# Job Hunter

A personal job-hunting dashboard that fetches jobs daily from LinkedIn, Glassdoor, Shine, Indeed and more — then lets you tailor your resume for each role using Groq AI (free). Download tailored PDFs in 5 layouts, track applications, and check Gmail for recruiter replies.

**You review and apply manually — no auto-apply, no account bans.**

---

## What it does

- Fetches new jobs every morning (configurable hour) across multiple job boards
- Scores and filters jobs by experience, keywords, and company type
- **Tailor Resume** button rewrites your summary + bullets to match the job description
- Highlights matching keywords in bold in the tailored PDF
- 5 PDF layout options: Classic, Modern Sidebar, Tech/FAANG, Executive, Compact
- Diff view to see exactly what changed vs your base resume
- Tracks which jobs you've applied to
- Checks Gmail for recruiter replies on applied jobs

---

## Requirements

- Python 3.10+
- Google Chrome or Chromium (for PDF generation)
- A free [Groq](https://console.groq.com) account (100K tokens/day free)

---

## Setup (5 steps)

### 1. Clone and install dependencies

```bash
git clone https://github.com/Kaushal727281/job-hunter.git
cd job-hunter
pip install -r requirements.txt
```

### 2. Get a free Groq API key

1. Go to [console.groq.com](https://console.groq.com) → Sign up (free)
2. Click **API Keys** → **Create API Key**
3. Copy the key (shown only once)

### 3. Configure your environment

```bash
cp .env.example .env
```

Edit `.env` and fill in your values:

```
GROQ_API_KEY=gsk_...your_key_here...

# Optional — only needed for the Gmail reply-checker feature
GMAIL_ADDRESS=you@gmail.com
GMAIL_APP_PASSWORD=xxxx xxxx xxxx xxxx
```

> **Gmail App Password** (only if you want Gmail reply checking):
> Go to [myaccount.google.com/security](https://myaccount.google.com/security) → Enable 2-Step Verification → Search "App Passwords" → Generate one for Mail.

### 4. Add your resume and config

**Resume:**
Replace `base_resume.html` with your own resume in HTML format.
The file must use these CSS classes for the AI tailor to work:
- `.summary-text` — your profile/summary paragraph
- `.job` — each experience block, with `.job-title`, `.job-company`, `.duration`
- `ul > li` inside `.job` — bullet points (these get rewritten)
- `.skill-group` with `.skill-group-label` and `.tag` chips — skills sidebar

> Tip: Open the included `base_resume.html` in a browser first to see the expected structure, then replace the content with your own.

**Config:**
```bash
cp config.example.json config.json
```

Edit `config.json`:
- `candidate.name` / `candidate.email` — your details
- `job_search.queries` — job titles to search for
- `job_search.locations` — cities or "Remote India"
- `filters.min_experience_years` — filter out junior roles

### 5. Run the app

```bash
python app.py
```

Open [http://localhost:5000](http://localhost:5000) in your browser.

---

## Daily Usage

| Action | How |
|---|---|
| Fetch new jobs | Click **Fetch New Jobs** in the dashboard |
| Tailor resume for a job | Click **✨ Tailor Resume** on any job card |
| View tailored resume | Click **📄 View Resume** |
| See what changed | Click **🔍 Diff** |
| Download PDF | Click **⬇ PDF ▾** → choose a layout |
| Mark as applied | Click **Mark Applied** |

---

## Running in the background (keep alive)

```bash
# Option A: tmux (recommended)
tmux new -s jobhunter
python app.py
# Ctrl+B then D to detach

# Option B: nohup
nohup python app.py > app.log 2>&1 &
```

The app auto-fetches new jobs every morning at 8 AM (configurable via `config.json` → `email.send_hour`).

---

## Project structure

```
job-hunter/
├── app.py                  # Flask web app — main entry point
├── job_fetcher.py          # Scrapes LinkedIn, Glassdoor, Shine, Indeed etc.
├── resume_tailor.py        # Groq AI resume rewriter
├── pdf_generator.py        # HTML → PDF via headless Chrome
├── job_store.py            # JSON-based job database (output/jobs.json)
├── gmail_checker.py        # Checks Gmail for recruiter replies
├── base_resume.html        # YOUR base resume (replace with your own)
├── config.json             # Your search preferences (gitignored after setup)
├── config.example.json     # Template — copy to config.json
├── .env.example            # Template — copy to .env
├── requirements.txt        # Python dependencies
├── templates/
│   ├── index.html          # Dashboard
│   ├── job_detail.html     # Job detail + resume preview
│   └── layouts/            # 5 PDF layout templates
│       ├── classic.html
│       ├── modern.html
│       ├── tech.html
│       ├── executive.html
│       └── compact.html
└── output/                 # Generated PDFs + job database (gitignored)
```

---

## Troubleshooting

| Problem | Fix |
|---|---|
| `GROQ_API_KEY not set` | Check your `.env` file has `GROQ_API_KEY=gsk_...` |
| `Groq rate limit — try again in Xm` | Free tier: 100K tokens/day. Wait and retry. |
| `Chrome/Chromium not found` | Install [Google Chrome](https://www.google.com/chrome/) |
| PDF downloads but is blank | Make sure Chrome is not sandboxed; try `--no-sandbox` (already set) |
| No jobs fetched | Check internet connection; broaden `config.json` queries or increase `days_old` |
| Tailored resume looks plain | Re-tailor the job — old resumes used a previous template |
| Gmail replies not showing | Set `GMAIL_ADDRESS` and `GMAIL_APP_PASSWORD` in `.env` |
