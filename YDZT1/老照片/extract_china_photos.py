#!/usr/bin/env python3
"""
Re-download the large China photos PDF (158MB) and extract pages.
Uses chunked reading with longer timeout to avoid connection drops.
"""

import sys, io, time
import http.client
from pathlib import Path
from urllib.parse import urlparse

import fitz

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

PDF_URL = "https://news-sv.aij.or.jp/da2/kosyashin/pdf/aij-chuta_kosyashin_10-5.pdf"
OUTPUT_DIR = Path(__file__).parent / "Kosyashin_China" / "10-5_中国写真"
PDF_PATH = OUTPUT_DIR / "aij-chuta_kosyashin_10-5.pdf"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
EXTRACT_DPI = 200


def download_pdf_chunked(url: str, dest: Path, chunk_size: int = 32768, read_timeout: int = 120) -> bool:
    """Download with persistent connection and chunked reads."""
    parsed = urlparse(url)
    hostname = parsed.hostname
    path = parsed.path

    # Keep connection alive by reusing
    for attempt in range(3):
        dest_partial = dest.with_suffix(".pdf.part")
        try:
            conn = http.client.HTTPSConnection(hostname, timeout=300)
            conn.request("GET", path, headers={"User-Agent": USER_AGENT})
            resp = conn.getresponse()
            if resp.status not in (200, 206):
                print(f"    HTTP {resp.status}, retrying...")
                conn.close()
                time.sleep(3)
                continue

            total = int(resp.getheader("Content-Length", 0))
            downloaded = 0

            with open(dest_partial, "wb") as f:
                while True:
                    try:
                        chunk = resp.read(chunk_size)
                        if not chunk:
                            break
                        f.write(chunk)
                        downloaded += len(chunk)
                        if downloaded % (1024 * 1024) < chunk_size:  # print every ~1MB
                            if total:
                                pct = downloaded / total * 100
                                mb = downloaded / 1024 / 1024
                                total_mb = total / 1024 / 1024
                                print(f"\r    Downloading: {mb:.1f}/{total_mb:.1f} MB ({pct:.1f}%)", end="", flush=True)
                            else:
                                mb = downloaded / 1024 / 1024
                                print(f"\r    Downloading: {mb:.1f} MB", end="", flush=True)
                    except (TimeoutError, OSError) as e:
                        print(f"\n    Read error at {downloaded/1024/1024:.1f} MB: {e}")
                        # Resume from where we left off
                        headers = {"User-Agent": USER_AGENT, "Range": f"bytes={downloaded}-"}
                        conn.close()
                        time.sleep(2)
                        conn = http.client.HTTPSConnection(hostname, timeout=300)
                        conn.request("GET", path, headers=headers)
                        resp = conn.getresponse()
                        if resp.status == 206:
                            print(f"    Resumed from {downloaded/1024/1024:.1f} MB")
                            continue
                        elif resp.status == 200:
                            # Server doesn't support range, restart
                            print(f"    Server doesn't support resume, restarting...")
                            conn.close()
                            time.sleep(2)
                            break
                        else:
                            conn.close()
                            raise

            conn.close()
            print()
            # Rename partial to final
            if dest_partial.exists():
                dest_partial.rename(dest)
            print(f"    Downloaded: {dest.stat().st_size / 1024 / 1024:.1f} MB")
            return True

        except Exception as e:
            print(f"\n    Error (attempt {attempt + 1}/3): {e}")
            if dest_partial.exists():
                dest_partial.unlink()
        if attempt < 2:
            time.sleep(5)
    return False


def extract_pages(pdf_path: Path, output_dir: Path, dpi: int = EXTRACT_DPI) -> int:
    """Extract PDF pages as JPEGs."""
    doc = fitz.open(pdf_path)
    total = len(doc)
    zoom = dpi / 72.0
    mat = fitz.Matrix(zoom, zoom)
    count = 0

    for page_num in range(total):
        page = doc[page_num]
        pix = page.get_pixmap(matrix=mat)
        img_name = f"page_{page_num + 1:03d}.jpg"
        img_path = output_dir / img_name
        pix.save(str(img_path), "JPEG")
        count += 1
        size_kb = img_path.stat().st_size / 1024
        print(f"\r    Extracting: {page_num + 1}/{total} ({pix.width}x{pix.height}, {size_kb:.0f} KB)", end="", flush=True)

    print()
    doc.close()
    return count


def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    print(f"PDF: {PDF_URL}")
    print(f"Output: {OUTPUT_DIR}")
    print()

    # Remove incomplete PDF
    if PDF_PATH.exists():
        print(f"Removing incomplete PDF ({PDF_PATH.stat().st_size / 1024 / 1024:.1f} MB)")
        PDF_PATH.unlink()

    # Download
    print("Downloading PDF...")
    if not download_pdf_chunked(PDF_URL, PDF_PATH):
        print("FAILED to download PDF")
        return

    # Extract
    print("Extracting pages...")
    count = extract_pages(PDF_PATH, OUTPUT_DIR)
    print(f"Extracted {count} images")

    # Write info.txt
    info_lines = [
        "Section: 中国写真",
        "Main Page: https://news-sv.aij.or.jp/da2/kosyashin/gallery_3_kosyashin.htm",
        f"PDF URL: {PDF_URL}",
        f"PDF File: aij-chuta_kosyashin_10-5.pdf",
        "",
        f"--- Extracted Images ({count} pages) ---",
    ]
    for i in range(1, count + 1):
        info_lines.append(f"  page_{i:03d}.jpg")
    info_lines.append("")
    (OUTPUT_DIR / "info.txt").write_text("\n".join(info_lines), encoding="utf-8")
    print("Wrote info.txt")
    print(f"Done! {count} images extracted.")


if __name__ == "__main__":
    main()
