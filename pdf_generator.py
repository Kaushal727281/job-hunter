"""
pdf_generator.py
Converts tailored HTML resume to PDF using headless Chrome.
Chrome must be installed (standard on macOS).
"""

import subprocess
import logging
import shutil
from pathlib import Path

logger = logging.getLogger(__name__)

# Candidate Chrome binary paths (macOS + Linux)
CHROME_PATHS = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium-browser",
    "/usr/bin/chromium",
]


def _find_chrome() -> str:
    for path in CHROME_PATHS:
        if Path(path).exists():
            return path
    # Fall back to PATH
    for name in ("google-chrome", "chromium", "chromium-browser"):
        found = shutil.which(name)
        if found:
            return found
    raise FileNotFoundError(
        "Chrome/Chromium not found. Install Google Chrome or set CHROME_PATH env var."
    )


def html_to_pdf(html_path: Path, pdf_path: Path) -> Path:
    """
    Converts an HTML file to PDF using headless Chrome.
    Returns the pdf_path on success.
    """
    chrome = _find_chrome()
    cmd = [
        chrome,
        "--headless",
        "--disable-gpu",
        "--no-sandbox",
        "--disable-dev-shm-usage",
        f"--print-to-pdf={pdf_path.resolve()}",
        "--print-to-pdf-no-header",
        str(html_path.resolve()),
    ]
    logger.info(f"Generating PDF: {pdf_path.name}")
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        logger.error(f"Chrome PDF error: {result.stderr[:500]}")
        raise RuntimeError(f"headless Chrome failed (exit {result.returncode})")
    return pdf_path


def save_and_convert(html_content: str, output_dir: Path, filename_stem: str) -> Path:
    """
    Writes HTML to a temp file, converts to PDF, returns PDF path.
    Also keeps the HTML file alongside the PDF (useful for debugging).
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    html_path = output_dir / f"{filename_stem}.html"
    pdf_path = output_dir / f"{filename_stem}.pdf"

    html_path.write_text(html_content, encoding="utf-8")
    html_to_pdf(html_path, pdf_path)
    return pdf_path
