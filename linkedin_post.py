#!/usr/bin/env python3
"""
linkedin_post.py
----------------
Posts content to LinkedIn with PDF certificate attachments.
Supports deleting the N most recent posts before reposting.
"""

import sys
import time
import tempfile
import os

import dotenv
dotenv.load_dotenv(os.path.join(os.path.dirname(__file__), ".env"), override=False)

import browser_cookie3 as bc3
from playwright.sync_api import sync_playwright
from playwright_stealth import Stealth

CERT_DIR = os.path.expanduser(
    "~/gitQW/IO/Resume/job-hunter/Certificate"
)

# (post_text, pdf_filename)  — order matters for deletion (newest first to delete)
POSTS = [
    (
        """🎉 Excited to share that I just completed the Ultimate AWS Certified Developer Associate 2026 (DVA-C02) course on Udemy by Stephane Maarek — 31.5 hours of in-depth coverage of Lambda, API Gateway, DynamoDB, SQS, SNS, CloudFormation, Cognito, and much more.

As a Lead Software Engineer building cloud-native microservices with Spring Boot, this was a natural next step to deepen my AWS expertise and prepare for the official DVA-C02 exam.

Next milestone: taking the actual AWS Certified Developer – Associate exam. 🚀

#AWS #CloudDeveloper #DVA-C02 #AWSCertified #SpringBoot #CloudNative #BackendEngineering #LearningAndGrowing""",
        "UC-d0f71281-62c1-4c6e-b363-f35e4ff82da2.pdf",   # AWS DVA-C02
    ),
    (
        """🤖 Just earned the Introduction to Model Context Protocol (MCP) certificate from Anthropic!

MCP is rapidly becoming the standard protocol for connecting LLMs to external tools, APIs, data sources, and services. As someone building AI-integrated backend systems — from RAG pipelines to LLM-powered features in production — understanding MCP at the protocol level is incredibly valuable.

Excited about what this unlocks for building smarter, more composable AI architectures.

#MCP #ModelContextProtocol #Anthropic #GenerativeAI #LLM #AIEngineering #BackendDevelopment #Innovation""",
        "certificate-8jggpbfudjv3-1787300715.pdf",        # Anthropic MCP
    ),
    (
        """🐳 Sharing my Docker and Kubernetes: The Complete Guide course certificate from Udemy (Stephen Grider, 22 hours).

Containerization has become absolutely foundational to my day-to-day work since completing this — from local dev environments to deploying microservices on AKS/EKS in production. Kubernetes has transformed how we think about scalability and resilience.

Great to see how far the ecosystem has come since 2022!

#Docker #Kubernetes #DevOps #Containerization #Microservices #CloudNative #SoftwareEngineering""",
        "UC-a8532a64-7cdd-4d3d-b30b-ba88fe8edd1f.pdf",    # Docker & K8s
    ),
]


def settle(page, ms: int = 2000):
    try:
        page.wait_for_load_state("networkidle", timeout=ms)
    except Exception:
        pass
    time.sleep(0.5)


def delete_recent_posts(page, count: int = 3):
    """Delete the N most recent posts from the user's activity page."""
    print(f"\n[DELETE] Removing {count} most recent posts...")
    page.goto(
        os.environ.get("CANDIDATE_LINKEDIN", "").rstrip("/") + "/recent-activity/all/",
        wait_until="domcontentloaded", timeout=30000,
    )
    settle(page, 5000)

    deleted = 0
    for attempt in range(count * 3):   # extra attempts in case of stale elements
        if deleted >= count:
            break

        # Find the "..." more-options button on the first visible post
        more_btn_sels = [
            'button[aria-label="Open control menu"]',
            'button[aria-label*="more actions"]',
            'button[aria-label*="More options"]',
            '.feed-shared-control-menu__trigger',
            'button.update-components-more',
            '[data-control-name="update.control_menu"]',
        ]
        clicked_more = False
        for sel in more_btn_sels:
            try:
                btns = page.locator(sel)
                if btns.count() > 0 and btns.first.is_visible(timeout=2000):
                    btns.first.click()
                    settle(page, 1500)
                    clicked_more = True
                    break
            except Exception:
                pass

        if not clicked_more:
            print(f"  [WARN] Could not find more-options button on attempt {attempt+1}")
            time.sleep(1)
            continue

        # Look for "Delete" in the dropdown
        delete_sels = [
            'button:has-text("Delete post")',
            'button:has-text("Delete")',
            'li:has-text("Delete") button',
            '[data-control-name="delete_update"]',
            '.feed-shared-control-menu__item:has-text("Delete")',
        ]
        clicked_delete = False
        for sel in delete_sels:
            try:
                btn = page.locator(sel).first
                if btn.is_visible(timeout=2000):
                    btn.click()
                    settle(page, 2000)
                    clicked_delete = True
                    break
            except Exception:
                pass

        if not clicked_delete:
            print(f"  [WARN] Delete option not found in menu")
            # Close menu by pressing Escape
            page.keyboard.press("Escape")
            time.sleep(1)
            continue

        # Confirm deletion dialog if it appears
        for confirm_sel in (
            'button:has-text("Delete")',
            'button[aria-label="Delete"]',
            '.artdeco-modal button:has-text("Delete")',
        ):
            try:
                btn = page.locator(confirm_sel).first
                if btn.is_visible(timeout=2000):
                    btn.click()
                    settle(page, 3000)
                    break
            except Exception:
                pass

        deleted += 1
        print(f"  Deleted post {deleted}/{count}")
        time.sleep(2)

    print(f"[DELETE] Done — deleted {deleted} posts")
    return deleted


def post_with_document(page, text: str, pdf_path: str) -> bool:
    """Post text + PDF document to LinkedIn feed."""
    print(f"\n  Navigating to LinkedIn feed...")
    page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded", timeout=30000)
    settle(page, 4000)

    # ── Step 1: Click "Start a post" ─────────────────────────────────────────
    print("  Opening post composer...")
    clicked = False
    try:
        btn = page.get_by_role("button", name="Start a post")
        if btn.count() > 0 and btn.first.is_visible(timeout=3000):
            btn.first.click()
            settle(page, 3000)
            clicked = True
            print("  Opened composer via 'Start a post' button")
    except Exception:
        pass

    if not clicked:
        try:
            page.evaluate("""() => {
                const el = [...document.querySelectorAll('button, div[role="button"]')]
                    .find(e => e.textContent.trim() === 'Start a post' ||
                               e.getAttribute('aria-label') === 'Start a post');
                if (el) el.click();
            }""")
            settle(page, 3000)
            clicked = True
            print("  Opened composer via JS")
        except Exception:
            pass

    if not clicked:
        page.screenshot(path="/tmp/li_composer_fail.png")
        print("  [FAIL] Could not open post composer")
        return False

    # ── Step 2: Click "+" in composer toolbar to reveal document option ──────
    print("  Clicking '+' to expand composer toolbar...")
    # The composer has: 📷 media | 📅 event | 🎉 celebration | + more
    # "Add a document" is behind the "+" button
    plus_clicked = False
    try:
        # The "+" button is the last icon in the composer footer
        plus = page.locator('.share-creation-state__footer button').last
        if plus.is_visible(timeout=3000):
            plus.click()
            settle(page, 2000)
            plus_clicked = True
            print("  Clicked '+' (last footer button)")
    except Exception:
        pass

    if not plus_clicked:
        # Try by SVG content or aria-label
        for sel in (
            'button[aria-label*="more" i]:visible',
            'button:has(svg[data-test-icon="plus-medium"])',
        ):
            try:
                btn = page.locator(sel).first
                if btn.is_visible(timeout=1500):
                    btn.click()
                    settle(page, 2000)
                    plus_clicked = True
                    print(f"  Clicked '+': {sel}")
                    break
            except Exception:
                pass

    # Now find "Add a document" in the dropdown/menu
    print("  Looking for 'Add a document' option...")
    doc_clicked = False
    for sel in (
        'button:has-text("Add a document")',
        'div[aria-label*="document" i]',
        'li:has-text("Add a document")',
        'button[aria-label*="document" i]',
        'span:has-text("Add a document")',
    ):
        try:
            el = page.locator(sel).first
            if el.is_visible(timeout=3000):
                el.click()
                settle(page, 2000)
                doc_clicked = True
                print(f"  Clicked document option: {sel}")
                break
        except Exception:
            pass

    if not doc_clicked:
        page.screenshot(path="/tmp/li_doc_btn_fail.png")
        # Print what's in the expanded menu
        menu_items = page.evaluate("""() => {
            return [...document.querySelectorAll('li, [role="menuitem"], [role="option"]')]
                .filter(e => e.offsetParent !== null)
                .map(e => e.textContent.trim().substring(0,60))
                .filter(t => t.length > 0);
        }""")
        print(f"  Menu items visible: {menu_items[:10]}")
        print("  [WARN] Document option not found — falling back to text-only")
        return _post_text_only(page, text)

    # ── Step 4: Upload the PDF ────────────────────────────────────────────────
    print(f"  Uploading {os.path.basename(pdf_path)}...")
    page.screenshot(path="/tmp/li_before_upload.png")

    # Playwright intercepts the native file picker
    try:
        with page.expect_file_chooser(timeout=6000) as fc_info:
            # The file chooser may open automatically after clicking the doc button
            # or we may need to click a trigger inside the upload dialog
            for sel in (
                'button:has-text("Choose file")',
                'button:has-text("Upload")',
                'label[for*="file"]',
                'input[type="file"]',
            ):
                try:
                    el = page.locator(sel).first
                    if el.is_visible(timeout=1000):
                        el.click()
                        break
                except Exception:
                    pass
        fc_info.value.set_files(pdf_path)
        print(f"  File set via file chooser")
    except Exception:
        # Fallback: set directly on hidden input
        try:
            fi = page.locator('input[type="file"]').first
            fi.wait_for(state="attached", timeout=6000)
            fi.set_input_files(pdf_path)
            print("  File set via direct input")
        except Exception as e2:
            print(f"  [FAIL] Could not upload file: {e2}")
            page.screenshot(path="/tmp/li_upload_fail.png")
            return _post_text_only(page, text)

    # Wait for LinkedIn to process/render the document preview
    print("  Waiting for document processing...")
    settle(page, 8000)
    page.screenshot(path="/tmp/li_after_upload.png")

    # LinkedIn document flow: after upload there's a "Next" step (add title)
    for next_sel in (
        'button:has-text("Next")',
        'button:has-text("Done")',
        'button:has-text("Save")',
        'button[aria-label="Next"]',
    ):
        try:
            btn = page.locator(next_sel).first
            if btn.is_visible(timeout=4000):
                btn.click()
                settle(page, 3000)
                print(f"  Clicked post-upload step: '{next_sel}'")
                break
        except Exception:
            pass

    # ── Step 4: Type the post text ────────────────────────────────────────────
    print("  Typing post text...")
    editor_sels = [
        '.ql-editor[contenteditable="true"]',
        'div[role="textbox"][contenteditable="true"]',
        'div.editor-content div[contenteditable="true"]',
        '[data-placeholder*="talk about"]',
        '[data-placeholder*="What"]',
    ]
    typed = False
    for sel in editor_sels:
        try:
            ed = page.locator(sel).first
            if ed.is_visible(timeout=4000):
                ed.click()
                time.sleep(0.4)
                page.keyboard.type(text, delay=12)
                typed = True
                print(f"  Typed into: {sel}")
                break
        except Exception:
            pass

    if not typed:
        print("  [WARN] Could not find text editor — posting document without text")

    settle(page, 2000)

    # ── Step 5: Click Post ────────────────────────────────────────────────────
    print("  Clicking 'Post'...")
    for sel in (
        'button.share-actions__primary-action:has-text("Post")',
        'button[aria-label="Post"]:not([disabled])',
        '.share-actions__primary-action',
        'button:has-text("Post"):not([disabled])',
    ):
        try:
            btn = page.locator(sel).first
            if btn.is_visible(timeout=3000):
                btn.evaluate("el => el.scrollIntoView({block:'center'})")
                time.sleep(0.5)
                btn.click()
                settle(page, 6000)
                print(f"  Posted via: {sel}")
                return True
        except Exception:
            pass

    page.screenshot(path="/tmp/li_post_btn_fail.png")
    print("  [FAIL] Post button not found")
    return False


def _post_text_only(page, text: str) -> bool:
    """Fallback: post text without document."""
    print("  [FALLBACK] Posting text only...")
    editor_sels = [
        '.ql-editor[contenteditable="true"]',
        'div[role="textbox"][contenteditable="true"]',
    ]
    for sel in editor_sels:
        try:
            ed = page.locator(sel).first
            if ed.is_visible(timeout=3000):
                ed.click()
                time.sleep(0.3)
                page.keyboard.type(text, delay=12)
                break
        except Exception:
            pass
    settle(page, 1500)
    for sel in ('button.share-actions__primary-action:has-text("Post")', '.share-actions__primary-action'):
        try:
            btn = page.locator(sel).first
            if btn.is_visible(timeout=3000):
                btn.click()
                settle(page, 5000)
                return True
        except Exception:
            pass
    return False


def inject_li_cookies(ctx):
    try:
        chrome_cookies = bc3.chrome(domain_name="linkedin.com")
        pw_cookies = []
        for c in chrome_cookies:
            domain = c.domain if c.domain.startswith(".") else "." + c.domain
            try:
                pw_cookies.append({
                    "name": c.name, "value": c.value,
                    "domain": domain, "path": c.path or "/",
                    "secure": bool(c.secure), "httpOnly": False,
                    "sameSite": "None",
                })
            except Exception:
                pass
        if pw_cookies:
            ctx.add_cookies(pw_cookies)
            print(f"[INFO] Injected {len(pw_cookies)} LinkedIn cookies")
    except Exception as e:
        print(f"[WARN] Cookie injection failed: {e}")


def main():
    tmp = tempfile.mkdtemp(prefix="li_post_")

    with sync_playwright() as pw:
        try:
            ctx = pw.chromium.launch_persistent_context(
                user_data_dir=tmp, headless=False, channel="chrome",
                viewport={"width": 1280, "height": 900},
                args=["--disable-blink-features=AutomationControlled"],
            )
        except Exception:
            ctx = pw.chromium.launch_persistent_context(
                user_data_dir=tmp, headless=False,
                viewport={"width": 1280, "height": 900},
                args=["--disable-blink-features=AutomationControlled"],
            )

        page = ctx.new_page()
        Stealth().apply_stealth_sync(page)
        inject_li_cookies(ctx)

        # Verify login
        print("\n[INFO] Verifying LinkedIn session...")
        page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded", timeout=30000)
        settle(page, 4000)
        if "login" in page.url or "authwall" in page.url:
            print("[ERROR] Not logged in to LinkedIn")
            ctx.close()
            sys.exit(1)
        print(f"[INFO] Logged in — {page.url}")

        # ── Step 1: Delete the 3 old posts ───────────────────────────────────
        delete_recent_posts(page, count=3)

        # ── Step 2: Re-post with PDF attachments ─────────────────────────────
        results = []
        for i, (text, pdf_file) in enumerate(POSTS, 1):
            pdf_path = os.path.join(CERT_DIR, pdf_file)
            print(f"\n{'='*60}")
            print(f"  POST {i}/{len(POSTS)}: {pdf_file}")
            print(f"  Preview: {text[:80].strip()}...")
            print(f"{'='*60}")

            if not os.path.isfile(pdf_path):
                print(f"  [ERROR] PDF not found: {pdf_path}")
                results.append(False)
                continue

            success = post_with_document(page, text, pdf_path)
            results.append(success)
            status = "[SUCCESS]" if success else "[FAIL]"
            print(f"  {status} Post {i}")

            if i < len(POSTS):
                print(f"  Waiting 10s before next post...")
                time.sleep(10)

        print(f"\n{'='*60}")
        print(f"RESULTS: {sum(results)}/{len(results)} posts published")
        for i, r in enumerate(results, 1):
            print(f"  Post {i}: {'✓ published' if r else '✗ failed'}")
        print(f"{'='*60}")

        time.sleep(5)
        ctx.close()


if __name__ == "__main__":
    main()
