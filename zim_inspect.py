#!/usr/bin/env python3
"""Show what the first entries of a .zim file contain, and dump the raw HTML of the
first entry that converts to one line of text or less.

Usage:
    python zim_inspect.py wikipedia.zim            (entries 0-59)
    python zim_inspect.py wikipedia.zim 0 200      (start, count)

Keep this file in the same folder as zim_to_txt.py.
"""

import sys
from pathlib import Path

from zim_to_txt import HTML_TYPES, html_to_text

sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def main():
    if len(sys.argv) < 2 or not Path(sys.argv[1]).is_file():
        sys.exit("Usage: python zim_inspect.py file.zim [start] [count]")

    from libzim.reader import Archive

    zim = Archive(sys.argv[1])
    get_entry = getattr(zim, "get_entry_by_id", None) or getattr(zim, "_get_entry_by_id")
    start = int(sys.argv[2]) if len(sys.argv) > 2 else 0
    count = int(sys.argv[3]) if len(sys.argv) > 3 else 60
    end = min(start + count, zim.entry_count)

    print(f"entry_count: {zim.entry_count}")
    print("id | redirect | mimetype | bytes | text lines | path | title")
    sample = None

    for i in range(start, end):
        entry = get_entry(i)
        if entry.is_redirect:
            print(f"{i} | yes | - | - | - | {entry.path} | {entry.title}")
            continue
        item = entry.get_item()
        mimetype = item.mimetype.split(";")[0].strip().lower()
        raw = bytes(item.content)
        lines = "-"
        if mimetype in HTML_TYPES:
            text = html_to_text(raw.decode("utf-8", errors="replace"))
            lines = len(text.splitlines())
            if lines <= 1 and sample is None:
                sample = (i, entry.path, raw.decode("utf-8", errors="replace"))
        print(f"{i} | no | {mimetype} | {len(raw)} | {lines} | {entry.path} | {entry.title}")

    if sample is None:
        print("\nNo HTML entry with one line of text or less in this range.")
        return

    i, path, html = sample
    out = Path("inspect_sample.html")
    out.write_text(html, encoding="utf-8")
    print(f"\nFirst short entry: #{i} ({path}), {len(html)} characters of HTML.")
    print(f"Full HTML saved to {out.resolve()}")
    print("---- first 3000 characters of the HTML ----")
    print(html[:3000])


if __name__ == "__main__":
    main()
