"""
manual_solve.py (file-signal mode)

Opens a visible persistent Playwright Chromium context using a dedicated user_data_dir so you
can manually solve Cloudflare/Turnstile in the opened browser. The script will either detect
that a cf_clearance-like cookie exists for the site OR wait for a filesystem signal file
(named 'solved.signal' inside the user_data_dir) created by the user. When either is detected
it closes the browser and exits, leaving cookies persisted for loc_scraper.py to reuse.

Usage (recommended):
  1. Run this script in the same environment that will run loc_scraper.py:
     python manual_solve.py
  2. A visible browser window will open. Manually solve any Cloudflare/Turnstile challenge there.
  3. After solving, EITHER create the file:
       playground/playwright_user_data_loc/solved.signal
     or let the script auto-detect the cf_clearance cookie.
  4. The script will close the browser and exit.

This removes the need to press Enter in the terminal and enables automation driven by a file signal.
"""
from pathlib import Path
import time
from playwright.sync_api import sync_playwright

USER_DATA_DIR = Path.cwd() / "playwright_user_data_loc"
USER_DATA_DIR.mkdir(exist_ok=True)
SIGNAL_FILE = USER_DATA_DIR / "solved.signal"

POLL_INTERVAL = 2.0  # seconds
TARGET_DOMAIN = "www.loc.gov"

print(f"Opening persistent browser with user_data_dir={USER_DATA_DIR}")
with sync_playwright() as p:
    context = p.chromium.launch_persistent_context(user_data_dir=str(USER_DATA_DIR), headless=False)
    page = context.new_page()
    print("Navigating to https://www.loc.gov/ ...")
    page.goto('https://www.loc.gov/')
    print("Browser opened. Please solve any Cloudflare/Turnstile challenge in the opened window.")

    print(f"Waiting for either cookie detection or file signal: {SIGNAL_FILE}")
    try:
        while True:
            # 1) Check for signal file
            if SIGNAL_FILE.exists():
                print(f"Signal file {SIGNAL_FILE} detected. Closing browser and exiting.")
                break

            # 2) Check for cf cookie in context cookies
            try:
                cookies = context.cookies()
                for c in cookies:
                    name = c.get('name', '').lower()
                    domain = c.get('domain', '')
                    if ('cf_clearance' in name or name.startswith('cf_chl') or name.startswith('__cf_')) and TARGET_DOMAIN in domain:
                        print(f"Found Cloudflare cookie '{name}' for domain {domain}. Closing browser and exiting.")
                        raise KeyboardInterrupt
            except KeyboardInterrupt:
                break
            except Exception:
                # ignore transient read errors
                pass

            time.sleep(POLL_INTERVAL)
    finally:
        try:
            context.close()
        except Exception:
            pass

    print("Browser closed. Cookies/session data saved to user_data_dir.")
