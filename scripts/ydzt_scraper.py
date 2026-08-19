#!/usr/bin/env python3
"""
YDZT scraper
- Parses https://news-sv.aij.or.jp/da2/yachou/gallery_3_chuta2.htm (Shift_JIS)
- For target item numbers, visits each detail page, records text, extracts hidden image URLs
  (HTTP parsing first; fallback to Playwright to reveal URLs) and downloads images into
  per-title folders under a root directory.

Usage:
  python scripts\ydzt_scraper.py --root ./YDZT_downloads --targets "1-5,14-22,23-28,29,67-69,75"

Requires: requests, beautifulsoup4, playwright (pip install playwright) and `playwright install` for browsers
"""

import argparse
import json
import os
import re
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

# Playwright is optional fallback
try:
    from playwright.sync_api import sync_playwright
except Exception:
    sync_playwright = None


BASE_URL = "https://news-sv.aij.or.jp/da2/yachou/gallery_3_chuta2.htm"
ROOT_PAGE_BASE = "https://news-sv.aij.or.jp/da2/yachou/"
HEADERS = {"User-Agent": "YDZT-scraper/1.0 (+https://example.com)"}

IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".tif", ".tiff")


def parse_targets_spec(spec: str) -> set:
    parts = re.split(r"\s*,\s*", spec.strip())
    nums = set()
    for p in parts:
        if not p:
            continue
        if "-" in p:
            a, b = p.split("-", 1)
            try:
                a_i = int(a); b_i = int(b)
                for i in range(a_i, b_i + 1):
                    nums.add(i)
            except ValueError:
                continue
        else:
            try:
                nums.add(int(p))
            except ValueError:
                continue
    return nums


def safe_name(s: str) -> str:
    s = s.strip()
    s = re.sub(r"[\\/:*?\"<>|]+", "_", s)
    s = re.sub(r"\s+", "_", s)
    return s[:120]


def download_url_stream(url: str, dest: Path, timeout: int = 30, retries: int = 3) -> bool:
    for attempt in range(1, retries + 1):
        try:
            with requests.get(url, headers=HEADERS, stream=True, timeout=timeout) as r:
                r.raise_for_status()
                dest.parent.mkdir(parents=True, exist_ok=True)
                with open(dest, "wb") as f:
                    for chunk in r.iter_content(chunk_size=8192):
                        if chunk:
                            f.write(chunk)
            return True
        except Exception as e:
            print(f"  ⚠ download attempt {attempt} failed for {url}: {e}")
            time.sleep(1 + attempt)
    return False


def parse_index_page(url: str):
    r = requests.get(url, headers=HEADERS, timeout=30)
    r.encoding = 'shift_jis'
    soup = BeautifulSoup(r.text, 'html.parser')
    rows = soup.select('table tr')
    entries = {}
    for tr in rows:
        tds = tr.find_all('td')
        if len(tds) < 3:
            continue
        try:
            num_text = tds[0].get_text(strip=True)
            num = int(re.sub(r"[^0-9]", "", num_text))
        except Exception:
            continue
        title = tds[1].get_text(strip=True)
        a = tds[2].find('a')
        href = a['href'] if a and a.has_attr('href') else None
        if href:
            entries[num] = {
                'num': num,
                'title': title,
                'href': urljoin(url, href)
            }
    return entries


def extract_images_from_html(page_url: str, html: str):
    soup = BeautifulSoup(html, 'html.parser')
    imgs = []
    # collect <img src>, data-src, and anchors linking to images
    for img in soup.find_all('img'):
        src = img.get('src') or img.get('data-src')
        if not src:
            continue
        if any(src.lower().endswith(ext) for ext in IMAGE_EXTS):
            imgs.append(urljoin(page_url, src))
    for a in soup.find_all('a'):
        href = a.get('href')
        if href and any(href.lower().endswith(ext) for ext in IMAGE_EXTS):
            imgs.append(urljoin(page_url, href))
    # unique preserve order
    seen = set(); out = []
    for u in imgs:
        if u not in seen:
            seen.add(u); out.append(u)
    return out


def playwright_extract_image_urls(detail_url: str, headless: bool = True, timeout: int = 30) -> list:
    if sync_playwright is None:
        print("⚠ Playwright not installed; cannot run browser fallback")
        return []
    urls = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        context = browser.new_context()
        page = context.new_page()
        page.goto(detail_url, timeout=timeout*1000)
        # try quick extraction
        js_code = """() => {
            const out = [];
            document.querySelectorAll('img').forEach(i=>{if(i.src) out.push(i.src); if(i.dataset && i.dataset.src) out.push(i.dataset.src)});
            document.querySelectorAll('a').forEach(a=>{if(a.href && /\\.(jpg|jpeg|png|gif|webp|tif|tiff)(\?|$)/i.test(a.href)) out.push(a.href)});
            return out;
        }"""
        found = page.evaluate(js_code)
        for u in found:
            urls.append(urljoin(detail_url, u))
        # if none or few, try clicking thumbnail anchors
        if len(urls) < 1:
            # click up to 20 thumbnails
            thumbs = page.query_selector_all('a img')
            clicks = min(len(thumbs), 20)
            for idx in range(clicks):
                try:
                    el = thumbs[idx]
                    parent = el.evaluate_handle('e => e.closest("a") || e')
                    try:
                        parent.as_element().click(timeout=5000)
                    except Exception:
                        pass
                    time.sleep(0.5)
                except Exception:
                    pass
            # re-evaluate
            js_code2 = """() => {
                const out = [];
                document.querySelectorAll('img').forEach(i=>{if(i.src) out.push(i.src); if(i.dataset && i.dataset.src) out.push(i.dataset.src)});
                document.querySelectorAll('a').forEach(a=>{if(a.href && /\\.(jpg|jpeg|png|gif|webp|tif|tiff)(\?|$)/i.test(a.href)) out.push(a.href)});
                return out;
            }"""
            found2 = page.evaluate(js_code2)
            for u in found2:
                urls.append(urljoin(detail_url, u))
        # close
        context.close(); browser.close()
    # uniq
    out = []
    seen = set()
    for u in urls:
        if u and u not in seen:
            seen.add(u); out.append(u)
    return out


def run(args):
    targets = parse_targets_spec(args.targets)
    print(f"Targets parsed: {sorted(targets)}")
    entries = parse_index_page(BASE_URL)
    selected = {n: entries[n] for n in sorted(targets) if n in entries}
    if not selected:
        print("No matching entries found on index page for given targets")
        return

    root = Path(args.root).resolve()
    root.mkdir(parents=True, exist_ok=True)

    summary = []

    for num, info in selected.items():
        print(f"\n=== Processing {num}: {info.get('title')} -> {info.get('href')} ===")
        title = info.get('title') or f"item_{num}"
        safe_title = safe_name(title)
        folder = root / safe_title
        folder.mkdir(parents=True, exist_ok=True)

        metadata = {
            'item': num,
            'title': title,
            'source': info.get('href'),
            'images': [],
            'text_file': None,
            'fetched_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
            'errors': [],
        }

        # fetch detail page via HTTP first
        try:
            r = requests.get(info.get('href'), headers=HEADERS, timeout=30)
            r.encoding = 'shift_jis'
            html = r.text
            # save text
            text_path = folder / f"item_{num}.html"
            text_path.write_text(html, encoding='utf-8', errors='replace')
            metadata['text_file'] = str(text_path)
            imgs = extract_images_from_html(info.get('href'), html)
            print(f"  Found {len(imgs)} image URLs via static parse")
        except Exception as e:
            print(f"  ⚠ HTTP fetch failed: {e}")
            imgs = []
            metadata['errors'].append(f"http_fetch_error: {e}")

        # fallback to playwright if no images
        if len(imgs) == 0 and args.browser_fallback:
            print("  Running Playwright fallback to reveal image URLs...")
            try:
                imgs = playwright_extract_image_urls(info.get('href'), headless=not args.show_browser)
                print(f"  Playwright found {len(imgs)} images")
            except Exception as e:
                print(f"  ⚠ Playwright extraction failed: {e}")
                metadata['errors'].append(f"playwright_error: {e}")

        # download images
        for idx, img_url in enumerate(imgs, start=1):
            parsed = urlparse(img_url)
            ext = Path(parsed.path).suffix or '.jpg'
            fname = f"{safe_title}_p{num}_{idx}{ext}"
            dest = folder / fname
            ok = download_url_stream(img_url, dest, timeout=30, retries=3)
            if ok:
                print(f"   ✓ downloaded {img_url} -> {dest.name}")
                metadata['images'].append({'url': img_url, 'path': str(dest)})
            else:
                print(f"   ✗ failed to download {img_url}")
                metadata['errors'].append(f"download_failed:{img_url}")

        # write metadata
        meta_path = folder / f"metadata_item_{num}.json"
        meta_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding='utf-8')
        summary.append({'item': num, 'title': title, 'images': len(metadata['images']), 'errors': metadata['errors']})

    # write summary
    summary_path = root / 'summary.json'
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f"\nDone. Summary saved to {summary_path}")


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', default='./YDZT_downloads', help='Root folder to save downloads')
    parser.add_argument('--targets', default='1-5,14-22,23-28,29,67-69,75', help='Target item ranges, e.g. 1-5,14-22')
    parser.add_argument('--browser-fallback', action='store_true', help='Enable Playwright fallback to reveal hidden image URLs')
    parser.add_argument('--show-browser', action='store_true', help='Run browser in visible mode when using fallback')
    args = parser.parse_args()
    run(args)
