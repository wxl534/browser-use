"""
loc_scraper.py

A Scrapling-based scraper that searches https://www.loc.gov/ for a query (default: "china buddhist"),
iterates search results, opens detail pages, extracts the best image URL, downloads images into an
ImagesCache directory and writes per-image records to image_record.jsonl and temple_photo_info.md
in browseruse_agent_data/ so they match the project task.md expectations.

Notes:
- Requires Scrapling with fetchers (stealthy fetcher / Playwright). Install in your venv:
    pip install -e .[fetchers]
  and ensure playwright browsers are installed (scrapling install or python -m playwright install chromium)
- This script is conservative: single-threaded, small waits, 1 retry on failure. Adjust args as needed.

Usage:
  python loc_scraper.py --term "china buddhist" --max-pages 3

"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
from pathlib import Path
from typing import List, Optional, Tuple

from scrapling.fetchers import StealthyFetcher

# Defaults and paths
BASE_URL = "https://www.loc.gov/"
DEFAULT_TERM = "china buddhist"
# Save images and metadata under the project loc folder per user request
IMAGES_CACHE_DIR = Path(r"D:\desktop\1\browser-use-main\loc\ImagesCache")  # absolute Windows path
BROWSERUSE_DATA_DIR = Path(r"D:\desktop\1\browser-use-main\loc\browseruse_agent_data")
IMAGE_RECORD_FILE = BROWSERUSE_DATA_DIR / "image_record.jsonl"
INFO_MD_FILE = BROWSERUSE_DATA_DIR / "temple_photo_info.md"

# Tunables
WAIT_BETWEEN_REQUESTS = 1.0  # seconds
MAX_RETRIES = 1
IMAGES_PER_ITEM = 1

# Persistent Playwright user data dir for manual CF solve
USER_DATA_DIR = str(Path.cwd() / "playwright_user_data_loc")
# Optional storage_state JSON produced by fetch_cf_cookie.py (方案C)
STORAGE_STATE = os.environ.get('IDP_STORAGE_STATE', '')
# Load cookies/user-agent from storage_state if present (to inject cf_clearance)
STORED_COOKIES = None
STORED_USER_AGENT = None
if STORAGE_STATE:
    try:
        ss_path = Path(STORAGE_STATE)
        if ss_path.exists():
            with ss_path.open('r', encoding='utf-8') as _f:
                _data = json.load(_f)
            STORED_COOKIES = _data.get('cookies') or None
            STORED_USER_AGENT = (_data.get('_meta') or {}).get('user_agent')
    except Exception:
        STORED_COOKIES = None
        STORED_USER_AGENT = None


def ensure_dirs():
    IMAGES_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    BROWSERUSE_DATA_DIR.mkdir(parents=True, exist_ok=True)


def append_jsonl(record: dict):
    with IMAGE_RECORD_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def rebuild_info_md():
    # Read JSONL and write a simple markdown summary matching task.md expectations.
    if not IMAGE_RECORD_FILE.exists():
        INFO_MD_FILE.write_text("# temple_photo_info\n\n(no records yet)\n", encoding="utf-8")
        return

    lines = [json.loads(line) for line in IMAGE_RECORD_FILE.read_text(encoding="utf-8").splitlines() if line.strip()]
    md_lines = ["# temple_photo_info", ""]
    for r in lines:
        seq = r.get("sequence")
        file_name = r.get("file_name")
        title = r.get("title")
        collection_title = r.get("collection_title")
        page_url = r.get("page_url")
        image_url = r.get("image_url")
        summary = r.get("summary", "")
        md_lines.append(f"- {seq:03d} | {file_name} | {title} | {collection_title} | [{page_url}]({page_url}) | {image_url}")
        if summary:
            md_lines.append(f"  - {summary}")
    INFO_MD_FILE.write_text("\n".join(md_lines), encoding="utf-8")


def compute_hash(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def save_image_bytes(seq: int, prefix: str, title_for_name: str, img_bytes: bytes, ext: str = "jpg") -> Tuple[str, str]:
    # final file name: prefix_{seq:03d}_{shorttitle}_{hash[:8]}.ext
    h = compute_hash(img_bytes)
    safe_title = "".join(c for c in title_for_name if c.isalnum() or c in (" ", "-", "_")).strip().replace(" ", "_")[:60]
    base = f"{prefix}_{seq:03d}_{safe_title}_{h[:8]}"
    fname = f"{base}.{ext}"
    path = IMAGES_CACHE_DIR / fname
    with path.open("wb") as f:
        f.write(img_bytes)
    return fname, h


def pick_best_image_from_candidates(candidates: List[str]) -> Optional[str]:
    # Heuristic: prefer URLs containing "/full/" or largest-looking variants, else first
    if not candidates:
        return None
    for c in candidates:
        if "/full/" in c or "/!" in c or "=orig" in c:
            return c
    return candidates[0]


def extract_candidates_from_response(resp) -> List[str]:
    # resp is a Scrapling Response / Selector. Use common patterns to find image URLs.
    candidates: List[str] = []
    try:
        # og:image
        og = resp.css("meta[property='og:image']::attr(content)")
        if og:
            candidates.extend([str(x) for x in og])
    except Exception:
        pass

    try:
        # images in img tags
        imgs = resp.css("img")
        for img in imgs:
            src = img.attrib.get("src") or img.attrib.get("data-src")
            if src:
                candidates.append(resp.urljoin(src))
    except Exception:
        pass

    try:
        # links to manifests or iiif
        links = resp.css("a::attr(href)")
        for l in links:
            ls = str(l)
            if "manifest" in ls or "iiif" in ls:
                candidates.append(resp.urljoin(ls))
    except Exception:
        pass

    # Deduplicate preserving order
    seen = set()
    out = []
    for c in candidates:
        if not c:
            continue
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out


def download_bytes_from_url(url: str, timeout_ms: int = 30000) -> Optional[bytes]:
    # Use StealthyFetcher to GET the image; fall back to plain requests if necessary
    try:
        r = StealthyFetcher.fetch(url, headless=True, disable_resources=False, network_idle=False, timeout=timeout_ms, load_dom=False, wait=1000, real_chrome=True, user_data_dir=USER_DATA_DIR, cookies=STORED_COOKIES, useragent=STORED_USER_AGENT)
        if getattr(r, "status", 0) in (200, 206):
            body = r.body if hasattr(r, "body") else r.content
            if isinstance(body, (bytes, bytearray)) and len(body) > 100:
                return bytes(body)
    except Exception:
        pass
    # Try simple requests as fallback
    try:
        import requests

        rr = requests.get(url, timeout=10)
        if rr.status_code == 200 and len(rr.content) > 100:
            return rr.content
    except Exception:
        pass
    return None


def is_already_saved(page_url: str = None, content_hash: str = None) -> bool:
    if not IMAGE_RECORD_FILE.exists():
        return False
    try:
        for line in IMAGE_RECORD_FILE.open(encoding='utf-8'):
            try:
                rec = json.loads(line)
            except Exception:
                continue
            if page_url and rec.get('page_url') == page_url:
                return True
            if content_hash and rec.get('content_hash') == content_hash:
                return True
    except Exception:
        return False
    return False


def run_search_and_download(term: str, max_pages: int = 3, target_total: int = 50, prefix: str = "temple"):
    ensure_dirs()
    seq = sum(1 for _ in IMAGE_RECORD_FILE.open(encoding="utf-8")) + 1 if IMAGE_RECORD_FILE.exists() else 1
    downloaded = 0

    # Prepare page_action: perform search on homepage
    def page_action_do_search(page):
        # This function runs in Playwright context provided by Scrapling
        try:
            # Try common search input selectors
            selectors = ["input[name=q]", "input[type=search]", "input#search", "input[aria-label='Search']"]
            found = None
            for s in selectors:
                try:
                    el = page.locator(s)
                    if el.count() > 0:
                        found = el
                        break
                except Exception:
                    continue
            if not found:
                # fallback: focus first input
                try:
                    page.locator("input").first.fill(term)
                    page.keyboard.press("Enter")
                    return
                except Exception:
                    return
            found.fill(term)
            # try pressing Enter
            page.keyboard.press("Enter")
        except Exception:
            return

    # Open initial search and then walk pages
    # If we have storage_state cookies (方案C), directly fetch the search URL to avoid homepage search automation.
    if STORED_COOKIES:
        search_url = f"{BASE_URL}search/?q={term.replace(' ', '+')}&new=true"
        print(f"Fetching search URL with injected cookies: {search_url}")
        response = StealthyFetcher.fetch(search_url, headless=True, network_idle=True, timeout=120000, wait=3000, solve_cloudflare=True, real_chrome=True, user_data_dir=USER_DATA_DIR, cookies=STORED_COOKIES, useragent=STORED_USER_AGENT)
    else:
        # We'll reuse the initial session to avoid extra overhead
        response = StealthyFetcher.fetch(BASE_URL, headless=True, network_idle=True, timeout=120000, wait=3000, page_action=page_action_do_search, solve_cloudflare=True, real_chrome=True, user_data_dir=USER_DATA_DIR, cookies=STORED_COOKIES, useragent=STORED_USER_AGENT)
    # For safety, allow a small sleep to let JS run
    time.sleep(1)

    page_num = 1
    while page_num <= max_pages and downloaded < target_total:
        print(f"Processing results page {page_num}")
        # Extract results: try several loc.gov patterns and report matches
        try:
            sel_candidates = [
                "ul.search-results li.item",
                "ul.search-results li",
                "li.item",
                "article",
                ".search-results li",
                ".search-results",
                ".item",
                ".result",
                ".search-result",
                "div#results li",
            ]
            items = []
            for s in sel_candidates:
                try:
                    found = response.css(s)
                    if found and len(found) > 0:
                        print(f"Selector '{s}' matched {len(found)} items")
                        items = found
                        break
                except Exception:
                    continue
        except Exception:
            items = []

        if not items:
            # As a fallback, try to find /item/ links directly and count them
            try:
                anchors = [str(x) for x in response.css('a::attr(href)')]
                item_links = [a for a in anchors if '/item/' in a]
                print(f"No item-selector matches; found {len(item_links)} '/item/' links via anchor scan")
            except Exception:
                print("No items found on this page; stopping.")
                break
            if not item_links:
                print("No items found on this page; stopping.")
                break
            # Create a pseudo-list of hrefs to iterate
            items = []
            for href in item_links:
                # create a tiny object with methods used later: css and get_all_text
                class AnchorProxy:
                    def __init__(self, h):
                        self._h = h
                    def css(self, q):
                        if q == 'a::attr(href)':
                            return [self._h]
                        return []
                    def get_all_text(self):
                        return ''
                items.append(AnchorProxy(href))

        if not items:
            print("No items found on this page; stopping.")
            break

        first_debug_done = False
        for item in items:
            # Print debug info for the first matched item to diagnose why images aren't being found
            if not first_debug_done:
                first_debug_done = True
                try:
                    hrefs = [str(x) for x in item.css("a::attr(href)")]
                    print(f"First item hrefs (sample up to 5): {hrefs[:5]}")
                    title_el = item.css("a, h3, h2, .title")
                    title_text = str(title_el[0].get_all_text().strip()) if title_el else ""
                    print(f"Title text: '{title_text}'")
                    detail_url = hrefs[0] if hrefs else None
                    if detail_url:
                        detail_url = response.urljoin(detail_url)
                        print(f"Detail URL: {detail_url}")
                        try:
                            detail_resp = StealthyFetcher.fetch(detail_url, headless=True, network_idle=True, timeout=120000, wait=3000, solve_cloudflare=True, real_chrome=True, user_data_dir=USER_DATA_DIR, cookies=STORED_COOKIES, useragent=STORED_USER_AGENT)
                            if detail_resp:
                                cands = extract_candidates_from_response(detail_resp)
                                print(f"Candidates from detail page (first 10): {cands[:10]}")
                                og = detail_resp.css("meta[property='og:image']::attr(content)")
                                print(f"og:image meta: {[str(x) for x in og]}")

                                # Diagnostic: try downloading up to 3 candidates via StealthyFetcher and print status/size
                                for idx, cand in enumerate(cands[:3]):
                                    try:
                                        print(f"Trying StealthyFetcher for candidate {idx}: {cand}")
                                        rimg = StealthyFetcher.fetch(cand, headless=True, load_dom=False, disable_resources=True, network_idle=False, timeout=60000, wait=500, real_chrome=True, user_data_dir=USER_DATA_DIR, cookies=STORED_COOKIES, useragent=STORED_USER_AGENT)
                                        status = getattr(rimg, 'status', None)
                                        body = None
                                        if hasattr(rimg, 'body'):
                                            body = rimg.body
                                        elif hasattr(rimg, 'content'):
                                            body = rimg.content
                                        # normalize body to bytes
                                        body_bytes = None
                                        try:
                                            if body is None:
                                                body_bytes = None
                                            elif isinstance(body, (bytes, bytearray)):
                                                body_bytes = bytes(body)
                                            else:
                                                # memoryview or other buffer
                                                body_bytes = bytes(body)
                                        except Exception:
                                            body_bytes = None
                                        length = len(body_bytes) if body_bytes else 0
                                        print(f"StealthyFetcher returned status={status} len={length}")

                                        # Quick-save fix: if image bytes look valid, save immediately and record
                                        if length and length > 1000:
                                            try:
                                                # skip if page_url or content_hash already saved
                                                if is_already_saved(page_url=detail_url) or is_already_saved(content_hash=hashlib.sha256(body_bytes).hexdigest()):
                                                    print('Duplicate detected: skipping save for this candidate')
                                                else:
                                                    # attempt to derive extension
                                                    ext = 'jpg'
                                                    if str(cand).lower().endswith('.png'):
                                                        ext = 'png'
                                                    fname, content_hash = save_image_bytes(seq, prefix, title_text or term, body_bytes, ext=ext)
                                                    record = {
                                                        'sequence': seq,
                                                        'file_name': str(fname),
                                                        'title': f"{term}_{seq:03d}_{title_text[:80]}_图1",
                                                        'collection_title': title_text,
                                                        'page_url': detail_url,
                                                        'image_url': cand,
                                                        'content_hash': content_hash,
                                                        'summary': '',
                                                        'status': 'downloaded',
                                                    }
                                                    append_jsonl(record)
                                                    rebuild_info_md()
                                                    print(f"Quick-saved {fname} from candidate {idx} ({cand}) len={length}")
                                                    seq += 1
                                                    downloaded += 1
                                                    # stop after one quick-save
                                                    break
                                            except Exception as e:
                                                print('Quick-save failed', e)
                                    except Exception as e:
                                        print('StealthyFetcher download failed for candidate', idx, e)
                        except Exception as e:
                            print('Detail fetch during debug failed', e)
                except Exception as e:
                    print('Debugging first item failed', e)
            if downloaded >= target_total:
                break
            try:
                detail = item.css("a::attr(href)")
                detail_url = str(detail[0]) if detail else None
                title_el = item.css("a, h3, h2, .title")
                title_text = str(title_el[0].get_all_text().strip()) if title_el else ""
                if detail_url:
                    detail_url = response.urljoin(detail_url)
                else:
                    continue
            except Exception:
                continue

                # Skip duplicates based on existing JSONL
                # A simple check: does image_record already contain this page_url?
                already = False
                if IMAGE_RECORD_FILE.exists():
                    for line in IMAGE_RECORD_FILE.open(encoding="utf-8"):
                        try:
                            rec = json.loads(line)
                            if rec.get("page_url") == detail_url:
                                already = True
                                break
                        except Exception:
                            continue
                if already:
                    print(f"Skipping already processed: {detail_url}")
                    continue

                # Fetch detail page
                print(f"Fetching detail: {detail_url}")
                detail_resp = None
                try:
                    detail_resp = StealthyFetcher.fetch(detail_url, headless=True, network_idle=True, timeout=120000, wait=3000, solve_cloudflare=True, real_chrome=True, user_data_dir=USER_DATA_DIR, cookies=STORED_COOKIES, useragent=STORED_USER_AGENT)
                except Exception as e:
                    print("Detail fetch failed", e)
                    detail_resp = None

                if not detail_resp:
                    print("Detail resp empty, skipping")
                    continue

                # Extract candidate image URLs
                candidates = extract_candidates_from_response(detail_resp)
                chosen = pick_best_image_from_candidates(candidates)

                # If manifest/iiif candidate found, try to resolve to image URL (basic)
                if chosen and chosen.endswith(".json") and "manifest" in chosen:
                    # attempt to parse manifest
                    try:
                        man = StealthyFetcher.fetch(chosen, headless=True, load_dom=False, timeout=60000, wait=3000, real_chrome=True, user_data_dir=USER_DATA_DIR, cookies=STORED_COOKIES, useragent=STORED_USER_AGENT)
                        j = json.loads(man.body.decode("utf-8")) if hasattr(man, "body") else {}
                        # naive extraction for IIIF image
                        if isinstance(j, dict):
                            # look for sequences->canvases->images->resource->@id
                            img = None
                            seqs = j.get("sequences") or j.get("items")
                            if seqs and isinstance(seqs, list):
                                canvases = seqs[0].get("canvases") or seqs[0].get("items")
                                if canvases and isinstance(canvases, list):
                                    for c in canvases:
                                        imgs = c.get("images") or c.get("items")
                                        if imgs and isinstance(imgs, list):
                                            first = imgs[0]
                                            res = first.get("resource") or first.get("body")
                                            if isinstance(res, dict):
                                                img = res.get("@id") or res.get("id")
                                                break
                            if img:
                                chosen = img
                    except Exception:
                        pass

                # If still no chosen candidate, try to extract from detail_resp again
                if not chosen:
                    candidates = extract_candidates_from_response(detail_resp)
                    chosen = pick_best_image_from_candidates(candidates)

                if not chosen:
                    print("No image candidate found, skipping")
                    continue

                # Try downloading the chosen image
                img_bytes = download_bytes_from_url(chosen)
                if not img_bytes:
                    # Try other candidates
                    success = False
                    for c in candidates:
                        img_bytes = download_bytes_from_url(c)
                        if img_bytes:
                            chosen = c
                            success = True
                            break
                    if not success:
                        print("All image downloads failed for this item; skipping")
                        continue

                # Save image and write record
                fname, content_hash = save_image_bytes(seq, prefix, title_text or term, img_bytes, ext="jpg")
                record = {
                    "sequence": seq,
                    "file_name": str(fname),
                    "title": f"{term}_{seq:03d}_{title_text[:80]}_图1",
                    "collection_title": title_text,
                    "page_url": detail_url,
                    "image_url": chosen,
                    "content_hash": content_hash,
                    "summary": "",
                }
                append_jsonl(record)
                rebuild_info_md()

                print(f"Saved {fname} from {chosen}")
                seq += 1
                downloaded += 1

                time.sleep(WAIT_BETWEEN_REQUESTS)

            # Move to next page: try to find "next" link in response
            # Note: because we used the same response object for the search results, we need to fetch the next page explicitly
            page_num += 1
            if page_num <= max_pages and downloaded < target_total:
                # try building the paged search URL using loc.gov search params, fallback to clicking next link
                # Simple approach: find a link with rel=next
                try:
                    next_links = response.css("a[rel=next]::attr(href)")
                    if next_links:
                        next_url = str(next_links[0])
                        print(f"Navigating to next results: {next_url}")
                        response = StealthyFetcher.fetch(response.urljoin(next_url), headless=True, network_idle=True, timeout=120000, wait=3000, solve_cloudflare=True, real_chrome=True, user_data_dir=USER_DATA_DIR, cookies=STORED_COOKIES, useragent=STORED_USER_AGENT)
                        time.sleep(1)
                        continue
                except Exception:
                    pass
                # Otherwise attempt to simulate pressing next by adding page param (loc.gov uses ?q=...&sp=1 or &page=)
                try:
                    # fallback: build a search url with page parameter
                    search_params = f"?q={term.replace(' ', '+')}&st=gallery&page={page_num}"
                    next_url = BASE_URL + "search/" + search_params
                    print(f"Attempting fallback paged URL: {next_url}")
                    response = StealthyFetcher.fetch(next_url, headless=True, network_idle=True, timeout=120000, wait=3000, solve_cloudflare=True, real_chrome=True, user_data_dir=USER_DATA_DIR, cookies=STORED_COOKIES, useragent=STORED_USER_AGENT)
                    time.sleep(1)
                except Exception:
                    print("Failed to navigate to next page; stopping")
                    break

    print(f"Finished. Downloaded {downloaded} images.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--term", default=DEFAULT_TERM, help="Search term")
    parser.add_argument("--max-pages", type=int, default=3, help="Max result pages to process")
    parser.add_argument("--target-total", type=int, default=50, help="Stop after this many downloaded images")
    parser.add_argument("--prefix", default="temple", help="File name prefix")
    args = parser.parse_args()
    run_search_and_download(args.term, max_pages=args.max_pages, target_total=args.target_total, prefix=args.prefix)
