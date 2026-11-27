#!/usr/bin/env python3
"""Convert HTML articles and text-based PDFs in a .zim archive into .txt files.

Each supported archive entry is written as a separate UTF-8 text file in one
output folder. PDF extraction uses the embedded text layer and page geometry;
OCR is never run. Image-only/scanned PDF pages therefore produce no text.

Usage:
    python zim_to_txt_with_pdf.py wikipedia.zim
    python zim_to_txt_with_pdf.py wikipedia.zim output_folder
    python zim_to_txt_with_pdf.py wikipedia.zim --workers 12
    python zim_to_txt_with_pdf.py wikipedia.zim --limit 1000

Dependencies:
    python -m pip install libzim pymupdf

The output folder must be new or empty.
"""

import argparse
import datetime
import math
import multiprocessing
import os
import re
import signal
import sys
import time
import unicodedata
from collections import Counter
from html.parser import HTMLParser
from pathlib import Path

HTML_TYPES = {"text/html", "application/xhtml+xml"}
PDF_TYPES = {"application/pdf", "application/x-pdf"}
BINARY_TYPES = {"application/octet-stream", "binary/octet-stream"}

META_REFRESH = re.compile(r"<meta[^>]+http-equiv\s*=\s*[\"']?refresh", re.IGNORECASE)
SKIP_TAGS = {"head", "script", "style", "noscript", "template", "svg"}
BLOCK_TAGS = {
    "p", "div", "section", "article", "header", "footer", "nav", "aside", "main",
    "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "li", "dl", "dt", "dd",
    "table", "tr", "caption", "blockquote", "pre", "figure", "figcaption",
    "hr", "br", "details", "summary", "form", "fieldset",
}

WHITESPACE = re.compile(r"\s+")
SPACES = re.compile(r"[ \t]+")
INVALID_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
RESERVED_NAMES = {"CON", "PRN", "AUX", "NUL"}
RESERVED_NAMES |= {f"COM{i}" for i in range(1, 10)}
RESERVED_NAMES |= {f"LPT{i}" for i in range(1, 10)}
MAX_NAME_BYTES = 150
CHUNK_SIZE = 20000

LIST_START = re.compile(r"^(?:[•●▪◦‣*-]\s+|\d+[.)]\s+|[A-Za-z][.)]\s+|\([A-Za-z0-9]+\)\s+)")
PAGE_NUMBER = re.compile(r"^(?:\d{1,4}|[ivxlcdm]{1,8})$", re.IGNORECASE)


class _TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag == "body":
            self.skip_depth = 0
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
    """Turn an article/document title into a cross-platform filename stem."""
    stem = INVALID_CHARS.sub("_", str(name)).strip().rstrip(". ")
    stem = stem.encode("utf-8")[:MAX_NAME_BYTES].decode("utf-8", "ignore").rstrip(". ")
    if not stem:
        stem = "untitled"
    if stem.split(".")[0].upper() in RESERVED_NAMES:
        stem = "_" + stem
    return stem


def write_unique(out_dir, stem, text):
    """Write a uniquely named UTF-8 file; safe when several workers run at once."""
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


# ---------------------------- PDF text extraction ---------------------------


def _normalise_pdf_line(text):
    text = unicodedata.normalize("NFC", text)
    text = text.replace("\u00a0", " ").replace("\u200b", "").replace("\u00ad", "")
    text = text.replace("\ufb00", "ff").replace("\ufb01", "fi").replace("\ufb02", "fl")
    text = text.replace("\ufb03", "ffi").replace("\ufb04", "ffl")
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text)
    return WHITESPACE.sub(" ", text).strip()


def _span_cluster_text(spans):
    """Join adjacent font spans, inserting spaces where geometry indicates one."""
    pieces = []
    previous_right = None
    previous_text = ""
    for span in sorted(spans, key=lambda item: item["bbox"][0]):
        value = str(span.get("text", ""))
        if not value:
            continue
        x0 = float(span["bbox"][0])
        size = float(span.get("size", 10) or 10)
        if (
            previous_right is not None
            and x0 - previous_right > max(1.5, size * 0.18)
            and previous_text
            and not previous_text.endswith((" ", "\n"))
            and not value.startswith((" ", "\n", ",", ".", ";", ":", "!", "?", ")", "]"))
        ):
            pieces.append(" ")
        pieces.append(value)
        previous_right = float(span["bbox"][2])
        previous_text = value
    return _normalise_pdf_line("".join(pieces))


def _line_records(page):
    """Extract positioned text lines. Image blocks are intentionally ignored."""
    page_dict = page.get_text("dict", sort=False)
    records = []
    for block in page_dict.get("blocks", []):
        if block.get("type", 0) != 0:
            continue
        for line in block.get("lines", []):
            spans = [s for s in line.get("spans", []) if str(s.get("text", "")).strip()]
            if not spans:
                continue
            spans.sort(key=lambda s: (float(s["bbox"][0]), float(s["bbox"][1])))

            # Some PDFs encode multiple visual columns as one line. Split only
            # where there is an unusually large horizontal gap between spans.
            clusters = []
            current = []
            for span in spans:
                if current:
                    prev = current[-1]
                    gap = float(span["bbox"][0]) - float(prev["bbox"][2])
                    size = max(float(span.get("size", 10) or 10), float(prev.get("size", 10) or 10))
                    if gap > max(24.0, size * 3.0):
                        clusters.append(current)
                        current = []
                current.append(span)
            if current:
                clusters.append(current)

            for cluster in clusters:
                text = _span_cluster_text(cluster)
                if not text:
                    continue
                boxes = [s["bbox"] for s in cluster]
                bbox = (
                    min(float(b[0]) for b in boxes),
                    min(float(b[1]) for b in boxes),
                    max(float(b[2]) for b in boxes),
                    max(float(b[3]) for b in boxes),
                )
                sizes = [float(s.get("size", 10) or 10) for s in cluster]
                fonts = [str(s.get("font", "")) for s in cluster]
                flags = [int(s.get("flags", 0) or 0) for s in cluster]
                records.append({
                    "text": text,
                    "bbox": bbox,
                    "size": max(sizes),
                    "bold": any((flag & 16) != 0 for flag in flags) or any("bold" in f.lower() for f in fonts),
                })
    return records


def _find_xy_cut(records, axis, page_width, page_height, median_line_height):
    """Find the strongest whitespace cut which cleanly divides the text boxes."""
    start_index = 0
    end_index = 2 if axis == 0 else 3
    extent = page_width if axis == 0 else page_height
    min_gap = max(8.0, page_width * 0.012) if axis == 0 else max(2.0, median_line_height * 0.32)
    ordered = sorted(records, key=lambda r: (r["bbox"][start_index], r["bbox"][end_index]))
    count = len(ordered)
    prefix_max = []
    running_max = -math.inf
    for record in ordered:
        running_max = max(running_max, record["bbox"][end_index])
        prefix_max.append(running_max)

    suffix_min = [math.inf] * count
    running_min = math.inf
    for index in range(count - 1, -1, -1):
        running_min = min(running_min, ordered[index]["bbox"][start_index])
        suffix_min[index] = running_min

    best = None
    for index in range(count - 1):
        prefix_end = prefix_max[index]
        next_start = suffix_min[index + 1]
        gap = next_start - prefix_end
        if gap < min_gap:
            continue

        # With a positive gap between the furthest edge on the first side and
        # the nearest edge on the second, no text box crosses this cut.
        left = ordered[:index + 1]
        right = ordered[index + 1:]

        # Column gutters tend to be a much larger fraction of page width than
        # paragraph leading is of page height. A slight vertical-cut preference
        # helps retain column-wise rather than row-wise reading order.
        score = gap / max(extent, 1.0)
        if axis == 0:
            score *= 1.35
        candidate = (score, gap, left, right)
        if best is None or candidate[0] > best[0]:
            best = candidate
    return best


def _xy_reading_order(records, page_width, page_height, depth=0):
    """Recursively split whitespace to read columns left-to-right, top-to-bottom."""
    if len(records) < 2 or depth > 32:
        return sorted(records, key=lambda r: (r["bbox"][1], r["bbox"][0]))

    heights = [max(1.0, r["bbox"][3] - r["bbox"][1]) for r in records]
    median_height = sorted(heights)[len(heights) // 2]
    vertical_cut = _find_xy_cut(records, 0, page_width, page_height, median_height)
    horizontal_cut = _find_xy_cut(records, 1, page_width, page_height, median_height)

    candidates = []
    if vertical_cut:
        candidates.append((vertical_cut[0], 0, vertical_cut[2], vertical_cut[3]))
    if horizontal_cut:
        candidates.append((horizontal_cut[0], 1, horizontal_cut[2], horizontal_cut[3]))
    if not candidates:
        return sorted(records, key=lambda r: (r["bbox"][1], r["bbox"][0]))

    _, axis, first, second = max(candidates, key=lambda item: item[0])
    # Vertical divisions are output column by column; horizontal divisions are
    # output in upper-then-lower page order.
    return (
        _xy_reading_order(first, page_width, page_height, depth + 1)
        + _xy_reading_order(second, page_width, page_height, depth + 1)
    )


def _remove_running_matter(pages):
    """Remove repeated short lines in page margins and isolated page numbers."""
    occurrences = Counter()
    page_total = len(pages)
    if page_total < 2:
        return pages

    for page in pages:
        height = page["height"]
        seen_on_page = set()
        for record in page["lines"]:
            x0, y0, x1, y1 = record["bbox"]
            text = record["text"]
            key = WHITESPACE.sub(" ", text).strip().casefold()
            if not key or len(key) > 100:
                continue
            if y0 <= height * 0.075 or y1 >= height * 0.925:
                seen_on_page.add(key)
        occurrences.update(seen_on_page)

    threshold = max(2, math.ceil(page_total * 0.25))
    repeated = {key for key, count in occurrences.items() if count >= threshold}
    for page in pages:
        height = page["height"]
        filtered = []
        for record in page["lines"]:
            x0, y0, x1, y1 = record["bbox"]
            key = WHITESPACE.sub(" ", record["text"]).strip().casefold()
            in_margin = y0 <= height * 0.075 or y1 >= height * 0.925
            if in_margin and key in repeated:
                continue
            if in_margin and PAGE_NUMBER.fullmatch(key):
                continue
            filtered.append(record)
        page["lines"] = filtered
    return pages


def _is_list_item(text):
    return bool(LIST_START.match(text.strip()))


def _starts_new_paragraph(previous, current):
    px0, py0, px1, py1 = previous["bbox"]
    cx0, cy0, cx1, cy1 = current["bbox"]
    previous_height = max(1.0, py1 - py0)
    current_height = max(1.0, cy1 - cy0)
    gap = cy0 - py1

    # New column / out-of-order region.
    if gap < -max(2.0, previous_height * 0.25):
        return True

    # A large baseline/paragraph gap or a clear indent change usually marks
    # a new paragraph. Hanging indents after bullets are allowed to continue.
    if gap > max(2.5, min(previous_height, current_height) * 0.58):
        return True
    indent_limit = max(15.0, previous["size"] * (3.0 if _is_list_item(previous["text"]) else 1.8))
    if abs(cx0 - px0) > indent_limit:
        return True

    size_delta = abs(current["size"] - previous["size"])
    if size_delta > max(1.1, previous["size"] * 0.16):
        return True
    if current["bold"] != previous["bold"] and size_delta > 0.2:
        return True
    if _is_list_item(current["text"]):
        return True
    return False


def _join_pdf_lines(ordered_lines):
    paragraphs = []
    current_text = ""
    previous = None

    for record in ordered_lines:
        value = record["text"].strip()
        if not value:
            continue
        if previous is None or _starts_new_paragraph(previous, record):
            if current_text:
                paragraphs.append(current_text)
            current_text = value
        else:
            # Repair words split by end-of-line hyphenation, but only when the
            # following line begins with a lowercase letter.
            if current_text.endswith("-") and value[:1].islower():
                current_text = current_text[:-1] + value
            else:
                current_text += " " + value
        previous = record

    if current_text:
        paragraphs.append(current_text)
    # Keep consecutive list items on separate lines rather than inserting a
    # blank paragraph between every bullet or numbered entry.
    rendered = []
    for paragraph in paragraphs:
        if (
            rendered
            and _is_list_item(paragraph)
            and _is_list_item(rendered[-1].split("\n")[-1])
        ):
            rendered[-1] += "\n" + paragraph
        else:
            rendered.append(paragraph)
    return "\n\n".join(rendered)


def pdf_to_text(pdf_data):
    """Extract a PDF's embedded text, using coordinates to infer reading order.

    This function never calls OCR and never renders pages for text recognition.
    Returns (cleaned_text, embedded_document_title).
    """
    import pymupdf

    document = pymupdf.open(stream=pdf_data, filetype="pdf")
    try:
        metadata = document.metadata or {}
        pdf_title = (metadata.get("title") or "").strip()
        page_records = []
        for page in document:
            rect = page.rect
            page_records.append({
                "width": float(rect.width),
                "height": float(rect.height),
                "lines": _line_records(page),
            })

        _remove_running_matter(page_records)
        page_texts = []
        for page in page_records:
            lines = _xy_reading_order(page["lines"], page["width"], page["height"])
            text = _join_pdf_lines(lines).strip()
            if text:
                page_texts.append(text)
        full_text = "\n\n".join(page_texts)
        full_text = re.sub(r"[ \t]+\n", "\n", full_text)
        full_text = re.sub(r"\n{3,}", "\n\n", full_text).strip()
        return (full_text + "\n" if full_text else ""), pdf_title
    finally:
        document.close()


# ------------------------------- ZIM handling -------------------------------


def _get_entry_function(zim):
    return getattr(zim, "get_entry_by_id", None) or getattr(zim, "_get_entry_by_id", None)


_state = {}


def _init_worker(zim_path, out_dir):
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    from libzim.reader import Archive
    import pymupdf  # noqa: F401 - verify PDF dependency once per worker

    zim = Archive(zim_path)
    _state["zim"] = zim
    _state["get_entry"] = _get_entry_function(zim)
    _state["out_dir"] = Path(out_dir)


def _process_entry(i):
    entry = _state["get_entry"](i)
    if entry.is_redirect:
        return "skipped", 0, False

    item = entry.get_item()
    mimetype = item.mimetype.split(";")[0].strip().lower()
    entry_path = str(entry.path or "")
    is_pdf = mimetype in PDF_TYPES or (
        mimetype in BINARY_TYPES and entry_path.lower().endswith(".pdf")
    )
    output_title = entry.title or entry_path

    if mimetype in HTML_TYPES:
        html = bytes(item.content).decode("utf-8", errors="replace")
        if META_REFRESH.search(html[:3000]):
            return "redirect_pages", 0, False
        text = html_to_text(html)
    elif is_pdf:
        text, pdf_title = pdf_to_text(bytes(item.content))
        if pdf_title:
            output_title = pdf_title
    else:
        return "skipped", 0, False

    if not text:
        return "empty", 0, is_pdf

    size = write_unique(_state["out_dir"], safe_stem(output_title), text)
    return "written", size, is_pdf


def _process_chunk(bounds):
    start, end = bounds
    counts = {
        "written": 0,
        "pdfs": 0,
        "skipped": 0,
        "redirect_pages": 0,
        "empty": 0,
        "failed": 0,
        "bytes": 0,
    }
    consecutive_failures = 0
    for i in range(start, end):
        try:
            kind, size, is_pdf = _process_entry(i)
            counts[kind] += 1
            if is_pdf and kind == "written":
                counts["pdfs"] += 1
            counts["bytes"] += size
            consecutive_failures = 0
        except Exception as exc:
            counts["failed"] += 1
            consecutive_failures += 1
            print(f"Failed on entry #{i}: {ascii(exc)}", file=sys.stderr)
            if consecutive_failures >= 50:
                raise RuntimeError(
                    f"50 entries in a row failed (around entry #{i}). Last error: {ascii(exc)}"
                )
    return end - start, counts


def _fmt_time(seconds):
    return str(datetime.timedelta(seconds=int(seconds)))


def convert(zim_path, out_dir, limit=None, workers=1, chunk_size=CHUNK_SIZE):
    try:
        from libzim.reader import Archive
    except ImportError:
        sys.exit("The 'libzim' package is not installed. Run: python -m pip install libzim")

    try:
        import pymupdf  # noqa: F401
    except ImportError:
        sys.exit("The 'PyMuPDF' package is not installed. Run: python -m pip install pymupdf")

    zim = Archive(str(zim_path))
    if _get_entry_function(zim) is None:
        sys.exit("This libzim version has no get_entry_by_id method. Run: python -m pip install --upgrade libzim")
    total = zim.entry_count
    del zim
    if limit is not None:
        total = min(total, limit)

    if out_dir.exists() and not out_dir.is_dir():
        sys.exit(f"Not a folder: {out_dir}")
    if out_dir.is_dir() and any(out_dir.iterdir()):
        sys.exit(f"Output folder is not empty: {out_dir}\nDelete it or choose a new folder.")
    out_dir.mkdir(parents=True, exist_ok=True)

    chunks = [(start, min(start + chunk_size, total)) for start in range(0, total, chunk_size)]
    totals = {
        "written": 0,
        "pdfs": 0,
        "skipped": 0,
        "redirect_pages": 0,
        "empty": 0,
        "failed": 0,
        "bytes": 0,
    }
    done = 0
    start_time = last_print = time.monotonic()
    print(
        f"Converting {total:,} entries with {workers} worker process(es)...\n"
        "PDF extraction: embedded text only (no OCR); layout-aware reading order"
    )

    try:
        with multiprocessing.Pool(
            processes=workers,
            initializer=_init_worker,
            initargs=(str(zim_path), str(out_dir)),
        ) as pool:
            for count, counts in pool.imap_unordered(_process_chunk, chunks):
                done += count
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
                        f"{totals['written']:,} files ({totals['bytes'] / 1e9:.1f} GB)  "
                        f"{rate:,.0f} entries/s  elapsed {_fmt_time(elapsed)}  "
                        f"rough ETA {_fmt_time(eta)}"
                    )
    except RuntimeError as exc:
        sys.exit(f"Stopping: {exc}")
    except KeyboardInterrupt:
        sys.exit("Interrupted. The output folder holds a partial result; delete it before rerunning.")

    print(
        f"Done in {_fmt_time(time.monotonic() - start_time)}. "
        f"{totals['written']:,} files ({totals['bytes'] / 1e9:.1f} GB of text) written to {out_dir}\n"
        f"PDFs converted: {totals['pdfs']:,}\n"
        f"Skipped (redirects, images, CSS, etc.): {totals['skipped']:,}\n"
        f"Skipped (HTML redirect pages): {totals['redirect_pages']:,}\n"
        f"Skipped (no embedded text): {totals['empty']:,}\n"
        f"Failed: {totals['failed']:,}"
    )


def main():
    parser = argparse.ArgumentParser(
        description="Convert HTML articles and text-based PDFs in a .zim file to individual .txt files (no OCR)."
    )
    parser.add_argument("zim_file", type=Path, help="path to the .zim file")
    parser.add_argument(
        "output_folder",
        type=Path,
        nargs="?",
        help="new or empty output folder (default: .zim filename without its extension)",
    )
    parser.add_argument("--limit", type=int, help="only process the first N entries (for testing)")
    parser.add_argument(
        "--workers",
        type=int,
        default=os.cpu_count() or 1,
        help="number of worker processes (default: all logical processors)",
    )
    args = parser.parse_args()

    if not args.zim_file.is_file():
        sys.exit(f"File not found: {args.zim_file}")
    if args.workers < 1:
        sys.exit("--workers must be at least 1")
    if args.limit is not None and args.limit < 1:
        sys.exit("--limit must be at least 1")

    out_dir = args.output_folder or args.zim_file.with_suffix("")
    convert(args.zim_file, out_dir, args.limit, args.workers)


if __name__ == "__main__":
    main()
