#!/usr/bin/env python3
"""
Download and extract China-related old photos from AIJ Kosyashin gallery.

Source: https://news-sv.aij.or.jp/da2/kosyashin/gallery_3_kosyashin.htm

China-related sections:
  - 中国・ビルマ・ペルシャ・オマール・マジスト (10-4) -> PDF: aij-chuta_kosyashin_10-4.pdf (13.2MB)
  - 中国写真 (10-5) -> PDF: aij-chuta_kosyashin_10-5.pdf (158.1MB)

Approach: Download each PDF, extract pages as high-quality JPEGs at 200 DPI.
"""

import sys, io, re, time
import http.client
from pathlib import Path
from urllib.parse import urlparse

import fitz  # PyMuPDF

# Fix Windows console encoding
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

BASE_URL = "https://news-sv.aij.or.jp/da2/kosyashin/"
PDF_BASE = BASE_URL + "pdf/"

# China-related sections
SECTIONS = [
    {
        "name": "中国・ビルマ・ペルシャ・オマール・マジスト",
        "pdf": "aij-chuta_kosyashin_10-4.pdf",
        "folder": "10-4_中国ビルマペルシャオマールマジスト",
    },
    {
        "name": "中国写真",
        "pdf": "aij-chuta_kosyashin_10-5.pdf",
        "folder": "10-5_中国写真",
    },
]

OUTPUT_DIR = Path(__file__).parent / "Kosyashin_China"
MAX_RETRIES = 3
RETRY_DELAY = 2.0
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
EXTRACT_DPI = 200


def download_pdf(pdf_url: str, dest: Path) -> bool:
    """Download a PDF file with progress reporting."""
    for attempt in range(MAX_RETRIES):
        try:
            parsed = urlparse(pdf_url)
            conn = http.client.HTTPSConnection(parsed.hostname, timeout=300)
            conn.request("GET", parsed.path, headers={"User-Agent": USER_AGENT})
            resp = conn.getresponse()
            if resp.status == 200:
                total = int(resp.getheader("Content-Length", 0))
                downloaded = 0
                with open(dest, "wb") as f:
                    while True:
                        chunk = resp.read(65536)
                        if not chunk:
                            break
                        f.write(chunk)
                        downloaded += len(chunk)
                        if total:
                            pct = downloaded / total * 100
                            mb = downloaded / 1024 / 1024
                            total_mb = total / 1024 / 1024
                            print(f"\r    Downloading: {mb:.1f}/{total_mb:.1f} MB ({pct:.0f}%)", end="", flush=True)
                print()  # newline after progress
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
    """Extract each page of a PDF as a JPEG image."""
    doc = fitz.open(pdf_path)
    images = []

    zoom = dpi / 72.0
    mat = fitz.Matrix(zoom, zoom)

    total = len(doc)
    for page_num in range(total):
        page = doc[page_num]
        pix = page.get_pixmap(matrix=mat)

        img_name = f"page_{page_num + 1:03d}.jpg"
        img_path = output_dir / img_name
        pix.save(str(img_path), "JPEG")
        images.append(img_name)

        size_kb = img_path.stat().st_size / 1024
        print(f"\r    Extracting: {page_num + 1}/{total} pages ({pix.width}x{pix.height}, {size_kb:.0f} KB)", end="", flush=True)

    print()  # newline
    doc.close()
    return images


def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    print(f"Output directory: {OUTPUT_DIR}")
    print(f"Sections: {len(SECTIONS)}")
    print()

    total_images = 0

    for sec in SECTIONS:
        folder = OUTPUT_DIR / sec["folder"]
        folder.mkdir(parents=True, exist_ok=True)

        pdf_url = PDF_BASE + sec["pdf"]
        pdf_path = folder / sec["pdf"]

        print(f"{'='*60}")
        print(f"Section: {sec['name']}")
        print(f"  PDF: {sec['pdf']}")
        print(f"  URL: {pdf_url}")
        print(f"  Folder: {folder}")

        # Download PDF
        if not pdf_path.exists():
            print(f"  Downloading PDF...")
            if not download_pdf(pdf_url, pdf_path):
                print(f"  FAILED to download PDF, skipping")
                info_lines = [
                    f"Section: {sec['name']}",
                    f"PDF URL: {pdf_url}",
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
            f"Section: {sec['name']}",
            f"Main Page: https://news-sv.aij.or.jp/da2/kosyashin/gallery_3_kosyashin.htm",
            f"PDF URL: {pdf_url}",
            f"PDF File: {sec['pdf']}",
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
