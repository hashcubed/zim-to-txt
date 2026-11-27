# zim-to-txt

Images are not supported at the moment.

Two separate Python converters for turning a `.zim` archive (for example, a Kiwix dump) into individual plain-text files. Both write one `.txt` file per supported entry into a single output folder and support multiprocessing.

There are two versions to choose from:

| Script | Purpose | PDF support |
|---|---|---|
| [`zim_to_txt.py`](zim_to_txt.py) | Original standard converter. Converts HTML articles only. | No |
| [`zim_to_txt_with_pdf.py`](zim_to_txt_with_pdf.py) | Enhanced converter. Converts HTML articles and text-based PDFs, with layout-aware PDF text extraction. | Yes — embedded text only, no OCR |

The two scripts are independent. The original `zim_to_txt.py` remains unchanged; the PDF-enabled functionality is in `zim_to_txt_with_pdf.py`.

## Requirements

- Python 3.8 or newer for the original standard converter.
- `libzim` for both converters.
- `PyMuPDF` (`pymupdf`) for the enhanced PDF-enabled converter.
- Use a Python version supported by your installed PyMuPDF release. A current Python release is recommended.

## Install

For the original standard converter:

```powershell
python -m pip install --upgrade libzim
```

For the enhanced PDF-enabled converter:

```powershell
python -m pip install --upgrade libzim pymupdf
```

`libzim` provides the Python bindings used to open and read ZIM archives. `PyMuPDF` reads embedded PDF text, text positions, font information and page geometry. It does not perform OCR.

## Usage

Both scripts use the same command-line structure:

```text
python <script> <zim_file> [output_folder] [--workers N] [--limit N]
```

### Original standard converter — `zim_to_txt.py`

Converts HTML articles (`text/html` and `application/xhtml+xml`) only. PDF files and other non-HTML entries are skipped.

```powershell
python zim_to_txt.py wikipedia.zim
python zim_to_txt.py wikipedia.zim output
python zim_to_txt.py wikipedia.zim output --workers 6
python zim_to_txt.py wikipedia.zim test_output --limit 2000
```

### Enhanced PDF-enabled converter — `zim_to_txt_with_pdf.py`

Converts HTML articles and PDFs that contain embedded, selectable text. PDF extraction uses page geometry and text positions to improve reading order and paragraph reconstruction. OCR is never used.

```powershell
python zim_to_txt_with_pdf.py wikipedia.zim
python zim_to_txt_with_pdf.py wikipedia.zim output
python zim_to_txt_with_pdf.py wikipedia.zim output --workers 6
python zim_to_txt_with_pdf.py wikipedia.zim test_output --limit 2000
```

For example, to convert the medicine archive with the enhanced version:

```powershell
python zim_to_txt_with_pdf.py zimgit-medicine_en_2024-08.zim --workers 12
```

This creates a `zimgit-medicine_en_2024-08` folder beside the archive by default.

### Command-line arguments

| Argument | Description |
|---|---|
| `zim_file` | Path to the `.zim` file. |
| `output_folder` | Optional new or empty folder for the `.txt` files. Default: a folder named after the ZIM file, without its `.zim` extension, next to the archive. The script exits if the folder already contains files. |
| `--workers N` | Number of worker processes. Default: all logical processors. |
| `--limit N` | Process only the first N archive entries. Use this to inspect a sample before a full run. |

Use a different output folder, or empty the previous one, before running the other converter. Neither script writes into a non-empty output folder.

## Output

Both converters:

- Write one UTF-8 `.txt` file per successfully converted entry.
- Use safe filenames based on the article or document title.
- Replace characters that are invalid in Windows filenames with `_`.
- Remove trailing dots and spaces and prefix reserved Windows names (`CON`, `PRN`, `AUX`, `NUL`, `COM1`-`COM9`, `LPT1`-`LPT9`) with `_`.
- Truncate names to 150 UTF-8 bytes.
- Add `_2`, `_3` and so on when output names collide, including names that differ only by case. Which entry receives the plain name can vary between runs if titles collide.

For PDFs, the enhanced converter uses the embedded PDF document title as the output filename when available. Otherwise, it falls back to the ZIM entry title or path. An empty title ultimately falls back to `untitled`.

## HTML text conversion

Both scripts convert HTML to plain text:

- Content inside `<head>`, `<script>`, `<style>`, `<noscript>`, `<template>` and `<svg>` elements is removed.
- Block elements such as paragraphs, headings, list items and table rows are separated onto new lines.
- HTML entities are decoded.
- Whitespace is collapsed and empty lines are removed.
- Images, links and visual formatting are not included in the output.

## PDF text conversion (enhanced script only)

`zim_to_txt_with_pdf.py` extracts text already embedded in PDFs using PyMuPDF. It does not run OCR, render pages for text recognition or require an OCR engine.

The PDF extractor uses text positions and page geometry to estimate reading order. Its layout-cleaning steps include:

- **Column ordering:** identifies clear whitespace divisions and attempts to read columns from left to right, with text flowing from top to bottom within each region.
- **Paragraph reconstruction:** joins lines that appear to belong to the same paragraph, using spacing, indentation and font information.
- **Hyphenation repair:** reconnects words split by a line-end hyphen when the next line appears to continue the word.
- **Running headers and footers:** removes short, repeated text found in the top or bottom margins across pages.
- **Page numbers:** removes isolated page numbers found in page margins.
- **Lists:** keeps recognised bullet and numbered list items on separate lines.
- **Page separation:** separates text from consecutive pages with a blank line.

These are layout heuristics, not a perfect reconstruction of the original page. Complex tables, sidebars, footnotes, unusual text positioning and mixed layouts may still need manual checking. Images, diagrams and other non-text content are not converted.

**Scanned or image-only PDFs:** If a page contains only an image and has no embedded text, there is no text for the converter to extract. Such pages produce no text. OCR is deliberately not included. In PDFs containing both text and scanned pages, only the embedded text is extracted.

## What is converted and skipped

The original standard converter processes HTML entries only. The enhanced converter processes HTML and PDF entries, including entries labelled as generic binary data when their ZIM path ends in `.pdf`.

Both scripts skip and count these separately in the final summary:

- Redirect entries.
- HTML redirect pages (pages containing a `<meta http-equiv="refresh">` forward).
- Unsupported non-HTML entries, such as images, CSS and JavaScript. The standard converter also skips PDFs.
- Entries that produce no extractable text.

## Progress and errors

Every 10 seconds, each script prints progress including the number of entries processed, files written, processing rate, elapsed time and approximate ETA. For example:

```text
 12.3%  2,420,000/19,707,079 entries  512,345 files (4.2 GB)  3,400 entries/s  elapsed 0:12:30  rough ETA 1:25:00
```

The enhanced converter also reports the number of PDFs converted in its final summary.

- The ETA is approximate because processing speed varies across the archive.
- An entry that fails is reported on stderr and counted. The run continues.
- If 50 consecutive entries fail, the run stops with the last error.
- Ctrl+C stops the run. The output folder contains a partial result; delete it before starting again.

## Notes

- Most entries in a full Wikipedia ZIM are redirects, so the number of `.txt` files can be much lower than the total entry count.
- A single folder containing millions of files can be slow to open in a file browser. Use command-line tools or search to work with it.
- Writing millions of small files is often limited by disk speed and antivirus scanning. On Windows, excluding the output folder from real-time scanning can speed up writing.
- If the machine becomes unresponsive during a run, lower `--workers`.
- For the enhanced converter, inspect sample PDF output before running a full archive conversion or relying on the extracted text for important work.

## Inspecting a ZIM

`zim_inspect.py` prints a table of entries (ID, redirect status, type, size, number of text lines, path and title) and saves the raw HTML of the first entry that converts to one line of text or less. Keep it in the same folder as the converter scripts.

```powershell
python zim_inspect.py wikipedia.zim [start] [count]
```

`start` defaults to `0` and `count` defaults to `60`. The raw HTML is saved to `inspect_sample.html` in the current folder.

## Files

| File | Purpose |
|---|---|
| `zim_to_txt.py` | Original standard converter. Converts HTML articles only; requires `libzim`. |
| `zim_to_txt_with_pdf.py` | Enhanced converter. Converts HTML articles and text-based PDFs with layout-aware, no-OCR PDF extraction; requires `libzim` and `pymupdf`. |
| `zim_inspect.py` | Inspects entries in a ZIM archive and saves a sample of raw HTML. |
