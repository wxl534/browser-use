#!/usr/bin/env python3
"""
Re-download all images < 100KB from sub/images/ (full-res) instead of images/ (thumbnail).

The website structure:
  Thumbnail:  https://news-sv.aij.or.jp/da2/yachou/images/XXX.jpg  (small, ~10-100KB)
  Full-res:   https://news-sv.aij.or.jp/da2/yachou/sub/images/XXX.jpg  (large, ~100-300KB)

For each small image, try downloading from sub/images/ first.
If that fails (some images may not have a sub version), keep the original.
"""

import sys, io, time
import http.client
from pathlib import Path
from urllib.parse import urlparse

# Fix Windows console encoding
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

BASE = "https://news-sv.aij.or.jp/da2/yachou"
SUB_IMAGES_URL = f"{BASE}/sub/images/"
YDZT_DIR = Path(__file__).parent / "YDZT"
SIZE_THRESHOLD = 100 * 1024  # 100KB
MAX_RETRIES = 3
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"


def download_file(url: str, dest: Path, retries: int = MAX_RETRIES) -> bool:
    for attempt in range(retries):
        try:
            parsed = urlparse(url)
            conn = http.client.HTTPSConnection(parsed.hostname, timeout=60)
            conn.request("GET", parsed.path, headers={"User-Agent": USER_AGENT})
            resp = conn.getresponse()
            if resp.status == 200:
                with open(dest, "wb") as f:
                    while True:
                        chunk = resp.read(8192)
                        if not chunk:
                            break
                        f.write(chunk)
                return True
            else:
                print(f"      HTTP {resp.status}")
            conn.close()
        except Exception as e:
            print(f"      Error: {e} (attempt {attempt + 1}/{retries})")
        if attempt < retries - 1:
            time.sleep(2)
    return False


def main():
    # Find all small images
    small = []
    for jpg in sorted(YDZT_DIR.rglob("*.jpg")):
        if jpg.stat().st_size < SIZE_THRESHOLD:
            small.append(jpg)

    print(f"Found {len(small)} images under 100KB")
    print(f"Attempting to re-download from sub/images/ (full-res)\n")

    fixed = 0
    failed = 0
    skipped = 0

    for i, jpg_path in enumerate(small):
        rel = jpg_path.relative_to(YDZT_DIR)
        img_name = jpg_path.name
        old_size = jpg_path.stat().st_size
        print(f"[{i+1}/{len(small)}] {rel} ({old_size/1024:.1f} KB)")

        # Try sub/images/ first
        full_url = f"{SUB_IMAGES_URL}{img_name}"
        if download_file(full_url, jpg_path):
            new_size = jpg_path.stat().st_size
            if new_size > old_size:
                print(f"      FIXED: {old_size/1024:.1f} KB -> {new_size/1024:.1f} KB")
                fixed += 1
            else:
                # Same size, probably same image; try original images/ URL as fallback
                print(f"      Same size ({new_size/1024:.1f} KB), keeping original")
                skipped += 1
        else:
            print(f"      FAILED to download from sub/images/")
            failed += 1

        time.sleep(0.5)

    print(f"\n{'='*60}")
    print(f"Fixed: {fixed}, Failed: {failed}, Skipped: {skipped}")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
