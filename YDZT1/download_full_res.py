#!/usr/bin/env python3
"""
Download full-resolution images for all YDZT gallery entries.

Replaces existing thumbnail (130x75) images with full-res (672x428+) versions
from sub/images/ directory. Also processes missing entry #16.

Usage:
  python YDZT1\\download_full_res.py [--dry-run]
"""

import http.client
import json
import os
import re
import time
from pathlib import Path
from urllib.parse import urlparse

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
BASE_URL = "https://news-sv.aij.or.jp/da2/yachou/"
GALLERY_TEMPLATE = "Gallery_3_chuta2-{num}k.htm"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
MAX_RETRIES = 3
RETRY_DELAY = 2.0
PAUSE_BETWEEN_ENTRIES = 2.0
PAUSE_BETWEEN_IMAGES = 0.5

# Target entries from YDZT.md
TARGETS = sorted(
    set(list(range(2, 6))
        + list(range(14, 23))
        + list(range(23, 29))
        + [29]
        + list(range(67, 70))
        + [75])
)

TEXT_ONLY = set(range(23, 29))  # marked text-only in spec (but actually have images)

OUTPUT_ROOT = Path(__file__).parent / "野帐"


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------
def fetch_html(url: str) -> str | None:
    """Fetch a URL and return Shift_JIS decoded HTML."""
    for attempt in range(MAX_RETRIES):
        try:
            parsed = urlparse(url)
            conn = http.client.HTTPSConnection(parsed.hostname, timeout=30)
            conn.request("GET", parsed.path, headers={"User-Agent": USER_AGENT})
            resp = conn.getresponse()
            if resp.status == 200:
                body = resp.read()
                try:
                    return body.decode("shift_jis")
                except UnicodeDecodeError:
                    return body.decode("utf-8", errors="replace")
            else:
                print(f"  HTTP {resp.status} for {url}")
            conn.close()
        except Exception as e:
            print(f"  Error fetching {url}: {e} (attempt {attempt + 1}/{MAX_RETRIES})")
        if attempt < MAX_RETRIES - 1:
            time.sleep(RETRY_DELAY)
    return None


def download_file(url: str, dest_path: Path) -> bool:
    """Download a file to dest_path. Returns True on success."""
    for attempt in range(MAX_RETRIES):
        try:
            parsed = urlparse(url)
            conn = http.client.HTTPSConnection(parsed.hostname, timeout=60)
            conn.request("GET", parsed.path, headers={"User-Agent": USER_AGENT})
            resp = conn.getresponse()
            if resp.status == 200:
                dest_path.parent.mkdir(parents=True, exist_ok=True)
                with open(dest_path, "wb") as f:
                    while True:
                        chunk = resp.read(8192)
                        if not chunk:
                            break
                        f.write(chunk)
                return True
            else:
                print(f"    HTTP {resp.status} for {url}")
            conn.close()
        except Exception as e:
            print(f"    Error downloading {url}: {e} (attempt {attempt + 1}/{MAX_RETRIES})")
        if attempt < MAX_RETRIES - 1:
            time.sleep(RETRY_DELAY)
    return False


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------
def parse_index() -> dict[int, dict]:
    """Parse the index page to get entry titles and links."""
    html = fetch_html("https://news-sv.aij.or.jp/da2/yachou/gallery_3_chuta2.htm")
    if not html:
        return {}

    entries = {}
    # Pattern: <td class="td_1">N</td><td>Title</td><td>...<a href="Gallery_3_chuta2-Nk.htm">
    rows = re.findall(
        r'<td\s+class="td_1">(\d+)</td>\s*<td>([^<]+)</td>\s*<td\s+class="td_3">.*?'
        r'<a\s+href="([^"]+)">',
        html,
    )
    for num_str, title, href in rows:
        num = int(num_str)
        entries[num] = {
            "num": num,
            "title": title.strip(),
            "href": href,
        }
    return entries


def parse_detail_page(html: str) -> dict:
    """Parse a detail page to extract image names, sub pages, text, PDF."""
    result = {
        "text": "",
        "image_names": [],       # e.g. ['01005.jpg', '01006.jpg', ...]
        "sub_pages": [],         # e.g. ['sub/img_01005.htm', ...]
        "pdf_url": None,
    }

    # Explanation text
    expl = re.search(r'<div\s+id="explanation">(.*?)</div>', html, re.DOTALL)
    if expl:
        text = expl.group(1)
        text = re.sub(r'<br\s*/?>', '\n', text)
        text = re.sub(r'<[^>]+>', '', text)
        text = re.sub(r'\s+', ' ', text).strip()
        result["text"] = text

    # Thumbnail images: images/XXXXX.jpg
    imgs = re.findall(r'<img\s+src="(images/([^"]+))"', html)
    for full_path, name in imgs:
        if name not in result["image_names"]:
            result["image_names"].append(name)

    # Sub pages: jamp('sub/img_XXXXX.htm') or jamp_2('sub/img_XXXXX.htm')
    subs = re.findall(r"""jamp_?\d*\(\s*'([^']+)'\s*\)""", html)
    for s in subs:
        if s.startswith("sub/") and s not in result["sub_pages"]:
            result["sub_pages"].append(s)

    # PDF
    pdf = re.search(r'href="(pdf/[^"]+\.pdf)"', html)
    if pdf:
        result["pdf_url"] = pdf.group(1)

    return result


def safe_name(s: str) -> str:
    """Sanitize a string for use as folder name."""
    s = s.strip()
    s = re.sub(r"[\\/:*?\"<>|]+", "_", s)
    s = re.sub(r"\s+", "_", s)
    return s[:120]


# ---------------------------------------------------------------------------
# Main processing
# ---------------------------------------------------------------------------
def process_entry(num: int, entries: dict, dry_run: bool) -> dict:
    """Download full-res images for one entry. Returns stats."""
    info = entries.get(num)
    if not info:
        print(f"\n[SKIP] #{num} not found on index page")
        return {"num": num, "status": "skipped", "reason": "not_on_index"}

    title = info["title"]
    safe_title = safe_name(title)
    folder = OUTPUT_ROOT / f"{num}_{title}"
    stats = {"num": num, "downloaded": 0, "updated": 0, "failed": 0, "skipped": 0}

    print(f"\n{'='*60}")
    print(f"#{num} {title}")
    print(f"{'='*60}")

    # Fetch detail page
    page_url = BASE_URL + info["href"]
    html = fetch_html(page_url)
    if not html:
        print(f"  FAILED to fetch {page_url}")
        stats["status"] = "fetch_failed"
        return stats

    parsed = parse_detail_page(html)
    image_names = parsed["image_names"]
    print(f"  Found {len(image_names)} images, {len(parsed['sub_pages'])} sub pages")

    if not image_names:
        print(f"  No images found, skipping")
        stats["status"] = "no_images"
        return stats

    # Create folder
    folder.mkdir(parents=True, exist_ok=True)

    # Download full-res images from sub/images/
    for idx, name in enumerate(image_names, 1):
        full_url = f"{BASE_URL}sub/images/{name}"
        dest = folder / name

        if dest.exists() and not dry_run:
            # Check if it's still a small thumbnail by file size
            size = dest.stat().st_size
            if size > 50_000:  # already a large image
                print(f"  [{idx}/{len(image_names)}] {name} already exists ({size} bytes), skipping")
                stats["skipped"] += 1
                continue
            else:
                print(f"  [{idx}/{len(image_names)}] {name} small ({size} bytes), replacing")

        if dry_run:
            print(f"  [DRY-RUN] Would download {name} -> {dest}")
            stats["downloaded"] += 1
            continue

        ok = download_file(full_url, dest)
        if ok:
            new_size = dest.stat().st_size
            print(f"    OK ({new_size} bytes)")
            stats["downloaded"] += 1
        else:
            print(f"    FAILED")
            stats["failed"] += 1

        time.sleep(PAUSE_BETWEEN_IMAGES)

    # Update info.txt with full-res URLs
    info_lines = [
        f"Entry: {num}",
        f"URL: {page_url}",
        "",
        f"Title: {parsed.get('text', '')}",
        "",
        "--- Full-Resolution Image URLs ---",
    ]
    for name in image_names:
        full_url = f"{BASE_URL}sub/images/{name}"
        info_lines.append(f"  {name}  ->  {full_url}")

    if parsed["sub_pages"]:
        info_lines.append("")
        info_lines.append("--- Sub Pages ---")
        for sub in parsed["sub_pages"]:
            info_lines.append(f"  {sub}  ->  {BASE_URL + sub}")

    if parsed["pdf_url"]:
        info_lines.append("")
        info_lines.append("--- PDF ---")
        info_lines.append(f"  {parsed['pdf_url']}  ->  {BASE_URL + parsed['pdf_url']}")

    info_path = folder / "info.txt"
    if not dry_run:
        info_path.write_text("\n".join(info_lines), encoding="utf-8")

    # Write metadata JSON
    metadata = {
        "item": num,
        "title": title,
        "source": page_url,
        "text": parsed.get("text", ""),
        "images": [
            {
                "name": name,
                "url": f"{BASE_URL}sub/images/{name}",
                "local_path": str(folder / name),
            }
            for name in image_names
        ],
        "pdf_url": f"{BASE_URL}{parsed['pdf_url']}" if parsed["pdf_url"] else None,
        "sub_pages": [BASE_URL + s for s in parsed["sub_pages"]],
    }
    meta_path = folder / "metadata.json"
    if not dry_run:
        meta_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")

    stats["status"] = "done"
    return stats


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="Preview without downloading")
    args = parser.parse_args()

    print(f"YDZT Full-Resolution Image Downloader")
    print(f"Targets: {TARGETS} ({len(TARGETS)} entries)")
    print(f"Dry run: {args.dry_run}")

    # Parse index
    print("\nParsing index page...")
    entries = parse_index()
    print(f"Found {len(entries)} entries on index")

    # Process
    all_stats = []
    for num in TARGETS:
        stats = process_entry(num, entries, args.dry_run)
        all_stats.append(stats)
        time.sleep(PAUSE_BETWEEN_ENTRIES)

    # Summary
    print(f"\n{'='*60}")
    print("SUMMARY")
    print(f"{'='*60}")
    total_downloaded = sum(s.get("downloaded", 0) for s in all_stats)
    total_failed = sum(s.get("failed", 0) for s in all_stats)
    total_skipped = sum(s.get("skipped", 0) for s in all_stats)
    print(f"Total images downloaded: {total_downloaded}")
    print(f"Total skipped (already large): {total_skipped}")
    print(f"Total failed: {total_failed}")

    for s in all_stats:
        num = s["num"]
        status = s.get("status", "unknown")
        dl = s.get("downloaded", 0)
        fail = s.get("failed", 0)
        skip = s.get("skipped", 0)
        print(f"  #{num}: {status} (downloaded={dl}, skipped={skip}, failed={fail})")

    # Write summary JSON
    summary_path = OUTPUT_ROOT / "summary_full_res.json"
    if not args.dry_run:
        summary_path.write_text(
            json.dumps(all_stats, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"\nSummary saved to {summary_path}")


if __name__ == "__main__":
    main()
