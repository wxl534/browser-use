#!/usr/bin/env python3
"""Rename YDZT folders based on volume name extracted from info.txt."""

import re
import sys
import io
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

YDZT_DIR = Path(__file__).parent / "YDZT"


def extract_volume_name(info_path: Path) -> str:
    """Extract the volume title from the text content line in info.txt.

    The text content line looks like:
      1&nbsp;第一巻・清国・自北京至張家口 1902...
      23&nbsp;南船北馬 天（1） 1902...
      29&nbsp;紫禁城実測帳 1901...

    We want the part after the entry number prefix, up to the first year pattern or
    the first long space that separates title from description.
    """
    text = info_path.read_text(encoding="utf-8")

    # Find the text content line (line after "--- Text Content ---")
    lines = text.split("\n")
    for i, line in enumerate(lines):
        if "--- Text Content ---" in line:
            if i + 1 < len(lines):
                content_line = lines[i + 1].strip()
                # Remove entry number prefix: "1&nbsp;第..." or "23&nbsp;南..."
                m = re.match(r"^\d+\s*&nbsp;\s*(.+)", content_line)
                if m:
                    title_part = m.group(1)
                    # Extract just the title, stop before year pattern like 1902 or 明治35
                    # Title is everything before the first 4-digit year or era year
                    tm = re.match(r"(.+?)\s*(?:19\d{2}|明治\d+|大正\d+|昭和\d+)", title_part)
                    if tm:
                        return tm.group(1).strip().rstrip("・")
                    # Fallback: return first 30 chars
                    return title_part[:40].strip()
            break
    return ""


def main():
    for folder in sorted(YDZT_DIR.iterdir()):
        if not folder.is_dir():
            continue

        info_path = folder / "info.txt"
        if not info_path.exists():
            continue

        vol_name = extract_volume_name(info_path)
        if not vol_name:
            print(f"  {folder.name}/ -> could not extract volume name, skipping")
            continue

        # Build new name: entry number + volume name
        new_name = f"{folder.name}_{vol_name}"
        new_path = folder.parent / new_name

        if new_path.exists():
            print(f"  {folder.name}/ -> {new_name}/ (already exists, skipping)")
            continue

        # Clean name for filesystem safety
        new_name = re.sub(r'[<>:"/\\|?*]', '_', new_name)

        print(f"  {folder.name}/ -> {new_name}/")
        folder.rename(YDZT_DIR / new_name)


if __name__ == "__main__":
    main()
