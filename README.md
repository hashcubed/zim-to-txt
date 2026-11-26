# zim-to-txt

Converts a `.zim` archive (for example a Kiwix Wikipedia dump) into plain text: one `.txt` file per article, all in a single folder. Conversion runs in one pass and is spread across multiple worker processes.

## Requirements

- Python 3.8 or newer
- `libzim`

## Install

This installs one package:

- `libzim`: the official Python bindings for the libzim library, which opens and reads ZIM archives.

Everything else the scripts use is in the Python standard library.

```
pip install libzim
```

## Usage

```
python zim_to_txt.py <zim_file> [output_folder] [--workers N] [--limit N]
```

```
python zim_to_txt.py wikipedia.zim
python zim_to_txt.py wikipedia.zim output
python zim_to_txt.py wikipedia.zim output --workers 6
python zim_to_txt.py wikipedia.zim test_output --limit 2000
```

| Argument | Description |
|---|---|
| `zim_file` | Path to the `.zim` file. |
| `output_folder` | New or empty folder for the `.txt` files. Default: a folder named after the ZIM, next to it. The script exits if the folder already contains files. |
| `--workers N` | Number of worker processes. Default: all logical processors. |
| `--limit N` | Process only the first N entries. Use it to check the output before a full run. |

## Output

- One UTF-8 `.txt` file per article, named after the article title.
- Plain text only: no images, links or formatting. Infoboxes, tables and navigation boxes are included as plain lines of text.

### What is converted

Only HTML entries (`text/html`, `application/xhtml+xml`) are converted. These are skipped and counted separately in the final summary:

- Redirect entries
- HTML redirect pages (pages whose only content is a `<meta http-equiv="refresh">` forward)
- Non-HTML entries such as images and CSS
- Articles that contain no text

### Text conversion

- `<head>`, `<script>`, `<style>`, `<noscript>`, `<template>` and `<svg>` content is removed.
- Each block element (paragraph, heading, list item, table row and so on) is written on its own line.
- HTML entities are decoded, whitespace is collapsed and blank lines are removed.

### File names

- Characters not allowed in Windows file names (`< > : " / \ | ? *` and control characters) are replaced with `_`.
- Trailing dots and spaces are removed.
- Reserved Windows names (`CON`, `PRN`, `AUX`, `NUL`, `COM1`-`COM9`, `LPT1`-`LPT9`) get a `_` prefix.
- Names are truncated to 150 bytes. An empty title falls back to the entry path, and then to `untitled`.
- Duplicate names, including names that differ only by case, get `_2`, `_3` and so on. Which article keeps the plain name can vary between runs.

## Progress and errors

Every 10 seconds the script prints progress:

```
 12.3%  2,420,000/19,707,079 entries  512,345 files (4.2 GB)  3,400 entries/s  elapsed 0:12:30  rough ETA 1:25:00
```

The ETA is approximate because entry processing speed varies across the archive.

- An entry that fails is reported on stderr and counted. The run continues.
- If 50 consecutive entries fail, the run stops with the last error.
- Ctrl+C stops the run. Delete the partial output folder before starting again.

When finished, the script prints the number of files written and the skip and failure counts.

## Notes

- Most entries in a full Wikipedia ZIM are redirects, so the number of `.txt` files is much lower than the entry count.
- A single folder with millions of files is slow to open in a file browser. Use command-line tools or search to work with it.
- Writing millions of small files is often limited by disk and antivirus scanning. On Windows, excluding the output folder from real-time scanning can speed up writing.
- If the machine becomes unresponsive during a run, lower `--workers`.

## Files

| File | Purpose |
|---|---|
| `zim_to_txt.py` | The converter. |
