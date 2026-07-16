# Job Hunter Tool

Fetches new jobs every morning, tailors your resume for each with Claude AI, and emails you a digest with PDF attachments.

**You review and apply manually** — no auto-apply, no account bans.

---

## Quick Setup (15 minutes)

### Step 1 — Install Python dependencies

```bash
cd job-hunter/
pip install -r requirements.txt
```

### Step 2 — Get your API keys (all free)

#### A. RapidAPI / JSearch (500 free requests/month)
1. Go to [rapidapi.com](https://rapidapi.com) → Sign up / Log in
2. Search for **"JSearch"** API
3. Click **Subscribe to Test** → choose the **Free** plan (500 req/mo)
4. Copy your **X-RapidAPI-Key** from the API console header

#### B. Anthropic API ($5 free credit on signup)
1. Go to [console.anthropic.com](https://console.anthropic.com) → Sign up
2. Go to **API Keys** → **Create Key**
3. Copy the key (shown only once — save it!)

#### C. Gmail App Password (no cost)
> Required because Gmail blocks plain password login from scripts.

1. Go to [myaccount.google.com/security](https://myaccount.google.com/security)
2. Enable **2-Step Verification** (if not already enabled)
3. Search for **"App Passwords"** in the search bar
4. Select app: **Mail** → device: **Mac** → Generate
5. Copy the 16-character password (format: `xxxx xxxx xxxx xxxx`)

### Step 3 — Configure your .env

```bash
cp .env.example .env
```

Edit `.env` and fill in all values:
```
RAPIDAPI_KEY=abc123...
ANTHROPIC_API_KEY=sk-ant-...
GMAIL_ADDRESS=you@gmail.com
GMAIL_APP_PASSWORD=xxxx xxxx xxxx xxxx
DIGEST_RECIPIENT=you@gmail.com
```

### Step 4 — Verify your config

Open `config.json` and adjust:
- `queries` — job titles to search for
- `locations` — where to look
- `max_jobs_per_run` — max new jobs per day (default 20)
- `top_n_with_pdf` — how many tailored PDFs to attach in email (default 5)
- `send_hour` — what hour to run daily (default 8 = 8:00 AM)

### Step 5 — Test run

```bash
python main.py --test
```

This will:
- Fetch 3 jobs from JSearch
- Tailor your resume for each using Claude
- Save PDFs to `output/YYYY-MM-DD/`
- Print results to terminal (does NOT send email)

Check the `output/` folder to see your tailored resumes and cover notes.

### Step 6 — Start the daily scheduler

```bash
python scheduler.py
```

This runs in the foreground. To keep it running after closing terminal:

```bash
# Option A: tmux (recommended)
tmux new -s jobhunter
python scheduler.py
# Ctrl+B then D to detach

# Option B: nohup
nohup python scheduler.py > scheduler.log 2>&1 &
```

---

## Output Structure

```
output/
  2025-01-20/
    Google-Lead_Java_Engineer/
      resume.html        — tailored HTML (debug)
      resume.pdf         — tailored PDF (sent in email)
      cover_note.txt     — 3-sentence cover note
    Amazon-Senior_Java_Developer/
      resume.html
      resume.pdf
      cover_note.txt
```

---

## Daily Email

You'll receive an email each morning like:

| # | Role / Company | Location | Salary | Match | Action |
|---|---|---|---|---|---|
| 1 | Lead Java Engineer / Google | Bengaluru | ₹40–60 LPA | **9/10** | Apply → |
| 2 | Senior Java Dev / Amazon | Remote | Not disclosed | **7/10** | Apply → |

Top 5 tailored PDFs are attached to the email.

---

## Troubleshooting

| Problem | Fix |
|---|---|
| `RAPIDAPI_KEY not set` | Check your `.env` file |
| `Chrome not found` | Install Google Chrome or set `CHROME_PATH` env var |
| Gmail auth error | Regenerate App Password; don't use your regular password |
| No jobs found | Try broadening search in `config.json` or change `date_posted` to `"3days"` |
| Claude JSON parse error | Rare — check `job_hunter.log` for raw API response |

---

## Files

| File | Purpose |
|---|---|
| `main.py` | Orchestrator — run this |
| `job_fetcher.py` | Calls JSearch API, deduplicates jobs |
| `resume_tailor.py` | Sends JD + resume to Claude, gets tailored HTML |
| `pdf_generator.py` | HTML → PDF via headless Chrome |
| `email_sender.py` | Sends digest email via Gmail SMTP |
| `scheduler.py` | Daily 8 AM trigger |
| `base_resume.html` | Your base resume (source of truth) |
| `config.json` | Search preferences |
| `seen_jobs.json` | Auto-generated — tracks seen job IDs |
| `.env` | Your secrets (gitignored) |
