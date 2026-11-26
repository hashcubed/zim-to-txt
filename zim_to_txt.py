#!/usr/bin/env python3
"""Convert every article in a .zim file into its own .txt file, all in one folder.

Usage:
    python zim_to_txt.py wikipedia.zim
    python zim_to_txt.py wikipedia.zim output_folder
    python zim_to_txt.py wikipedia.zim --workers 12       (default: all logical processors)
    python zim_to_txt.py wikipedia.zim --limit 1000       (test on the first 1000 entries)

The output folder must be new or empty.
"""

import argparse
import datetime
import multiprocessing
import os
import re
import signal
import sys
import time
from html.parser import HTMLParser
from pathlib import Path

HTML_TYPES = {"text/html", "application/xhtml+xml"}

# Redirect pages stored as HTML (e.g. a link forwarding to a section of another article).
META_REFRESH = re.compile(r"<meta[^>]+http-equiv\s*=\s*[\"']?refresh", re.IGNORECASE)

# Content inside these tags is never shown as article text.
SKIP_TAGS = {"head", "script", "style", "noscript", "template", "svg"}

# Tags that start a new line of text.
BLOCK_TAGS = {
    "p", "div", "section", "article", "header", "footer", "nav", "aside", "main",
    "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "li", "dl", "dt", "dd",
    "table", "tr", "caption", "blockquote", "pre", "figure", "figcaption",
    "hr", "br", "details", "summary", "form", "fieldset",
}

WHITESPACE = re.compile(r"\s+")
SPACES = re.compile(r"[ \t]+")

# Characters that cannot appear in Windows file names (also safe on Linux/macOS).
INVALID_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
RESERVED_NAMES = {"CON", "PRN", "AUX", "NUL"}
RESERVED_NAMES |= {f"COM{i}" for i in range(1, 10)}
RESERVED_NAMES |= {f"LPT{i}" for i in range(1, 10)}
MAX_NAME_BYTES = 150  # keeps the full file name well under the 255-byte limit

CHUNK_SIZE = 20000  # entries handed to a worker at a time


class _TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag == "body":
            self.skip_depth = 0  # recover if an earlier skipped tag was never closed
        if tag in SKIP_TAGS:
            self.skip_depth += 1
            return
        if tag in BLOCK_TAGS:
            self.parts.append("\n")
        elif tag in ("td", "th"):
            self.parts.append(" ")

    def handle_endtag(self, tag):
        if tag in SKIP_TAGS:
            if self.skip_depth > 0:
                self.skip_depth -= 1
            return
        if tag in BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data):
        if self.skip_depth == 0:
            self.parts.append(WHITESPACE.sub(" ", data))

    def text(self):
        raw = "".join(self.parts)
        lines = (SPACES.sub(" ", line).strip() for line in raw.split("\n"))
        return "\n".join(line for line in lines if line)


def html_to_text(html):
    parser = _TextExtractor()
    parser.feed(html)
    parser.close()
    text = parser.text()
    return text + "\n" if text else ""


def safe_stem(name):
    """Turn an article title into a file name stem that is valid on any OS."""
    stem = INVALID_CHARS.sub("_", name).strip().rstrip(". ")
    stem = stem.encode("utf-8")[:MAX_NAME_BYTES].decode("utf-8", "ignore")
    stem = stem.rstrip(". ")
    if not stem:
        stem = "untitled"
    if stem.split(".")[0].upper() in RESERVED_NAMES:
        stem = "_" + stem
    return stem


def write_unique(out_dir, stem, text):
    """Create <stem>.txt, or <stem>_2.txt, <stem>_3.txt ... if the name is taken.

    Exclusive creation ("x") is atomic, so this is safe with several processes
    and also catches names that differ only by case on Windows.
    Returns the number of bytes written.
    """
    data = text.encode("utf-8")
    n = 1
    while True:
        name = stem if n == 1 else f"{stem}_{n}"
        try:
            with open(out_dir / f"{name}.txt", "xb") as f:
                f.write(data)
            return len(data)
        except FileExistsError:
            n += 1


def _get_entry_function(zim):
    # Newer libzim versions call this get_entry_by_id; older ones _get_entry_by_id.
    return getattr(zim, "get_entry_by_id", None) or getattr(zim, "_get_entry_by_id", None)


# ---- worker processes ------------------------------------------------------

_state = {}


def _init_worker(zim_path, out_dir):
    signal.signal(signal.SIGINT, signal.SIG_IGN)  # Ctrl+C is handled by the main process
    from libzim.reader import Archive

    zim = Archive(zim_path)
    _state["zim"] = zim  # keep the archive alive
    _state["get_entry"] = _get_entry_function(zim)
    _state["out_dir"] = Path(out_dir)


def _process_entry(i):
    entry = _state["get_entry"](i)
    if entry.is_redirect:
        return "skipped", 0

    item = entry.get_item()
    mimetype = item.mimetype.split(";")[0].strip().lower()
    if mimetype not in HTML_TYPES:
        return "skipped", 0

    html = bytes(item.content).decode("utf-8", errors="replace")
    if META_REFRESH.search(html[:3000]):
        return "redirect_pages", 0
    text = html_to_text(html)
    if not text:
        return "empty", 0

    size = write_unique(_state["out_dir"], safe_stem(entry.title or entry.path), text)
    return "written", size


def _process_chunk(bounds):
    start, end = bounds
    counts = {"written": 0, "skipped": 0, "redirect_pages": 0, "empty": 0, "failed": 0, "bytes": 0}
    consecutive_failures = 0
    for i in range(start, end):
        try:
            kind, size = _process_entry(i)
            counts[kind] += 1
            counts["bytes"] += size
            consecutive_failures = 0
        except Exception as e:  # one bad entry must not stop the whole run
            counts["failed"] += 1
            consecutive_failures += 1
            print(f"Failed on entry #{i}: {ascii(e)}", file=sys.stderr)
            if consecutive_failures >= 50:
                raise RuntimeError(f"50 entries in a row failed (around entry #{i}). Last error: {ascii(e)}")
    return end - start, counts


# ---- main process ----------------------------------------------------------

def _fmt_time(seconds):
    return str(datetime.timedelta(seconds=int(seconds)))


def convert(zim_path, out_dir, limit=None, workers=1, chunk_size=CHUNK_SIZE):
    try:
        from libzim.reader import Archive
    except ImportError:
        sys.exit("The 'libzim' package is not installed. Run: pip install libzim")

    zim = Archive(str(zim_path))
    if _get_entry_function(zim) is None:
        sys.exit("This libzim version has no get_entry_by_id method. Run: pip install --upgrade libzim")
    total = zim.entry_count
    del zim
    if limit is not None:
        total = min(total, limit)

    if out_dir.exists() and not out_dir.is_dir():
        sys.exit(f"Not a folder: {out_dir}")
    if out_dir.is_dir() and any(out_dir.iterdir()):
        sys.exit(f"Output folder is not empty: {out_dir}\nDelete it or choose a new folder.")
    out_dir.mkdir(parents=True, exist_ok=True)

    chunks = [(s, min(s + chunk_size, total)) for s in range(0, total, chunk_size)]
    totals = {"written": 0, "skipped": 0, "redirect_pages": 0, "empty": 0, "failed": 0, "bytes": 0}
    done = 0
    start_time = last_print = time.monotonic()
    print(f"Converting {total:,} entries with {workers} worker process(es)...")

    try:
        with multiprocessing.Pool(
            processes=workers, initializer=_init_worker, initargs=(str(zim_path), str(out_dir))
        ) as pool:
            for n, counts in pool.imap_unordered(_process_chunk, chunks):
                done += n
                for key, value in counts.items():
                    totals[key] += value
                now = time.monotonic()
                if now - last_print >= 10:
                    last_print = now
                    elapsed = now - start_time
                    rate = done / elapsed
                    eta = (total - done) / rate if rate else 0
                    print(
                        f"{done / total:6.1%}  {done:,}/{total:,} entries  "
                        f"{totals['written']:,} files ({totals['bytes'] / 1e9:.1f} GB)  {rate:,.0f} entries/s  "
                        f"elapsed {_fmt_time(elapsed)}  rough ETA {_fmt_time(eta)}"
                    )
    except RuntimeError as e:
        sys.exit(f"Stopping: {e}")
    except KeyboardInterrupt:
        sys.exit("Interrupted. The output folder holds a partial result; delete it before rerunning.")

    print(
        f"Done in {_fmt_time(time.monotonic() - start_time)}. {totals['written']:,} files ({totals['bytes'] / 1e9:.1f} GB of text) written to {out_dir}\n"
        f"Skipped (redirects, images, CSS, etc.): {totals['skipped']:,}\n"
        f"Skipped (HTML redirect pages): {totals['redirect_pages']:,}\n"
        f"Skipped (no text): {totals['empty']:,}\n"
        f"Failed: {totals['failed']:,}"
    )


def main():
    parser = argparse.ArgumentParser(description="Convert a .zim file to one .txt file per article.")
    parser.add_argument("zim_file", type=Path, help="path to the .zim file")
    parser.add_argument("output_folder", type=Path, nargs="?",
                        help="new or empty folder for the .txt files (default: <zim name> next to the .zim)")
    parser.add_argument("--limit", type=int, help="only process the first N entries (for testing)")
    parser.add_argument("--workers", type=int, default=os.cpu_count() or 1,
                        help="number of worker processes (default: all logical processors)")
    args = parser.parse_args()

    if not args.zim_file.is_file():
        sys.exit(f"File not found: {args.zim_file}")
    if args.workers < 1:
        sys.exit("--workers must be at least 1")

    out_dir = args.output_folder or args.zim_file.with_suffix("")
    convert(args.zim_file, out_dir, args.limit, args.workers)


if __name__ == "__main__":
    main()
