"""
Playwright helper: open a page, pause for manual Cloudflare/Turnstile solve, then save storage_state JSON.
Usage:
  python -m pip install playwright
  python -m playwright install chromium
  python scripts\save_storage_state_playwright.py "https://idp.bl.uk/" cf_storage.json

After solving and the script saving cf_storage.json, set environment variable:
  set IDP_STORAGE_STATE=C:\path\to\cf_storage.json
or configure your runtime to load that storage_state so browser-use reuses cookies (cf_clearance).

Note: cf_clearance is bound to the exit IP and may expire; re-run if downloads start getting 403.
"""
from playwright.sync_api import sync_playwright
import sys
import json

def main():
    if len(sys.argv) < 3:
        print("Usage: python scripts\\save_storage_state_playwright.py <url> <out_path>")
        return 1
    url = sys.argv[1]
    out_path = sys.argv[2]
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context()
        page = context.new_page()
        print(f"Opening {url} — please solve any Cloudflare/Turnstile challenge in the browser window.")
        page.goto(url)
        input("After you have completed the challenge, press Enter here to save storage_state...\n")
        context.storage_state(path=out_path)
        print(f"Saved storage_state → {out_path}")
        browser.close()
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
