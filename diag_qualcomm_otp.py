"""
Diagnostic: inspect Qualcomm Eightfold OTP page DOM to find the correct input selectors.
Run: python3 -u diag_qualcomm_otp.py
"""
import os, sys, time, json
from pathlib import Path
from playwright.sync_api import sync_playwright
try:
    from playwright_stealth import Stealth
    HAS_STEALTH = True
except ImportError:
    HAS_STEALTH = False
import dotenv
dotenv.load_dotenv(Path(__file__).parent / ".env", override=False)

PROFILE_DIR = Path.home() / ".ef_profiles" / "diag_qualcomm_fresh"
PROFILE_DIR.mkdir(parents=True, exist_ok=True)

EMAIL = os.environ.get("CANDIDATE_EMAIL", "")
JOB_URL = "https://careers.qualcomm.com/careers/job/446720103301"

def dump_inputs(page, label=""):
    inputs = page.evaluate("""() => {
        var inputs = document.querySelectorAll('input');
        var result = [];
        for (var i = 0; i < inputs.length; i++) {
            var inp = inputs[i];
            result.push({
                type: inp.type,
                id: inp.id,
                name: inp.name,
                placeholder: inp.placeholder,
                maxlength: inp.maxLength,
                autocomplete: inp.autocomplete,
                className: inp.className.substring(0, 80),
                visible: inp.offsetWidth > 0 && inp.offsetHeight > 0,
                ariaLabel: inp.getAttribute('aria-label') || '',
                dataTestId: inp.getAttribute('data-testid') || '',
            });
        }
        return result;
    }""")
    print(f"\n=== INPUTS [{label}] ===")
    for inp in inputs:
        if inp.get("visible"):
            print(json.dumps(inp))

    # Also check for OTP-like divs
    otp_divs = page.evaluate("""() => {
        var all = document.querySelectorAll('[class*="otp"], [class*="code"], [class*="verify"], [id*="otp"], [id*="code"], [id*="verify"]');
        var result = [];
        for (var i = 0; i < all.length; i++) {
            var el = all[i];
            result.push({
                tag: el.tagName,
                id: el.id,
                className: el.className.substring(0, 80),
                text: el.innerText.substring(0, 50),
                visible: el.offsetWidth > 0 && el.offsetHeight > 0,
            });
        }
        return result;
    }""")
    if otp_divs:
        print(f"\n=== OTP-RELATED ELEMENTS [{label}] ===")
        for d in otp_divs:
            print(json.dumps(d))


def main():
    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            str(PROFILE_DIR),
            channel="chrome",
            headless=False,
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        if HAS_STEALTH:
            Stealth().apply_stealth_sync(page)

        print(f"[diag] Navigating to job URL: {JOB_URL}")
        page.goto(JOB_URL, timeout=30000)
        time.sleep(4)
        print("[diag] URL:", page.url)

        # Look for Apply button
        apply_selectors = [
            'button:has-text("Apply")',
            'a:has-text("Apply Now")',
            'a:has-text("Apply")',
            '[data-ph-at-id="apply-btn"]',
            '.apply-button',
        ]
        for sel in apply_selectors:
            try:
                el = page.locator(sel).first
                if el.is_visible(timeout=3000):
                    print(f"[diag] Clicking Apply button: {sel}")
                    el.click()
                    time.sleep(3)
                    break
            except Exception:
                pass

        print("[diag] URL after Apply click:", page.url)
        dump_inputs(page, "after-apply-click")

        # Check for sign-in modal
        sign_in_detected = False
        for chk in ['input[type="email"]', 'input[name="username"]', 'h2:has-text("Sign in")',
                     ':text("Sign in")', 'button:has-text("Continue")']:
            try:
                el = page.locator(chk).first
                if el.is_visible(timeout=3000):
                    print(f"[diag] Sign-in indicator found: {chk}")
                    sign_in_detected = True
                    break
            except Exception:
                pass

        if sign_in_detected:
            # Fill email
            for sel in ['input[type="email"]', 'input[name="username"]', 'input[name="email"]']:
                try:
                    el = page.locator(sel).first
                    if el.is_visible(timeout=3000):
                        print(f"[diag] Filling email in: {sel}")
                        el.fill(EMAIL)
                        time.sleep(0.5)
                        break
                except Exception:
                    pass

            # Click Continue
            for btn_sel in ['button:has-text("Continue")', 'button[type="submit"]']:
                try:
                    btn = page.locator(btn_sel).first
                    if btn.is_visible(timeout=3000):
                        print(f"[diag] Clicking: {btn_sel}")
                        btn.click()
                        break
                except Exception:
                    pass

            print("[diag] Waiting 15s for OTP page...")
            print(">>> CHECK YOUR EMAIL for OTP code! <<<")
            time.sleep(15)

            print("[diag] URL after Continue:", page.url)
            dump_inputs(page, "after-continue")

            # Dump full HTML for OTP page
            html = page.content()
            print("\n=== PAGE HTML (8000 chars) ===")
            print(html[:8000])

        else:
            print("[diag] No sign-in modal found. Dumping current page...")
            html = page.content()
            print(html[:6000])

        print("\n[diag] Done. Browser stays open for 60s for manual inspection.")
        time.sleep(60)
        ctx.close()

if __name__ == "__main__":
    main()
