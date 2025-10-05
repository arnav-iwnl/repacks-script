"""
Selenium Download Button Clicker (fixed + CLI)

Usage:
    python selenium_downloader_fixed.py --url <URL> --out "D:/Downloads/MyFolder" --headless --session-refresh 10

Features / fixes applied:
 - Added argparse command-line interface for URL, output dir, timeouts, headless toggle, and more.
 - Fixed ChromeOptions mistakes (removed invalid assignment to add_extension, cleaned args).
 - More robust selectors and fallbacks when scraping links and finding download buttons.
 - Improved download-detection: waits for .crdownload/.part/.tmp to appear and then disappear, and ensures stable file size.
 - Handles session expiration by restarting driver automatically.
 - Better error handling and logging through rich Console.
 - Keeps headless optional and allows --no-headless for debugging.
 - Ensures DOWNLOAD_DIR exists and uses absolute paths.

Note: Keep Chrome and chromedriver reasonably up to date. If "headless" causes issues on your platform, run without --headless.

"""
import rarfile
import shutil
import argparse
import time
import os
import re
from pathlib import Path
from typing import List, Optional
from rich.console import Console
from rich.progress import Progress, BarColumn, TextColumn, TimeElapsedColumn
from rich.panel import Panel
from rich.table import Table
from rich import box
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from webdriver_manager.chrome import ChromeDriverManager
from typing import Optional
from urllib.parse import urlparse, parse_qs, unquote, unquote_plus


console = Console()

# Default constants
DEFAULT_MAX_WAIT = 20
DEFAULT_DOWNLOAD_WAIT = 30
DEFAULT_SESSION_REFRESH = 10


def setup_driver(download_dir: Path, headless: bool = True, disable_images: bool = True) -> webdriver.Chrome:
    """Initialize Chrome driver with improved preferences and safe defaults."""
    options = webdriver.ChromeOptions()

    prefs = {
        "download.default_directory": str(download_dir),
        "download.prompt_for_download": False,
        "download.directory_upgrade": True,
        "profile.default_content_setting_values.notifications": 2,
        "profile.default_content_settings.popups": 0,
        # allow automatic downloads for some sites (useful when multiple files)
        "profile.content_settings.exceptions.automatic_downloads.*.setting": 1,
    }
    options.add_experimental_option("prefs", prefs)

    # Common recommended args
    if headless:
        options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--disable-extensions")
    options.add_experimental_option('excludeSwitches', ['enable-logging'])

    # Optionally block images to speed up page loads
    if disable_images:
        prefs["profile.managed_default_content_settings.images"] = 2

    try:
        driver = webdriver.Chrome(
            service=Service(ChromeDriverManager().install()),
            options=options,
        )

        # try to inject a small overlay-cleaner; ignore failures
        try:
            driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
                "source": """
                    window.__REMOVE_OVERLAYS_INTERVAL = setInterval(function() {
                        try {
                            const selectors = [
                                'div[style*="z-index: 2147483647"]',
                                'div[style*="position: fixed"][style*="cursor: pointer"]',
                                'iframe[src*="ad"]',
                                '.ad-overlay',
                                '#ad-overlay'
                            ];
                            selectors.forEach(sel => document.querySelectorAll(sel).forEach(el => el.remove()));
                        } catch(e) {}
                    }, 1000);
                """
            })
        except Exception:
            # not critical; continue
            pass

        return driver

    except Exception as e:
        console.print(f"[red]❌ Failed to initialize Chrome driver: {e}[/red]")
        raise


def scrape_links(driver: webdriver.Chrome, url: str, wait_time: int = DEFAULT_MAX_WAIT) -> List[str]:
    """Scrape download-page links from the main page.

    Tries multiple fallback selectors to collect hrefs found inside article-like containers.
    """
    links: List[str] = []
    console.print("[cyan]🌐 Opening main page...[/cyan]")
    driver.get(url)

    wait = WebDriverWait(driver, wait_time)
    try:
        # Wait for something that looks like content; broad fallback
        wait.until(EC.presence_of_element_located((By.TAG_NAME, "body")))
        time.sleep(1)

        # Prefer article-like blocks but fall back to generic anchors
        article_selectors = ["article", ".post", ".entry", ".paste-body", ".content"]
        anchors = []
        for sel in article_selectors:
            try:
                articles = driver.find_elements(By.CSS_SELECTOR, sel)
                if not articles:
                    continue
                for a in articles:
                    anchors.extend(a.find_elements(By.CSS_SELECTOR, "a[href]"))
                if anchors:
                    break
            except Exception:
                continue

        # final fallback: grab all anchors on page
        if not anchors:
            anchors = driver.find_elements(By.CSS_SELECTOR, "a[href]")

        for a in anchors:
            try:
                href = a.get_attribute("href")
                if href and href.startswith("http"):
                    links.append(href)
            except Exception:
                continue

        # Keep order unique
        seen = set()
        unique = []
        for l in links:
            if l not in seen:
                seen.add(l)
                unique.append(l)
        links = unique

        console.print(f"[green]📄 Found {len(links)} links (raw).[/green]")

    except Exception as e:
        console.print(f"[red]❌ Error during scraping: {e}[/red]")
        raise

    return links


def get_incomplete_files(download_dir: Path) -> List[str]:
    files = []
    try:
        for f in os.listdir(download_dir):
            if f.endswith(('.part', '.tmp')):
                files.append(f)
    except Exception:
        pass
    return files


def wait_for_download_complete(download_dir: Path, before_files: set, target_url: Optional[str] = None,
                               timeout: int = 150, stable_checks: int = 3) -> Optional[str]:
    """
    Wait for Chrome to finish a download or detect an already-completed file.
    Also supports Chrome duplicate naming (Forza (1).rar, etc.)
    """
    start = time.time()

    # Pre-check: already complete?
    if target_url:
        existing = check_file_exists(download_dir, target_url)
        if existing:
            console.print(f"[yellow]⏭️ Already downloaded earlier: {existing}[/yellow]")
            return existing

    while time.time() - start < timeout:
        try:
            current = set(os.listdir(download_dir))
        except Exception:
            current = set()

        new_files = current - before_files

        # Wait for any temporary files to disappear
        incomplete = [f for f in new_files if f.endswith(('.crdownload', '.part', '.tmp'))]
        if incomplete:
            time.sleep(1.5)
            continue

        # Completed file detected
        if new_files:
            for fn in list(new_files):
                fp = download_dir / fn
                if not fp.exists():
                    continue
                size = os.path.getsize(fp)
                stable = True
                for _ in range(stable_checks):
                    time.sleep(1)
                    try:
                        new_size = os.path.getsize(fp)
                    except Exception:
                        new_size = size
                    if new_size != size:
                        stable = False
                        size = new_size
                        break
                if stable:
                    return fn

        # Also check for previously completed version mid-loop
        if target_url:
            existing = check_file_exists(download_dir, target_url)
            if existing:
                return existing

        time.sleep(1)

    return None



def get_filename_from_url(url: str) -> Optional[str]:
    """
    Robust filename extractor:
      1) Uses the URL path segment (last path part)
      2) Falls back to common query params (file, filename, name, etc.)
      3) Falls back to fragment (rare)
      4) As last resort tries a HEAD request and parses Content-Disposition header
    Returns: filename with extension (e.g. 'game.rar') or None if not determinable.
    """
    if not url:
        return None

    try:
        url = url.strip()
        parsed = urlparse(url)

        # 1) Path-based filename (most common)
        path = parsed.path or ""
        if path:
            candidate = unquote(path.split("/")[-1] or "")
            candidate = candidate.split("#")[0].split("?")[0].strip()
            if candidate and "." in candidate and not candidate.endswith(("/", "\\")):
                return candidate

        # 2) Query-string parameters (file=, filename=, name=, etc.)
        qs = parse_qs(parsed.query or "")
        for key in ("file", "filename", "name", "attachment", "download", "title"):
            if key in qs and qs[key]:
                candidate = unquote_plus(qs[key][0]).strip()
                if candidate and "." in candidate:
                    return candidate

        # 3) Fragment (rare)
        frag = parsed.fragment or ""
        if frag and "." in frag:
            frag_candidate = unquote(frag.split("/")[-1]).strip()
            if frag_candidate and "." in frag_candidate:
                return frag_candidate

        # 4) HEAD request -> Content-Disposition (last resort; optional)
        try:
            import requests
            head = requests.head(url, allow_redirects=True, timeout=5)
            cd = head.headers.get("content-disposition")
            if cd:
                # filename*=UTF-8''%e2%82%ac%20rates  or filename="name.ext"
                m = re.search(r"filename\*\s*=\s*([^;]+)", cd, flags=re.I)
                if m:
                    fname = m.group(1).strip().strip("\"'")
                    # handle RFC5987 (e.g. UTF-8''... percent-encoded)
                    if "''" in fname:
                        try:
                            fname = unquote(fname.split("''", 1)[1])
                        except Exception:
                            pass
                    fname = unquote(fname)
                    if "." in fname:
                        return fname

                m2 = re.search(r'filename\s*=\s*"?(?P<name>[^\";]+)"?', cd, flags=re.I)
                if m2:
                    fname = m2.group("name").strip().strip("\"'")
                    fname = unquote(fname)
                    if "." in fname:
                        return fname
        except Exception:
            # requests missing or HEAD failed — that's OK, just give up gracefully
            pass

    except Exception:
        pass

    return None



# def check_file_exists(download_dir, link):
#     """
#     Detect if the target file (e.g., .rar) or its partial download (.crdownload/.part/.tmp)
#     already exists in the download directory.

#     Handles duplicate naming like file (1).rar, file (1).rar.crdownload, etc.

#     Returns:
#         - The matching filename if found
#         - None if no match exists
#     """
#     filename = get_filename_from_url(link)
#     if not filename:
#         return None

#     base_name, ext = os.path.splitext(filename)
#     print(base_name, ext)
#     if not ext:
#         return None

#     # Create list of possible patterns (e.g., file.rar, file (1).rar, file.rar.crdownload, file (1).rar.crdownload)
#     candidates = []
#     for i in range(0, 10):  # check up to 10 duplicate variants
#         suffix = f" ({i})" if i > 0 else ""
#         candidates.append(f"{base_name}{suffix}{ext}")  # completed file
#         candidates.append(f"{base_name}{suffix}.rar")  # completed file
#         candidates.append(f"{base_name}{suffix}{ext}.crdownload")  # Chrome in-progress
#         candidates.append(f"{base_name}{suffix}.rar.crdownload")  # Chrome in-progress
#         candidates.append(f"{base_name}{suffix}{ext}.part")  # Firefox or wget
#         candidates.append(f"{base_name}{suffix}{ext}.tmp")  # generic temp

#     for cand in candidates:
#         file_path = Path(download_dir) / cand
#         if file_path.exists():
#             return cand

#     return None

def check_file_exists(download_dir: Path, url: str) -> Optional[str]:
    """
    Detect if the target (completed or in-progress) file already exists.
    Handles:
      - exact names (game.rar)
      - browser temp variants (game.rar.crdownload, game.rar.part, game.rar.tmp)
      - duplicate suffixes added by browser (game (1).rar, game (1).rar.crdownload)
    Returns the matching filename found in the folder (string) or None.
    """
    try:
        hint = get_filename_from_url(url)
        if not hint:
            return None

        # Normalize
        hint = hint.strip()
        # print(f'THis is hint going thru check file exitis{hint}')
        base, ext = os.path.splitext(hint)
        # print(f'THis is base going thru check file exitis{base}')
        if not ext:
            return None

        # Build a regex to match:
        #   - exact "base.ext"
        #   - "base (1).ext", "base (2).ext", etc.
        #   - those + temp extensions like .crdownload/.part/.tmp appended
        # Use case-insensitive matching
        # escape base for regex but allow spaces/percent-encodings etc in actual filenames
        esc_base = re.escape(base)
        pattern = re.compile(rf"^{esc_base}(?:\s*\(\d+\))?{re.escape(ext)}(?:\.crdownload|\.part|\.tmp)?$", flags=re.I)

        for fname in os.listdir(download_dir):
            if pattern.match(fname):
                return fname

        # As a fallback, allow base to appear anywhere in the stem (helps with slight variations)
        # but still require same extension or a known temp extension.
        fallback_exts = [ext, ext + ".crdownload", ext + ".part", ext + ".tmp"]
        for fname in os.listdir(download_dir):
            lf = fname.lower()
            for fe in fallback_exts:
                if lf.endswith(fe.lower()):
                    stem = lf[: -len(fe)]
                    # remove trailing ' (1)' etc for comparison
                    stem_clean = re.sub(r"\s*\(\d+\)$", "", stem).strip()
                    if base.lower() in stem_clean:
                        return fname

        return None

    except Exception:
        return None


def find_download_button(driver: webdriver.Chrome, wait: WebDriverWait):
    # Try several sensible selectors (both CSS and XPath)
    selectors = [
        (By.CSS_SELECTOR, "button.gay-button"),
        (By.CSS_SELECTOR, "button.link-button"),
        (By.CSS_SELECTOR, "button[class*='gay-button']"),
        (By.XPATH, "//button[contains(@class, 'gay-button') or contains(., 'DOWNLOAD') or contains(., 'Download') or contains(., 'download')]") ,
        (By.CSS_SELECTOR, "a[href][class*='download']"),
        (By.CSS_SELECTOR, "a[href*='download']"),
        (By.XPATH, "//a[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'),'download')]")
    ]

    for by, sel in selectors:
        try:
            el = wait.until(EC.element_to_be_clickable((by, sel)))
            return el
        except Exception:
            continue
    return None


def click_download_button(driver: webdriver.Chrome, page_url: str, download_dir: Path, max_wait: int = DEFAULT_MAX_WAIT) -> object:
    """Open page_url, attempt to click download and wait for completion.

    Returns True on success, False on failure, or 'SESSION_EXPIRED' if session needs restart.
    """
    try:
        # ensure session alive
        try:
            _ = driver.current_url
        except Exception:
            return "SESSION_EXPIRED"

        console.print("  [cyan]🔗 Opening page...[/cyan]")
        driver.get(page_url)
        wait = WebDriverWait(driver, max_wait)
        time.sleep(1)

        # attempt to remove obvious overlays
        try:
            overlays = driver.find_elements(By.CSS_SELECTOR, "div[style*='z-index'][style*='fixed']")
            for o in overlays:
                try:
                    driver.execute_script("arguments[0].remove();", o)
                except Exception:
                    pass
        except Exception:
            pass

        btn = find_download_button(driver, wait)
        if not btn:
            console.print("  [red]❌ Could not find download button[/red]")
            return False

        files_before = set(os.listdir(download_dir))
        console.print("  [cyan]🖱️ Clicking download button...[/cyan]")

        clicked = False
        try:
            driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", btn)
            time.sleep(0.5)
            driver.execute_script("arguments[0].click();", btn)
            clicked = True
        except Exception:
            pass

        if not clicked:
            try:
                from selenium.webdriver.common.action_chains import ActionChains
                actions = ActionChains(driver)
                actions.move_to_element(btn).click().perform()
                clicked = True
            except Exception:
                pass

        if not clicked:
            try:
                btn.click()
                clicked = True
            except Exception as e:
                console.print(f"  [red]❌ Click failed: {e}[/red]")
                return False

        # Wait for new file to appear & complete
        completed = wait_for_download_complete(download_dir, files_before, target_url=page_url, timeout=150)


        if completed:
            console.print(f"  [green]✅ Downloaded: {completed}[/green]")
            # Auto-extract if it's a rar
            # completed_path = download_dir / completed
            # if completed_path.suffix.lower() == ".rar":
            #      extract_dir = download_dir / "extracted"
            # if extract_rar(completed_path, extract_dir, overwrite=True):
            #     console.print(f"  [green]📂 Extracted to: {extract_dir}[/green]")
            # else:
            #     console.print(f"  [yellow]⚠️ Extraction failed for: {completed}[/yellow]")
            # return True

        console.print("  [yellow]⚠️ Download timeout or didn't start[/yellow]")
        return False

    except Exception as e:
        console.print(f"  [red]❌ Error: {e}[/red]")
        if "invalid session id" in str(e).lower() or "session" in str(e).lower():
            return "SESSION_EXPIRED"
        return False

def read_urls_from_txt(file_path: str) -> list[str]:
    """Read non-empty, non-comment lines from a text file."""
    urls = []
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    urls.append(line)
        console.print(f"[cyan]📄 Loaded {len(urls)} URLs from {file_path}[/cyan]")
    except FileNotFoundError:
        console.print(f"[red]⚠️ File not found: {file_path}[/red]")
    except Exception as e:
        console.print(f"[red]Error reading {file_path}: {e}[/red]")
    return urls

# def extract_rar(file_path: Path, extract_dir: Path, overwrite: bool = True) -> bool:
#     """
#     Extracts a .rar file to extract_dir.
#     overwrite=True will replace existing files.
#     Returns True if extraction succeeds, False otherwise.
#     """
#     try:
#         extract_dir.mkdir(parents=True, exist_ok=True)
#         rf = rarfile.RarFile(file_path)
#         for member in rf.infolist():
#             target_path = extract_dir / member.filename
#             if target_path.exists() and overwrite:
#                 if target_path.is_file():
#                     target_path.unlink()
#                 elif target_path.is_dir():
#                     shutil.rmtree(target_path)
#             rf.extract(member, path=extract_dir)
#         rf.close()
#         return True
#     except Exception as e:
#         console.print(f"[red]❌ Failed to extract {file_path.name}: {e}[/red]")
#         return False


def run(args):
    download_dir = Path(args.output).expanduser().absolute()
    download_dir.mkdir(parents=True, exist_ok=True)

    driver = None
    try:
        driver = setup_driver(download_dir, headless=args.headless, disable_images=not args.no_image_block)
        links = []

# 1️⃣ If input-txt provided, load URLs from it
        if args.input_txt:
            links.extend(read_urls_from_txt(args.input_txt))

# 2️⃣ If --url also provided, scrape that page for links
        if args.url:
            console.print(f"[cyan]🌐 Scraping main page: {args.url}[/cyan]")
            page_links = scrape_links(driver, args.url, wait_time=args.max_wait)
            links.extend(page_links)

# Deduplicate
        links = list(dict.fromkeys(links))

        # optionally filter to plausible download pages (simple heuristic)
        # If user set --filter-downloads, try to keep only links containing 'download' or 'dl'
        if args.filter_downloads:
            filtered = [l for l in links if any(k in l.lower() for k in ("download", "dl", "fitgirl", "torrent", "drive"))]
            if filtered:
                links = filtered

        if not links:
            console.print("[red]❌ No links found![/red]")
            return

        console.print(f"\n[green]📋 Found {len(links)} candidate links[/green]")
        console.print(f"[cyan]📁 Download directory: {download_dir}[/cyan]")

        successful = 0
        failed = 0
        skipped = 0
        downloads_since_refresh = 0

        with Progress(
            TextColumn("[bold blue]{task.description}"),
            BarColumn(bar_width=40, style="cyan", complete_style="green"),
            TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
            TextColumn("•"),
            TextColumn("[cyan]{task.completed}/{task.total}"),
            TextColumn("•"),
            TextColumn("[green]✅  {task.fields[success]}"),
            TextColumn("[yellow]⏭️  {task.fields[skipped]}"),
            TextColumn("[red]❌ {task.fields[failed]}"),
            console=console,
        ) as progress:
            main_task = progress.add_task("[cyan]Overall Progress", total=len(links), success=0, skipped=0, failed=0)

            for idx, link in enumerate(links, 1):
                console.print(f"\n[bold][dim]{'─'*60}[/dim][/bold]")
                filename = get_filename_from_url(link) or link[:60]
                console.print(f"[bold cyan][{idx}/{len(links)}][/bold cyan] {filename}")

                existing = check_file_exists(download_dir, link)
                if existing:
                    try:
                        size_mb = os.path.getsize(download_dir / existing) / (1024 * 1024)
                        console.print(f"  [yellow]⏭️  File already exists: {existing} ({size_mb:.1f}MB)[/yellow]")
                    except Exception:
                        console.print(f"  [yellow]⏭️  File already exists: {existing}[/yellow]")
                    skipped += 1
                    progress.update(main_task, advance=1, success=successful, skipped=skipped, failed=failed)
                    continue

                if downloads_since_refresh >= args.session_refresh:
                    console.print("  [yellow]🔄 Refreshing browser session...[/yellow]")
                    try:
                        driver.quit()
                    except Exception:
                        pass
                    time.sleep(1)
                    driver = setup_driver(download_dir, headless=args.headless, disable_images=not args.no_image_block)
                    downloads_since_refresh = 0

                result = click_download_button(driver, link, download_dir, max_wait=args.max_wait)
                if result == "SESSION_EXPIRED":
                    console.print("  [yellow]🔄 Restarting browser session (expired)...[/yellow]")
                    try:
                        driver.quit()
                    except Exception:
                        pass
                    time.sleep(1)
                    driver = setup_driver(download_dir, headless=args.headless, disable_images=not args.no_image_block)
                    downloads_since_refresh = 0
                    result = click_download_button(driver, link, download_dir, max_wait=args.max_wait)

                if result is True:
                    successful += 1
                    downloads_since_refresh += 1
                else:
                    failed += 1

                progress.update(main_task, advance=1, success=successful, skipped=skipped, failed=failed)

                if idx < len(links):
                    time.sleep(args.delay_between)

        # Summary
        console.print("\n")
        table = Table(title="📊 Download Summary", box=box.ROUNDED, border_style="cyan")
        table.add_column("Status", style="bold")
        table.add_column("Count", justify="right")
        table.add_column("Percentage", justify="right")

        total = len(links)
        table.add_row("✅ Downloaded", str(successful), f"{successful/total*100:.1f}%", style="green")
        table.add_row("⏭️ Skipped (exist)", str(skipped), f"{skipped/total*100:.1f}%", style="yellow")
        table.add_row("❌ Failed", str(failed), f"{failed/total*100:.1f}%", style="red")
        table.add_row("[dim]" + "─" * 15 + "[/dim]", "[dim]─" * 8 + "[/dim]", "[dim]─" * 10 + "[/dim]")

        total_available = successful + skipped
        table.add_row("📈 Total Available", str(total_available), f"{total_available/total*100:.1f}%", style="bold cyan")

        console.print(table)
        console.print(f"\n[cyan]📁 Download location: {download_dir}[/cyan]")

    except KeyboardInterrupt:
        console.print("\n[yellow]⚠️ Script interrupted by user[/yellow]")
    except Exception as e:
        console.print(f"\n[red]❌ Fatal error: {e}[/red]")
        import traceback
        traceback.print_exc()
    finally:
        try:
            if driver:
                driver.quit()
                console.print("[dim]🔒 Browser closed[/dim]")
        except Exception:
            pass


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Selenium Download Button Clicker (fixed)")
    parser.add_argument("--url", help="Main page URL to scrape for download links")
    parser.add_argument("--output", required=True, help="Output download directory")
    parser.add_argument("--headless", action="store_true", default=False, help="Run browser in headless mode")
    parser.add_argument("--no-image-block", action="store_true", default=False, help="Don't block images (set when site needs images/js to render)")
    parser.add_argument("--max-wait", type=int, default=DEFAULT_MAX_WAIT, help="Max wait time for elements (seconds)")
    parser.add_argument("--session-refresh", type=int, default=DEFAULT_SESSION_REFRESH, help="Restart browser after this many successful downloads")
    parser.add_argument("--delay-between", type=float, default=2.0, help="Delay between processing links (seconds)")
    parser.add_argument("--filter-downloads", action="store_true", help="Try to filter scraped links to likely download pages")
    parser.add_argument("--input-txt",type=str,help="Path to a .txt file containing URLs to download (one per line).")

    args = parser.parse_args()
    # map to expected names
    args.session_refresh = args.session_refresh
    args.no_image_block = args.no_image_block

    run(args)
