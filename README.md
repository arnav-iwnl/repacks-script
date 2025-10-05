# Selenium Download Button Clicker for FUCKINGFAST.co

Automate clicking “Download” buttons across one or more pages using Selenium + Chrome.

This script can:
- Scrape a main page for candidate links
- Optionally read a text file of URLs (one per line)
- Heuristically find and click the download button on each page
- Detect when a download actually starts and completes (.crdownload/.part/.tmp handling and stable file-size checks)
- Skip files that already exist (including duplicate suffixes like "(1)")
- Periodically refresh the browser session to avoid stale/expired sessions
- Show a nice progress bar and a final summary using Rich

The core script is `selenium_downloader_fixed.py`.


## Requirements

- Python 3.9+
- Google Chrome installed (the script uses `webdriver-manager` to fetch a matching ChromeDriver automatically)

Python dependencies:

```bash
pip install selenium webdriver-manager rich rarfile requests
```

Notes:
- `rarfile` is imported by the script (planned for optional extraction). Even though extraction is currently commented out, the import is required.
- `requests` is optional; if available, the script can use a HEAD request to better infer filenames.


## Quick start

```bash
# 1) Create and activate a virtualenv (recommended)
python3 -m venv .venv
source .venv/bin/activate
pip install -U pip

# 2) Install dependencies
pip install selenium webdriver-manager rich rarfile requests

# 3) Run: scrape a main page for links and download
python selenium_downloader_fixed.py \
  --output ~/Downloads/my-files \
  --url https://example.com/page-with-links \
  --headless \
  --filter-downloads \
  --max-wait 30
```

If you already have a list of direct/download-page URLs in a text file:

```bash
python selenium_downloader_fixed.py \
  --output ~/Downloads/my-files \
  --input-txt urls.txt \
  --headless
```

You can provide both `--url` and `--input-txt`; links from both sources are combined and deduplicated.


## CLI reference

```bash
python selenium_downloader_fixed.py --help
```

Options (with defaults):

- `--output <path>` (required): Directory where files will be downloaded.
- `--url <url>`: Main page to open and scrape for candidate links.
- `--input-txt <path>`: Path to a `.txt` file with URLs (one per line; `#` comments allowed).
- `--headless` (default: off): Run Chrome headless. Omit for a visible browser window.
- `--no-image-block` (default: off): Do not block images. By default, the driver blocks images to speed up loading; use this flag if the site needs images to render correctly.
- `--max-wait <seconds>` (default: 20): Maximum wait for elements to appear.
- `--session-refresh <n>` (default: 10): Restart the browser after this many successful downloads to avoid stale sessions.
- `--delay-between <seconds>` (default: 2.0): Delay between processing successive links.
- `--filter-downloads` (default: off): After scraping, keep links that look like download pages (simple heuristics like “download”, “dl”, etc.).


## How it works

1) The script sets up a Chrome driver with sensible defaults:
   - Optional headless mode
   - Image blocking by default (toggle with `--no-image-block`)
   - A designated download folder
2) It gathers links from:
   - The main page (`--url`) using multiple fallbacks to find anchors
   - An optional text file (`--input-txt`)
3) For each link, it opens the page and tries several strategies to find and click a likely download button (JS click, ActionChains, direct click).
4) It waits until a new file appears in the download directory and verifies completion by watching for temporary extensions to disappear and the file size to stabilize.
5) It skips files that already exist (including duplicate patterns like `name (1).ext`).
6) It presents progress and a final result summary.


## Examples

Example I used: 
```bash
python selenium_downloader_fixed.py --input-txt pastebin.txt --out "D:\Downloads" --headless --session-refresh 5
```

Basic scrape + download (headless):

```bash
python selenium_downloader_fixed.py \
  --output ~/Downloads/repacks \
  --url https://example.com/listing \
  --headless \
  --filter-downloads
```

Use a prepared list of URLs (one per line):

```bash
python selenium_downloader_fixed.py \
  --output ~/Downloads/repacks \
  --input-txt urls.txt \
  --headless
```

Debug a problematic site with a visible browser and images enabled:

```bash
python selenium_downloader_fixed.py \
  --output ~/Downloads/debug \
  --url https://example.com/page \
  --no-image-block \
  --max-wait 40
```


## Tips and troubleshooting

- Chrome not found / driver fails to start:
  - Ensure Google Chrome is installed and up to date. `webdriver-manager` will match the driver automatically.
- Nothing clicks / button not found:
  - Try without `--headless` and with `--no-image-block` so the site renders fully.
  - Increase `--max-wait`.
- Download never completes (.crdownload stays):
  - Check disk space and network. Some sites require manual captcha or additional clicks.
  - Try running non-headless to observe behavior.
- Files are “skipped” immediately:
  - The script detected an existing file matching the target (including duplicates like `name (1).ext`). Remove or move existing files if you want to re-download.
- Running in CI/containers:
  - The driver is started with flags like `--no-sandbox` and `--disable-dev-shm-usage` to improve compatibility in constrained environments.


## Development

- Main entry point: `selenium_downloader_fixed.py`
- Style: standard Python; no external build system.
- Optional future work: enable the commented-out `.rar` extraction flow (already scaffolded via `rarfile`).


## Legal and ethical use

Use this tool only for content you have the legal right to download. Respect website terms of service, robots.txt, and applicable laws in your jurisdiction.


## License

No license specified. If you plan to publish or distribute, add an appropriate `LICENSE` file.
