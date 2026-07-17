"""
pdf_generator.py
Converts tailored HTML resume to PDF using headless Chrome via CDP.
Uses Chrome DevTools Protocol so displayHeaderFooter=false is guaranteed —
the old --print-to-pdf-no-header flag is unreliable on Chrome 112+.
"""

import base64
import json
import logging
import shutil
import subprocess
import tempfile
import time
import urllib.request
import websocket
from pathlib import Path

logger = logging.getLogger(__name__)

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
    for name in ("google-chrome", "chromium", "chromium-browser"):
        found = shutil.which(name)
        if found:
            return found
    raise FileNotFoundError(
        "Chrome/Chromium not found. Install Google Chrome or set CHROME_PATH env var."
    )


def _cdp_print_to_pdf(html_path: Path, pdf_path: Path) -> Path:
    """
    Use Chrome DevTools Protocol to print HTML → PDF with no header/footer.
    Starts a temporary headless Chrome instance on a random debug port.
    """
    chrome = _find_chrome()
    port   = 9322   # use a fixed non-standard port to avoid conflicts

    proc = subprocess.Popen(
        [
            chrome,
            "--headless",
            "--disable-gpu",
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-extensions",
            f"--remote-debugging-port={port}",
            f"--remote-allow-origins=http://localhost:{port}",
            "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    try:
        # Wait for Chrome to start accepting connections
        ws_url = None
        for _ in range(30):
            try:
                # GET /json returns list of existing tabs (Chrome starts with one)
                with urllib.request.urlopen(
                    f"http://localhost:{port}/json", timeout=2
                ) as resp:
                    tabs = json.loads(resp.read())
                    # Find a page tab (not devtools/extensions)
                    for tab in tabs:
                        if tab.get("type") == "page":
                            ws_url = tab["webSocketDebuggerUrl"]
                            break
                    if ws_url:
                        break
            except Exception:
                time.sleep(0.3)

        if not ws_url:
            raise RuntimeError("Chrome CDP did not start in time")

        ws = websocket.create_connection(ws_url, timeout=30)
        msg_id = 0

        def send(method, params=None):
            nonlocal msg_id
            msg_id += 1
            ws.send(json.dumps({"id": msg_id, "method": method, "params": params or {}}))
            # Wait for matching response
            for _ in range(100):
                raw = ws.recv()
                obj = json.loads(raw)
                if obj.get("id") == msg_id:
                    return obj
            raise RuntimeError(f"CDP no response for {method}")

        # Enable Page domain
        send("Page.enable")

        # Navigate to the HTML file
        file_url = html_path.resolve().as_uri()
        send("Page.navigate", {"url": file_url})

        # Wait for page load
        for _ in range(50):
            raw = ws.recv()
            obj = json.loads(raw)
            if obj.get("method") == "Page.loadEventFired":
                break
            time.sleep(0.1)
        time.sleep(0.5)   # let fonts/images settle

        # Print to PDF — no header, no footer, print background
        resp = send("Page.printToPDF", {
            "displayHeaderFooter": False,
            "printBackground":     True,
            "preferCSSPageSize":   True,
            "paperWidth":          8.27,   # A4
            "paperHeight":         11.69,
            "marginTop":           0,
            "marginBottom":        0,
            "marginLeft":          0,
            "marginRight":         0,
        })

        pdf_data = base64.b64decode(resp["result"]["data"])
        pdf_path.write_bytes(pdf_data)
        ws.close()
        logger.info(f"CDP PDF generated: {pdf_path.name} ({len(pdf_data):,} bytes)")

    finally:
        proc.terminate()
        proc.wait(timeout=5)

    return pdf_path


def html_to_pdf(html_path: Path, pdf_path: Path) -> Path:
    """
    Converts an HTML file to PDF using headless Chrome via CDP.
    Falls back to --print-to-pdf subprocess if CDP fails.
    Returns pdf_path on success.
    """
    logger.info(f"Generating PDF: {pdf_path.name}")
    try:
        return _cdp_print_to_pdf(html_path, pdf_path)
    except Exception as e:
        logger.warning(f"CDP PDF failed ({e}), falling back to --print-to-pdf")
        chrome = _find_chrome()
        cmd = [
            chrome,
            "--headless=old",
            "--disable-gpu",
            "--no-sandbox",
            "--disable-dev-shm-usage",
            f"--print-to-pdf={pdf_path.resolve()}",
            "--print-to-pdf-no-header",
            str(html_path.resolve()),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            logger.error(f"Chrome PDF fallback error: {result.stderr[:500]}")
            raise RuntimeError(f"headless Chrome failed (exit {result.returncode})")
        return pdf_path


def save_and_convert(html_content: str, output_dir: Path, filename_stem: str) -> Path:
    """
    Writes HTML to a temp file, converts to PDF, returns PDF path.
    Also keeps the HTML file alongside the PDF (useful for debugging).
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    html_path = output_dir / f"{filename_stem}.html"
    pdf_path  = output_dir / f"{filename_stem}.pdf"

    html_path.write_text(html_content, encoding="utf-8")
    html_to_pdf(html_path, pdf_path)
    return pdf_path
