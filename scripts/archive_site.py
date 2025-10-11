import argparse
import csv
import os
from datetime import datetime
from typing import List, Optional

import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse

try:
    from waybackpy import WaybackMachineSaveAPI
    from waybackpy.exceptions import WaybackError
except ImportError:  # pragma: no cover - dependency guaranteed in workflow
    WaybackMachineSaveAPI = None  # type: ignore[assignment]
    WaybackError = Exception  # type: ignore[assignment]


DEFAULT_OUTPUT_DIR = "archived"
DEFAULT_USER_AGENT = os.environ.get(
    "ARCHIVE_USER_AGENT",
    "Mozilla/5.0 (compatible; WhiteHouseArchiveBot/1.0; +https://github.com/alphaO4/WhiteHouse-Archive)",
)
REQUEST_TIMEOUT_SECONDS = 20


def fetch_html(url: str) -> str:
    """Fetches HTML content with a consistent user agent."""
    response = requests.get(url, timeout=REQUEST_TIMEOUT_SECONDS, headers={"User-Agent": DEFAULT_USER_AGENT})
    response.raise_for_status()
    return response.text


def sanitize_filename(url: str, timestamp: Optional[str]) -> str:
    """Creates a deterministic filename for an archived Wayback snapshot."""
    parsed = urlparse(url)
    path = parsed.path.rstrip("/")
    segments = [seg for seg in path.split("/") if seg]
    base = parsed.netloc
    if segments:
        base = "{}_{}".format(base, "_".join(segments))
    if timestamp:
        base = f"{base}_{timestamp}"
    base = base.replace(":", "_")
    if not base.endswith(".html"):
        base += ".html"
    return base


def to_iso_timestamp(timestamp: Optional[str]) -> str:
    if timestamp and len(timestamp) == 14 and timestamp.isdigit():
        dt = datetime.strptime(timestamp, "%Y%m%d%H%M%S")
        return dt.isoformat()
    return datetime.utcnow().isoformat()


def log_archive(output_dir: str, original_url: str, wayback_url: str, local_filename: str, timestamp: Optional[str]) -> None:
    os.makedirs(output_dir, exist_ok=True)
    log_path = os.path.join(output_dir, "archive_log.csv")
    file_exists = os.path.exists(log_path)
    with open(log_path, "a", newline="", encoding="utf-8") as log_file:
        writer = csv.writer(log_file)
        if not file_exists:
            writer.writerow(["captured_at", "original_url", "wayback_url", "local_filename"])
        writer.writerow([to_iso_timestamp(timestamp), original_url, wayback_url, local_filename])


def archive_with_wayback(url: str, output_dir: str) -> Optional[str]:
    if WaybackMachineSaveAPI is None:
        raise RuntimeError("waybackpy is not installed. Ensure dependencies are installed before running.")

    print(f"Requesting Wayback Machine snapshot for {url}")
    try:
        save_api = WaybackMachineSaveAPI(url, user_agent=DEFAULT_USER_AGENT)
        save_api.save()
    except WaybackError as err:
        print(f"Wayback Machine failed for {url}: {err}")
        return None

    archived_url = getattr(save_api, "archive_url", None)
    timestamp = getattr(save_api, "timestamp", None)
    if not archived_url:
        print(f"No archive URL returned for {url}")
        return None

    filename = sanitize_filename(url, timestamp)
    os.makedirs(output_dir, exist_ok=True)
    destination = os.path.join(output_dir, filename)
    if os.path.exists(destination):
        print(f"Snapshot already stored for {url} at {destination}")
        log_archive(output_dir, url, archived_url, filename, timestamp)
        return destination

    try:
        response = requests.get(archived_url, timeout=REQUEST_TIMEOUT_SECONDS, headers={"User-Agent": DEFAULT_USER_AGENT})
        response.raise_for_status()
    except requests.RequestException as err:
        print(f"Failed to download archived content for {url}: {err}")
        return None

    with open(destination, "w", encoding="utf-8") as handle:
        handle.write(response.text)

    log_archive(output_dir, url, archived_url, filename, timestamp)
    print(f"Archived {url} -> {archived_url} (saved to {destination})")
    return destination


def extract_related_links(base_url: str, html: str, limit: int) -> List[str]:
    soup = BeautifulSoup(html, "html.parser")
    base_parsed = urlparse(base_url)
    base_domain = base_parsed.netloc
    base_clean = base_url.rstrip("/")

    links: List[str] = []

    for anchor in soup.select("article a[href]"):
        href = urljoin(base_url, anchor.get("href"))
        parsed = urlparse(href)
        cleaned = href.split("#", 1)[0].rstrip("/")
        if parsed.netloc == base_domain and cleaned != base_clean and cleaned not in links:
            links.append(cleaned)
            if 0 < limit <= len(links):
                return links

    if not links:
        for anchor in soup.find_all("a", href=True):
            href = urljoin(base_url, anchor["href"])
            parsed = urlparse(href)
            cleaned = href.split("#", 1)[0].rstrip("/")
            if parsed.netloc != base_domain or cleaned == base_clean:
                continue
            if not cleaned.startswith(base_clean):
                # Allow likely article paths even if they live outside the listing path.
                if "/news" not in parsed.path and "/briefing-room" not in parsed.path:
                    continue
            if cleaned not in links:
                links.append(cleaned)
                if 0 < limit <= len(links):
                    return links

    return links if limit <= 0 else links[:limit]


def archive_site(url: str, output_dir: str = DEFAULT_OUTPUT_DIR, max_links: int = 10) -> None:
    print(f"Archiving main page: {url}")
    try:
        main_html = fetch_html(url)
    except requests.RequestException as err:
        raise RuntimeError(f"Failed to retrieve {url}: {err}") from err

    archive_with_wayback(url, output_dir)

    print("Extracting related links...")
    related_links = extract_related_links(url, main_html, max_links)
    print(f"Found {len(related_links)} related link(s) to archive")

    for related_url in related_links:
        try:
            archive_with_wayback(related_url, output_dir)
        except Exception as err:  # noqa: BLE001
            print(f"Failed to archive {related_url}: {err}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Archive a website and its related articles via the Wayback Machine.")
    parser.add_argument("--url", required=True, help="Base URL to archive (e.g., https://www.whitehouse.gov/news)")
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR, help="Directory to store archived HTML files")
    parser.add_argument("--max-links", type=int, default=10, help="Maximum number of related links to archive (0 for unlimited)")
    args = parser.parse_args()

    archive_site(args.url, output_dir=args.output_dir, max_links=args.max_links)
