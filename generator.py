#!/usr/bin/env python3
"""
IWCB website generator

This script processes an OPML file containing RSS/Atom feed URLs, fetches the feeds,
parses them, and generates an HTML page with the latest 3 entries from each feed
published within the last year, sorted by publication date.

Dependencies:
    - requests
    - feedparser
    - pystache (mustache templating)
    - python-dateutil

Usage:
    python generator.py input.opml output.html
"""

import argparse
import logging
import sys
import xml.etree.ElementTree as ET
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional, Tuple
from urllib.parse import urlparse

import feedparser
import pystache
import requests
from dateutil import parser as date_parser

# Configure logging
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# Configuration constants
REQUEST_TIMEOUT = 30  # seconds
MAX_CONTENT_LENGTH = 5 * 1024 * 1024  # 5MB
MAX_SHOWN_ENTRIES = 2
MAX_FEED_ENTRIES = 10
MAX_SHOW_TAGS = 5
MAX_WORKERS = 10  # concurrent feed fetches
RECENT_DAYS = 365  # one year


class FeedEntry:
    """Represents a single feed entry with normalized fields."""

    def __init__(
        self,
        title: str,
        link: str,
        published: datetime,
        feed_title: str,
        tags: list[str],
    ):
        self.title = title
        self.link = link
        self.published = published
        self.feed_title = feed_title
        self.tags = tags

    def published_human(self):
        return self.published.strftime("%d %b %Y")

    def published_machine(self):
        return self.published.isoformat()


def parse_opml_file(opml_path: Path) -> List[Tuple[str, str]]:
    """
    Parse OPML file and extract feed URLs with their titles.

    Args:
        opml_path: Path to the OPML file

    Returns:
        List of tuples containing (feed_title, feed_url)

    Raises:
        FileNotFoundError: If OPML file doesn't exist
        ET.ParseError: If OPML file is malformed
    """
    logger.info(f"Parsing OPML file: {opml_path}")

    try:
        tree = ET.parse(opml_path)
        root = tree.getroot()

        feeds = []

        # Look for outline elements with xmlUrl attribute
        for outline in root.iter("outline"):
            xml_url = outline.get("xmlUrl")
            if xml_url:
                title = outline.get("title") or outline.get("text")
                if title is None:
                    logger.error(f"OPML feed {xml_url} does not have title or text")
                    raise
                feeds.append((title, xml_url))
                logger.debug(f"Found feed: {title} -> {xml_url}")

        logger.info(f"Found {len(feeds)} feeds in OPML file")
        return feeds

    except FileNotFoundError:
        logger.error(f"OPML file not found: {opml_path}")
        raise
    except ET.ParseError as e:
        logger.error(f"Failed to parse OPML file: {e}")
        raise


def fetch_feed_content(url: str) -> Optional[str]:
    """
    Fetch feed content from URL with proper error handling and limits.

    Args:
        url: Feed URL to fetch

    Returns:
        Feed content as string, or None if fetch failed
    """
    try:
        logger.debug(f"Fetching feed: {url}")

        # Validate URL
        parsed_url = urlparse(url)
        if not parsed_url.scheme or not parsed_url.netloc:
            logger.warning(f"Invalid URL format: {url}")
            return None

        headers = {
            "User-Agent": "OPML Feed Aggregator 1.0",
            "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml",
        }

        response = requests.get(
            url, headers=headers, timeout=REQUEST_TIMEOUT, stream=True
        )
        response.raise_for_status()

        # Check content length
        content_length = response.headers.get("content-length")
        if content_length and int(content_length) > MAX_CONTENT_LENGTH:
            logger.warning(f"Feed too large ({content_length} bytes): {url}")
            return None

        # Read content with size limit
        content = b""
        for chunk in response.iter_content(chunk_size=8192):
            content += chunk
            if len(content) > MAX_CONTENT_LENGTH:
                logger.warning(f"Feed content exceeded size limit: {url}")
                return None

        return content.decode("utf-8", errors="ignore")

    except requests.exceptions.Timeout:
        logger.warning(f"Timeout fetching feed: {url}")
    except requests.exceptions.HTTPError as e:
        logger.warning(f"HTTP error fetching feed {url}: {e}")
    except requests.exceptions.RequestException as e:
        logger.warning(f"Request error fetching feed {url}: {e}")
    except Exception as e:
        logger.error(f"Unexpected error fetching feed {url}: {e}")

    return None


def parse_feed_date(date_string: str) -> Optional[datetime]:
    """
    Parse various date formats commonly found in feeds.

    Args:
        date_string: Date string to parse

    Returns:
        Parsed datetime object in UTC, or None if parsing failed
    """
    if not date_string:
        return None

    try:
        # Try parsing with dateutil (handles most formats)
        dt = date_parser.parse(date_string)

        # Ensure timezone info
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        else:
            dt = dt.astimezone(timezone.utc)

        return dt

    except (ValueError, TypeError) as e:
        logger.debug(f"Failed to parse date '{date_string}': {e}")
        return None


def parse_feed(feed_title: str, feed_content: str) -> List[FeedEntry]:
    """
    Parse feed content and extract recent entries.

    Args:
        feed_title: Title of the feed
        feed_content: Raw feed content

    Returns:
        List of FeedEntry objects from the last year
    """
    try:
        logger.debug(f"Parsing feed: {feed_title}")

        # Parse with feedparser
        parsed_feed = feedparser.parse(feed_content)

        if parsed_feed.bozo and hasattr(parsed_feed, "bozo_exception"):
            logger.debug(
                f"Feed parser warning for {feed_title}: {parsed_feed.bozo_exception}"
            )

        # Calculate cutoff date (one year ago)
        cutoff_date = datetime.now(timezone.utc) - timedelta(days=RECENT_DAYS)

        entries = []

        for entry in parsed_feed.entries[
            :MAX_FEED_ENTRIES
        ]:  # Limit processing to first 50 entries
            # Extract and normalize entry data
            title = getattr(entry, "title", "Untitled")
            link = getattr(entry, "link", "")

            # Parse publication date
            published = None
            for date_field in ["published", "updated", "created"]:
                date_value = getattr(entry, date_field, None)
                if date_value:
                    published = parse_feed_date(date_value)
                    if published:
                        break

            # Skip entries without valid dates or too old
            if not published or published < cutoff_date:
                continue

            tags = [
                tag.get("label") or tag.get("term")
                for tag in getattr(entry, "tags", [])
            ]

            entries.append(
                FeedEntry(
                    title=title.strip(),
                    link=link.strip(),
                    published=published,
                    feed_title=feed_title,
                    tags=[tag for tag in tags if tag is not None],
                )
            )

        # Sort by publication date (newest first) and take top 3
        entries.sort(key=lambda x: x.published, reverse=True)

        logger.debug(f"Extracted {len(entries)} recent entries from {feed_title}")
        return entries

    except Exception as e:
        logger.warning(f"Failed to parse feed {feed_title}: {e}")
        return []


def process_single_feed(feed_info: Tuple[str, str]) -> List[FeedEntry]:
    """
    Process a single feed: fetch and parse it.

    Args:
        feed_info: Tuple of (feed_title, feed_url)

    Returns:
        List of FeedEntry objects
    """
    feed_title, feed_url = feed_info

    # Fetch feed content
    content = fetch_feed_content(feed_url)
    if not content:
        return []

    # Parse feed content
    return parse_feed(feed_title, content)


def fetch_all_feeds(feeds: List[Tuple[str, str]]) -> List[FeedEntry]:
    """
    Fetch and parse all feeds concurrently.

    Args:
        feeds: List of (feed_title, feed_url) tuples

    Returns:
        Combined list of all feed entries
    """
    logger.info(f"Processing {len(feeds)} feeds with {MAX_WORKERS} workers")

    all_entries = []

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        # Submit all feed processing tasks
        future_to_feed = {
            executor.submit(process_single_feed, feed_info): feed_info
            for feed_info in feeds
        }

        # Collect results as they complete
        for future in as_completed(future_to_feed):
            feed_info = future_to_feed[future]
            feed_title = feed_info[0]

            try:
                entries = future.result()
                all_entries.extend(entries)
                logger.info(f"Processed {feed_title}: {len(entries)} entries")
            except Exception as e:
                logger.error(f"Failed to process {feed_title}: {e}")

    return all_entries


def generate_html(entries: List[FeedEntry], output_path: Path):
    """
    Generate HTML file from feed entries using Mustache templating.

    Args:
        entries: List of FeedEntry objects to include
        output_path: Path where HTML file should be written
    """
    logger.info(f"Generating HTML with {len(entries)} entries")

    # Group entries by OPML feed title
    feed_groups = defaultdict(list)

    for entry in entries:
        feed_groups[entry.feed_title].append(entry)

    # Sort entries within each group by publication date (newest first)
    # and take top 3 from each group
    recent_entries = []
    for feed_title in feed_groups.keys():
        group_entries = feed_groups[feed_title]
        group_entries.sort(key=lambda x: x.published, reverse=True)
        group_entries = [
            deepcopy(entry) for entry in group_entries[:MAX_SHOWN_ENTRIES]
        ]  # Top 3 from this feed group

        for entry in group_entries:
            entry.tags = entry.tags[:MAX_SHOW_TAGS]

        recent_entries.extend(group_entries)

    # Sort all entries globally by publication date for overall stats
    recent_entries.sort(key=lambda x: x.published, reverse=True)

    # Prepare template data
    template_data = {
        "title": "IWCB",
        "generated_date": datetime.now(timezone.utc).strftime("%d %b %Y"),
        "total_entries": len(recent_entries),
        "total_feeds": len(feed_groups),
        "entries": recent_entries,
    }

    # HTML template
    with open("./index.html") as index_tpl:
        html_template = index_tpl.read()

    # Render template
    try:
        renderer = pystache.Renderer()
        html_content = renderer.render(html_template, template_data)

        # Write to file
        output_path.write_text(html_content, encoding="utf-8")
        logger.info(f"HTML file written to: {output_path}")

    except Exception as e:
        logger.error(f"Failed to generate HTML: {e}")
        raise


def main():
    """Main function to orchestrate the feed aggregation process."""
    parser = argparse.ArgumentParser(
        description="Generate HTML from OPML feeds with recent entries"
    )
    parser.add_argument("opml_file", help="Input OPML file path")
    parser.add_argument("html_file", help="Output HTML file path")
    parser.add_argument(
        "--verbose", "-v", action="store_true", help="Enable verbose logging"
    )

    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    opml_path = Path(args.opml_file)
    html_path = Path(args.html_file)

    try:
        # Parse OPML file
        feeds = parse_opml_file(opml_path)

        if not feeds:
            logger.warning("No feeds found in OPML file")
            sys.exit(1)

        # Fetch and parse all feeds
        entries = fetch_all_feeds(feeds)

        # Generate HTML output
        generate_html(entries, html_path)

        logger.info("Feed aggregation completed successfully")

    except KeyboardInterrupt:
        logger.info("Process interrupted by user")
        sys.exit(130)
    except Exception as e:
        logger.error(f"Fatal error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
