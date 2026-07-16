"""
email_sender.py
Sends the morning job digest email with:
  - HTML table of all new jobs
  - Top N tailored PDF resumes as attachments
"""

import os
import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.application import MIMEApplication
from pathlib import Path
from datetime import date
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)


def _build_html(jobs_with_results: list[dict], today: str) -> str:
    rows = ""
    for i, item in enumerate(jobs_with_results, 1):
        job = item["job"]
        result = item.get("tailor_result", {})
        score = result.get("match_score", "—")
        score_color = "#2e7d32" if isinstance(score, int) and score >= 7 else (
            "#f57c00" if isinstance(score, int) and score >= 5 else "#c62828"
        )
        pdf_note = "✅ PDF attached" if item.get("pdf_path") else ""
        rows += f"""
        <tr style="border-bottom:1px solid #e0e0e0;">
          <td style="padding:10px 8px;font-weight:600;color:#0d47a1;">{i}</td>
          <td style="padding:10px 8px;">
            <strong>{job['title']}</strong><br>
            <span style="color:#555;font-size:13px;">{job['company']}</span>
          </td>
          <td style="padding:10px 8px;color:#444;">{job['location']}{'&nbsp;🌐' if job.get('is_remote') else ''}</td>
          <td style="padding:10px 8px;color:#444;">{job.get('salary','—')}</td>
          <td style="padding:10px 8px;text-align:center;">
            <span style="background:{score_color};color:#fff;padding:2px 8px;border-radius:12px;font-weight:700;">{score}</span>
          </td>
          <td style="padding:10px 8px;">
            <a href="{job.get('apply_link','#')}" style="color:#1976d2;text-decoration:none;font-weight:600;">Apply →</a>
            <br><span style="font-size:12px;color:#666;">{pdf_note}</span>
          </td>
        </tr>"""

    return f"""<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"></head>
<body style="font-family:'Segoe UI',Arial,sans-serif;background:#f5f7fa;padding:20px;">
  <div style="max-width:820px;margin:0 auto;background:#fff;border-radius:8px;
              box-shadow:0 2px 12px rgba(0,0,0,0.08);overflow:hidden;">

    <div style="background:linear-gradient(135deg,#0d47a1,#1976d2);padding:28px 36px;color:#fff;">
      <h1 style="margin:0;font-size:22px;">☀️ Morning Job Digest — {today}</h1>
      <p style="margin:6px 0 0;opacity:0.88;">{len(jobs_with_results)} new jobs found today</p>
    </div>

    <div style="padding:28px 36px;">
      <table style="width:100%;border-collapse:collapse;">
        <thead>
          <tr style="background:#f0f4f9;text-align:left;">
            <th style="padding:10px 8px;color:#0d47a1;font-size:12px;text-transform:uppercase;">#</th>
            <th style="padding:10px 8px;color:#0d47a1;font-size:12px;text-transform:uppercase;">Role / Company</th>
            <th style="padding:10px 8px;color:#0d47a1;font-size:12px;text-transform:uppercase;">Location</th>
            <th style="padding:10px 8px;color:#0d47a1;font-size:12px;text-transform:uppercase;">Salary</th>
            <th style="padding:10px 8px;color:#0d47a1;font-size:12px;text-transform:uppercase;text-align:center;">Match</th>
            <th style="padding:10px 8px;color:#0d47a1;font-size:12px;text-transform:uppercase;">Action</th>
          </tr>
        </thead>
        <tbody>
          {rows}
        </tbody>
      </table>

      <p style="margin-top:24px;color:#666;font-size:13px;border-top:1px solid #eee;padding-top:16px;">
        Top {min(5, len([i for i in jobs_with_results if i.get('pdf_path')]))} tailored resume PDFs are attached.<br>
        Review, pick your best fits, and apply manually. Good luck! 🚀
      </p>
    </div>
  </div>
</body>
</html>"""


def send_digest(jobs_with_results: list[dict], config: dict):
    """
    Send the morning digest email.
    jobs_with_results: list of dicts with keys: job, tailor_result (optional), pdf_path (optional)
    """
    gmail_addr = os.getenv("GMAIL_ADDRESS")
    app_password = os.getenv("GMAIL_APP_PASSWORD")
    recipient = os.getenv("DIGEST_RECIPIENT", gmail_addr)

    if not gmail_addr or not app_password:
        raise EnvironmentError("GMAIL_ADDRESS and GMAIL_APP_PASSWORD must be set in .env")

    today = str(date.today())
    email_cfg = config.get("email", {})
    subject = f"{email_cfg.get('subject_prefix', '[Job Digest]')} {len(jobs_with_results)} new jobs — {today}"

    msg = MIMEMultipart("mixed")
    msg["Subject"] = subject
    msg["From"] = gmail_addr
    msg["To"] = recipient

    html_body = _build_html(jobs_with_results, today)
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    # Attach top N PDFs
    attached = 0
    top_n = email_cfg.get("top_n_with_pdf", 5)
    for item in jobs_with_results:
        if attached >= top_n:
            break
        pdf_path = item.get("pdf_path")
        if pdf_path and Path(pdf_path).exists():
            job = item["job"]
            safe_name = f"{job['company']}-{job['title']}".replace("/", "-").replace(" ", "_")[:60]
            with open(pdf_path, "rb") as f:
                part = MIMEApplication(f.read(), _subtype="pdf")
                part.add_header("Content-Disposition", "attachment", filename=f"{safe_name}.pdf")
                msg.attach(part)
            attached += 1

    logger.info(f"Sending digest to {recipient} ({len(jobs_with_results)} jobs, {attached} PDFs)")
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(gmail_addr, app_password)
        server.sendmail(gmail_addr, recipient, msg.as_string())
    logger.info("Digest email sent successfully")
