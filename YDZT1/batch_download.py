#!/usr/bin/env python3
"""
Batch download script for AIJ Chuta2 gallery pages.

Target entries: 1-5, 14-22, 23-28 (text only, skip download), 29, 67-69, 75
For each entry:
  - Create a folder under YDZT/
  - Extract text content and image URLs into a .txt file
  - Download images (except 23-28 which are text-only)
"""

import os
import re
import sys
import time
import http.client
from pathlib import Path
from urllib.parse import urljoin, urlparse
from html.parser import HTMLParser

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
BASE_URL = "https://news-sv.aij.or.jp/da2/yachou/"
GALLERY_TEMPLATE = "Gallery_3_chuta2-{num}k.htm"
GALLERY_BASE = "https://news-sv.aij.or.jp/da2/yachou/"

# Entries: (start, end, text_only)
# 23-28 are pure text, no images to download
ENTRY_RANGES = [
    (1, 5, False),
    (14, 22, False),
    (23, 28, True),    # text only
    (29, 29, False),
    (67, 69, False),
    (75, 75, False),
]

OUTPUT_DIR = Path(__file__).parent / "YDZT"
LOCAL_HTML_DIR = Path(__file__).parent
MAX_RETRIES = 3
RETRY_DELAY = 2.0  # seconds between requests
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"

# ---------------------------------------------------------------------------
# HTML Parser
# ---------------------------------------------------------------------------
class GalleryPageParser(HTMLParser):
    """Parse a detail page to extract text and image URLs."""

    def __init__(self):
        super().__init__()
        self.thumbnails = []  # list of (thumb_src, sub_href)
        self.text_content = []
        self.current_tag = None
        self.in_explanation = False
        self.in_thumbnail = False
        self.in_pdf = False
        self.pdf_url = None
        self.title = None
        self.tag_stack = []
        self.img_src = None
        self.href = None

    def handle_starttag(self, tag, attrs):
        self.current_tag = tag
        attrs_dict = dict(attrs)

        if tag == "div":
            id_val = attrs_dict.get("id", "")
            if "explanation" in id_val:
                self.in_explanation = True
            elif "thumbnail" in id_val:
                self.in_thumbnail = True
            elif id_val == "pdf":
                self.in_pdf = True

        if tag == "h2" and "main_title" in attrs_dict.get("id", ""):
            self.in_explanation = True

        if tag == "h3":
            self.text_content.append(f"[Heading] ")

        if tag == "p":
            self.text_content.append("")

        if tag == "br":
            self.text_content.append("\n")

        if tag == "img":
            self.img_src = attrs_dict.get("src", "")

        if tag == "a":
            self.href = attrs_dict.get("href", "")

    def handle_endtag(self, tag):
        if tag == "div":
            id_val = ""
            # re-check attrs is not available in endtag, track with stack
            if self.in_explanation and tag == "div":
                self.in_explanation = False
            if self.in_pdf and tag == "div":
                self.in_pdf = False

        if tag == "td" and self.current_tag == "a":
            # reset
            pass

        if tag == "img":
            self.img_src = None
        if tag == "a":
            self.href = None

    def handle_data(self, data):
        data = data.strip()
        if not data:
            return

        # Collect text from explanation area
        if self.in_explanation:
            self.text_content.append(data)

        # Collect image info from thumbnail table
        # We look for img tags inside the thumbnail table
        if self.img_src:
            self.thumbnails.append({
                "src": self.img_src,
                "href": self.href if self.href else "",
            })

    def get_text(self):
        return "\n".join(line for line in self.text_content if line.strip())

    def get_image_urls(self):
        """Return list of unique image URLs found in thumbnails."""
        seen = set()
        result = []
        for t in self.thumbnails:
            src = t["src"]
            if src and not src.startswith(("javascript", "data:", "../images/spacer")):
                if src not in seen:
                    seen.add(src)
                    result.append(src)
        return result


def parse_page_simple(html: str):
    """
    Simpler regex-based parser that extracts:
    - All text content from the page
    - All image references (both thumb and full-size sub pages)
    - PDF links
    """
    results = {
        "title": "",
        "text": [],
        "image_paths": [],       # relative paths like images/01005.jpg
        "sub_pages": [],         # sub page hrefs like sub/img_01005.htm
        "pdf_url": None,
    }

    # Extract title from h2
    h2_match = re.search(r'<h2[^>]*>(.*?)</h2>', html, re.DOTALL)
    if h2_match:
        results["title"] = re.sub(r'<[^>]+>', '', h2_match.group(1)).strip()

    # Extract explanation text
    expl_match = re.search(r'<div\s+id="explanation">(.*?)</div>', html, re.DOTALL)
    if expl_match:
        expl_html = expl_match.group(1)
        # Remove tags but keep text
        expl_text = re.sub(r'<br\s*/?>', '\n', expl_html)
        expl_text = re.sub(r'<[^>]+>', '', expl_text)
        expl_text = re.sub(r'\s+', ' ', expl_text).strip()
        results["text"].append(expl_text)

    # Extract all image src from thumbnail area
    # Pattern: <img src="images/XXXXX.jpg"
    img_pattern = re.compile(r'<img\s+src="(images/[^"]+)"')
    for m in img_pattern.finditer(html):
        path = m.group(1)
        if path not in results["image_paths"]:
            results["image_paths"].append(path)

    # Extract sub page references (full-size image pages)
    sub_pattern = re.compile(r"jamp\(['\"](sub/[^'\"]+)['\"]\)")
    for m in sub_pattern.finditer(html):
        path = m.group(1)
        if path not in results["sub_pages"]:
            results["sub_pages"].append(path)

    # Extract PDF link
    pdf_match = re.search(r'href="(pdf/[^"]+\.pdf)"', html)
    if pdf_match:
        results["pdf_url"] = pdf_match.group(1)

    return results


def parse_sub_page(html: str):
    """Parse a sub page to extract the full-size image URL."""
    # Sub pages typically contain: <img src="../images/XXXXX.jpg" or similar
    img_match = re.search(r'<img[^>]+src="([^"]+)"', html)
    if img_match:
        return img_match.group(1)
    return None


# ---------------------------------------------------------------------------
# HTTP Helper
# ---------------------------------------------------------------------------
def fetch_url(url: str, retries: int = MAX_RETRIES) -> str | None:
    """Fetch a URL and return the content as string (Shift_JIS decoded)."""
    for attempt in range(retries):
        try:
            parsed = urlparse(url)
            conn = http.client.HTTPSConnection(parsed.hostname, timeout=30)
            headers = {"User-Agent": USER_AGENT}
            conn.request("GET", parsed.path, headers=headers)
            resp = conn.getresponse()
            if resp.status == 200:
                body = resp.read()
                # Try Shift_JIS first, fall back to utf-8
                try:
                    return body.decode("shift_jis")
                except UnicodeDecodeError:
                    return body.decode("utf-8", errors="replace")
            else:
                print(f"  HTTP {resp.status} for {url}")
            conn.close()
        except Exception as e:
            print(f"  Error fetching {url}: {e} (attempt {attempt + 1}/{retries})")
        if attempt < retries - 1:
            time.sleep(RETRY_DELAY)
    return None


def download_file(url: str, dest_path: Path, retries: int = MAX_RETRIES) -> bool:
    """Download a file to dest_path. Returns True on success."""
    for attempt in range(retries):
        try:
            parsed = urlparse(url)
            conn = http.client.HTTPSConnection(parsed.hostname, timeout=60)
            headers = {"User-Agent": USER_AGENT}
            conn.request("GET", parsed.path, headers=headers)
            resp = conn.getresponse()
            if resp.status == 200:
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
            print(f"    Error downloading {url}: {e} (attempt {attempt + 1}/{retries})")
        if attempt < retries - 1:
            time.sleep(RETRY_DELAY)
    return False


# ---------------------------------------------------------------------------
# Try to parse sub page to get full-res image, then download
# ---------------------------------------------------------------------------
def get_full_image_url(sub_page_rel: str, entry_num: int) -> str | None:
    """
    Fetch a sub page (e.g. sub/img_01005.htm) and extract the full-size image URL.
    Returns the absolute URL or None.
    """
    sub_url = BASE_URL + sub_page_rel
    html = fetch_url(sub_url)
    if not html:
        return None

    img_src = parse_sub_page(html)
    if img_src:
        # Resolve relative URL
        if img_src.startswith("http"):
            return img_src
        elif img_src.startswith("../"):
            # ../images/foo.jpg -> images/foo.jpg under BASE_URL
            return BASE_URL + img_src[3:]
        else:
            return BASE_URL + sub_page_rel.rsplit("/", 1)[0] + "/" + img_src
    return None


# ---------------------------------------------------------------------------
# Main processing
# ---------------------------------------------------------------------------
def process_entry(num: int, text_only: bool, output_dir: Path):
    """Process a single gallery entry."""
    print(f"\n{'='*60}")
    print(f"Processing entry {num}")
    print(f"{'='*60}")

    # Create folder
    folder = output_dir / f"{num}"
    folder.mkdir(parents=True, exist_ok=True)
    print(f"  Folder: {folder}")

    # Fetch the detail page
    page_url = BASE_URL + GALLERY_TEMPLATE.format(num=num)
    print(f"  URL: {page_url}")

    # Try local HTML file first if available
    html = None
    local_file = LOCAL_HTML_DIR / f"{num}.html"
    if local_file.exists():
        print(f"  Using local HTML: {local_file}")
        try:
            with open(local_file, "rb") as f:
                html = f.read().decode("shift_jis")
        except UnicodeDecodeError:
            with open(local_file, "rb") as f:
                html = f.read().decode("utf-8", errors="replace")

    if html is None:
        print(f"  Fetching from web...")
        html = fetch_url(page_url)

    if not html:
        print(f"  FAILED to get HTML for entry {num}")
        return

    # Parse
    parsed = parse_page_simple(html)

    # Build info text
    info_lines = []
    info_lines.append(f"Entry: {num}")
    info_lines.append(f"URL: {page_url}")
    info_lines.append(f"")
    info_lines.append(f"Title: {parsed['title']}")
    info_lines.append(f"")

    if parsed["text"]:
        info_lines.append("--- Text Content ---")
        for t in parsed["text"]:
            info_lines.append(t)
        info_lines.append("")

    # Image URLs
    info_lines.append("--- Thumbnail Image URLs ---")
    thumb_urls = []
    for img_path in parsed["image_paths"]:
        full_url = GALLERY_BASE + img_path
        thumb_urls.append(full_url)
        info_lines.append(f"  {img_path}  ->  {full_url}")
    info_lines.append("")

    # Sub pages (full-size image links)
    info_lines.append("--- Sub Pages (Full-size Image Links) ---")
    for sub in parsed["sub_pages"]:
        info_lines.append(f"  {sub}  ->  {BASE_URL + sub}")
    info_lines.append("")

    # PDF
    if parsed["pdf_url"]:
        info_lines.append(f"--- PDF ---")
        info_lines.append(f"  {parsed['pdf_url']}  ->  {BASE_URL + parsed['pdf_url']}")
        info_lines.append("")

    # Write info file
    info_path = folder / "info.txt"
    with open(info_path, "w", encoding="utf-8") as f:
        f.write("\n".join(info_lines))
    print(f"  Wrote: {info_path}")

    # Download images (skip for text-only entries)
    if text_only:
        print(f"  Text-only entry, skipping image download.")
        return

    # Strategy: try to get full-res images from sub pages first
    # Sub page pattern: sub/img_01005.htm -> contains link to full image
    print(f"  Fetching sub pages to find full-res images...")
    downloaded = 0
    failed = 0

    for i, sub_page in enumerate(parsed["sub_pages"]):
        sub_url = BASE_URL + sub_page
        print(f"    [{i+1}/{len(parsed['sub_pages'])}] Fetching sub page: {sub_page}")

        full_img_url = get_full_image_url(sub_page, num)
        if full_img_url:
            img_name = Path(full_img_url).name
            dest = folder / img_name
            if dest.exists():
                print(f"      Already exists: {img_name}, skipping")
                downloaded += 1
                continue
            print(f"      Downloading: {img_name}")
            if download_file(full_img_url, dest):
                downloaded += 1
                print(f"      OK")
            else:
                failed += 1
                print(f"      FAILED")
        else:
            print(f"      Could not extract image URL from sub page")
            # Fall back to downloading thumbnail
            if i < len(parsed["image_paths"]):
                thumb_url = GALLERY_BASE + parsed["image_paths"][i]
                img_name = Path(parsed["image_paths"][i]).name
                dest = folder / img_name
                if not dest.exists():
                    if download_file(thumb_url, dest):
                        downloaded += 1
                        print(f"      Downloaded thumbnail: {img_name}")
                    else:
                        failed += 1

        time.sleep(0.5)  # be polite

    # Also download any thumbnail images not covered by sub pages
    for img_path in parsed["image_paths"]:
        img_name = Path(img_path).name
        dest = folder / img_name
        if not dest.exists():
            thumb_url = GALLERY_BASE + img_path
            print(f"    Downloading thumbnail: {img_name}")
            if download_file(thumb_url, dest):
                downloaded += 1
            else:
                failed += 1
            time.sleep(0.5)

    print(f"  Downloaded: {downloaded}, Failed: {failed}")


def main():
    global OUTPUT_DIR
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # Collect all entry numbers
    entries = []
    for start, end, text_only in ENTRY_RANGES:
        for num in range(start, end + 1):
            entries.append((num, text_only))

    print(f"Total entries to process: {len(entries)}")
    for num, text_only in entries:
        tag = " [TEXT ONLY]" if text_only else ""
        print(f"  #{num}{tag}")

    # Process each entry
    for num, text_only in entries:
        process_entry(num, text_only, OUTPUT_DIR)
        time.sleep(RETRY_DELAY)  # pause between entries

    print(f"\n{'='*60}")
    print(f"Done! All files saved to: {OUTPUT_DIR}")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
