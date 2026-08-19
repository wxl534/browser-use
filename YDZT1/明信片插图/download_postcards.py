#!/usr/bin/env python3
"""
Download and extract postcard images from AIJ Hagakie gallery.

The postcard gallery (https://news-sv.aij.or.jp/da2/hagakie/gallery_3_hagakie2.htm)
organizes ~3717 postcards into 38 PDF groups. Each PDF contains scanned postcard pages.

Target groups: No. 20-23, 25-27, 29-30, 32, 35, 37
Each group becomes a folder with extracted images + info.txt.

Approach:
  1. Download each PDF from the website
  2. Extract each page as a high-quality JPEG image using PyMuPDF
  3. Write info.txt with metadata, PDF URL, and image listing
"""

import sys, io, re, time
import http.client
from pathlib import Path
from urllib.parse import urlparse

import fitz  # PyMuPDF

# Fix Windows console encoding
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

BASE_URL = "https://news-sv.aij.or.jp/da2/hagakie/"
PDF_BASE = BASE_URL + "pdf/"

# Target groups: (group_number, pdf_filename, description)
# From the HTML table:
# 20: 1901-2000 (Showa 8/3/4 - Showa 8/12/31)
# 21: 2001-2100 (Showa 9/1/1 - Showa 9/10/28)
# 22: 2101-2200 (Showa 9/10/31 - Showa 10/7/19)
# 23: 2201-2300 (Showa 10/7/25 - Showa 11/6/25)
# 25: 2401-2500 (Showa 12/10/7 - Showa 13/12/31)
# 26: 2501-2600 (Showa 13/12/31 - Showa 15/1/1)
# 27: 2601-2700 (Showa 15/1/5 - Showa 16/1/1)
# 29: 2801-2900 (Showa 16/12/7 - Showa 17/8/13)
# 30: 2901-3000 (Showa 17/8/19 - Showa 18/7/17)
# 32: 3101-3200 (Showa 19/4/15 - Showa 20/1/19)
# 35: 3401-3500 (Showa 21/5/15 - Showa 22/5/16)
# 37: 3601-3700 (Showa 23/12/6 - Showa 25/7/3)
GROUPS = [
    (20, "1901_2000", "1901番（昭和8年3月4日）～2000番（昭和8年12月31日）"),
    (21, "2001_2100", "2001番（昭和9年1月1日）～2100番（昭和9年10月28日）"),
    (22, "2101_2200", "2101番（昭和9年10月31日）～2200番（昭和10年7月19日）"),
    (23, "2201_2300", "2201番（昭和10年7月25日）～2300番（昭和11年6月25日）"),
    (25, "2401_2500", "2401番（昭和12年10月7日）～2500番（昭和13年12月31日）"),
    (26, "2501_2600", "2501番（昭和13年12月31日）～2600番（昭和15年1月1日）"),
    (27, "2601_2700", "2601番（昭和15年1月5日）～2700番（昭和16年1月1日）"),
    (29, "2801_2900", "2801番（昭和16年12月7日）～2900番（昭和17年8月13日）"),
    (30, "2901_3000", "2901番（昭和17年8月19日）～3000番（昭和18年7月17日）"),
    (32, "3101_3200", "3101番（昭和19年4月15日）～3195番（昭和20年1月19日）"),
    (35, "3401_3500", "3401番（昭和21年5月15日）～3500番（昭和22年5月16日）"),
    (37, "3601_3700", "3601番（昭和23年12月6日）～3700番（昭和25年7月3日）"),
]

OUTPUT_DIR = Path(__file__).parent / "Hagakie"
MAX_RETRIES = 3
RETRY_DELAY = 2.0
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"

# DPI for PDF page extraction - higher = larger but better quality
# At 200 DPI, a typical postcard page (~800x500 px) is readable
EXTRACT_DPI = 200


def download_pdf(pdf_url: str, dest: Path) -> bool:
    """Download a PDF file."""
    for attempt in range(MAX_RETRIES):
        try:
            parsed = urlparse(pdf_url)
            conn = http.client.HTTPSConnection(parsed.hostname, timeout=120)
            conn.request("GET", parsed.path, headers={"User-Agent": USER_AGENT})
            resp = conn.getresponse()
            if resp.status == 200:
                with open(dest, "wb") as f:
                    while True:
                        chunk = resp.read(65536)
                        if not chunk:
                            break
                        f.write(chunk)
                size_mb = dest.stat().st_size / 1024 / 1024
                print(f"    Downloaded: {size_mb:.1f} MB")
                return True
            else:
                print(f"    HTTP {resp.status}")
            conn.close()
        except Exception as e:
            print(f"    Error: {e} (attempt {attempt + 1}/{MAX_RETRIES})")
        if attempt < MAX_RETRIES - 1:
            time.sleep(RETRY_DELAY)
    return False


def extract_pdf_pages(pdf_path: Path, output_dir: Path, dpi: int = EXTRACT_DPI) -> list[str]:
    """Extract each page of a PDF as a JPEG image. Returns list of image filenames."""
    doc = fitz.open(pdf_path)
    images = []

    # Calculate zoom factor from DPI (default PDF is 72 DPI)
    zoom = dpi / 72.0
    mat = fitz.Matrix(zoom, zoom)

    for page_num in range(len(doc)):
        page = doc[page_num]
        pix = page.get_pixmap(matrix=mat)

        # Save as JPEG
        img_name = f"page_{page_num + 1:03d}.jpg"
        img_path = output_dir / img_name
        pix.save(str(img_path), "JPEG")
        images.append(img_name)

        size_kb = img_path.stat().st_size / 1024
        print(f"    Page {page_num + 1}/{len(doc)}: {pix.width}x{pix.height} -> {size_kb:.0f} KB")

    doc.close()
    return images


def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    print(f"Output directory: {OUTPUT_DIR}")
    print(f"Target groups: {len(GROUPS)}")
    print()

    total_images = 0

    for group_num, pdf_name, description in GROUPS:
        folder_name = f"{group_num}_{description}"
        # Clean folder name for filesystem
        folder_name = re.sub(r'[<>:"/\\|?*]', '_', folder_name)
        folder = OUTPUT_DIR / folder_name
        folder.mkdir(parents=True, exist_ok=True)

        pdf_url = f"{PDF_BASE}{pdf_name}.pdf"
        pdf_path = folder / f"{pdf_name}.pdf"

        print(f"{'='*60}")
        print(f"Group No.{group_num}: {description}")
        print(f"  URL: {pdf_url}")
        print(f"  Folder: {folder}")

        # Download PDF if not already present
        if not pdf_path.exists():
            print(f"  Downloading PDF...")
            if not download_pdf(pdf_url, pdf_path):
                print(f"  FAILED to download PDF, skipping")
                # Write partial info
                info_lines = [
                    f"Group: {group_num}",
                    f"PDF URL: {pdf_url}",
                    f"Description: {description}",
                    f"",
                    f"STATUS: FAILED to download PDF",
                ]
                (folder / "info.txt").write_text("\n".join(info_lines), encoding="utf-8")
                time.sleep(RETRY_DELAY)
                continue
        else:
            print(f"  PDF already exists: {pdf_path.stat().st_size / 1024 / 1024:.1f} MB")

        # Extract pages
        print(f"  Extracting pages...")
        images = extract_pdf_pages(pdf_path, folder)
        total_images += len(images)
        print(f"  Extracted {len(images)} images")

        # Write info.txt
        info_lines = [
            f"Group: No.{group_num}",
            f"Main Page: https://news-sv.aij.or.jp/da2/hagakie/gallery_3_hagakie2.htm",
            f"PDF URL: {pdf_url}",
            f"PDF File: {pdf_name}.pdf",
            f"",
            f"Description: {description}",
            f"",
            f"--- Extracted Images ({len(images)} pages) ---",
        ]
        for img in images:
            info_lines.append(f"  {img}")
        info_lines.append("")

        (folder / "info.txt").write_text("\n".join(info_lines), encoding="utf-8")
        print(f"  Wrote info.txt")

        time.sleep(RETRY_DELAY)

    print(f"\n{'='*60}")
    print(f"Done! Total images extracted: {total_images}")
    print(f"Output: {OUTPUT_DIR}")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
