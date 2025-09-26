from __future__ import annotations
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from dateutil import parser as date_parser
from feedgen.feed import FeedGenerator
from pathlib import Path
from typing import List, Optional, Tuple
from urllib.parse import urlparse
import config
import feedparser
import hashlib
import logging
import requests
import threading
import xml.etree.ElementTree as ET

# Configure logging
logging.basicConfig(level=logging.INFO, format=config.LOG_FORMAT)
logger = logging.getLogger(__name__)

sessions = {}


def get_session() -> requests.Session:
    cur_thread_id = threading.get_ident()
    if cur_thread_id not in sessions:
        session = requests.Session()
        session.headers.update(
            {
                "User-Agent": config.UA,
                "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml",
            }
        )
        sessions[cur_thread_id] = session
    return sessions[cur_thread_id]


def close_sessions():
    for session in sessions.values():
        session.close()


class FeedEntry:
    """Represents a single feed entry with normalized fields."""

    def __init__(
        self,
        title: str,
        link: str,
        published: datetime,
        feed_title: str,
        feed_url: str,
        feed_home_url: str,
        tags: List[str],
    ):
        self.title = title
        self.link = link
        self.published = published
        self.feed_title = feed_title
        self.feed_url = feed_url
        self.feed_home_url = feed_home_url
        self.tags = tags

    def published_human(self) -> str:
        return self.published.strftime("%d %b %Y")

    def published_machine(self) -> str:
        return self.published.isoformat()


def parse_opml_file(opml_path: Path) -> List[Tuple[str, str]]:
    """
    Parse OPML file and extract feed URLs with their titles.

    Args:
        opml_path: Path to the OPML file.

    Returns:
        List of tuples containing (feed_title, feed_url).

    Raises:
        FileNotFoundError: If OPML file doesn't exist.
        ET.ParseError: If OPML file is malformed.
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


def generate_feed(
    feed_url: str,
    feed_title: str,
    author_name: Optional[str],
    feed_home_url: str,
    feed_subtitle: Optional[str],
    entries: List[FeedEntry],
    output_path: Path,
):
    """
    Creates an Atom feed from a list of FeedEntry objects.

    Args:
        entries: A list of FeedEntry objects to include in the feed.
        output_path: Path where Atom file should be written.
    """
    fg = FeedGenerator()

    fg.id(feed_url)
    fg.title(feed_title)
    if author_name is not None:
        fg.author(name=author_name)
    fg.link(href=feed_url, rel="self")
    fg.link(href=feed_home_url, rel="alternate")
    if feed_subtitle is not None:
        fg.subtitle(feed_subtitle)

    for entry in entries:
        fe = fg.add_entry(order="append")

        fe.id(entry.link)
        fe.title(entry.title)
        fe.link(href=entry.link, rel="alternate")
        fe.published(entry.published)
        fe.updated(entry.published)
        fe.author(name=entry.feed_title, uri=entry.feed_url)

        for tag in entry.tags:
            fe.category(term=tag)

    fg.atom_file(output_path, pretty=True)


def fetch_feed_content(url: str) -> Optional[str]:
    """
    Fetch feed content from URL with proper error handling and limits.

    Args:
        url: Feed URL to fetch.

    Returns:
        Feed content as string, or None if fetch failed.
    """
    try:
        logger.info(f"Fetching feed: {url}")

        # Validate URL
        parsed_url = urlparse(url)
        if not parsed_url.scheme or not parsed_url.netloc:
            logger.warning(f"Invalid URL format: {url}")
            return None

        session = get_session()
        response = session.get(url, timeout=config.REQUEST_TIMEOUT, stream=True)
        response.raise_for_status()

        # Check content length
        content_length = response.headers.get("content-length")
        if content_length and int(content_length) > config.MAX_CONTENT_LENGTH:
            logger.warning(f"Feed too large ({content_length} bytes): {url}")
            return None

        # Read content with size limit
        content = b""
        for chunk in response.iter_content(chunk_size=8192):
            content += chunk
            if len(content) > config.MAX_CONTENT_LENGTH:
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
        date_string: Date string to parse.

    Returns:
        Parsed datetime object in UTC, or None if parsing failed.
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


def parse_feed(feed_title: str, feed_url: str, feed_content: str) -> List[FeedEntry]:
    """
    Parse feed content and extract recent entries.

    Args:
        feed_title: Title of the feed.
        feed_content: Raw feed content.

    Returns:
        List of FeedEntry objects.
    """
    try:
        logger.debug(f"Parsing feed: {feed_title}")

        # Parse with feedparser
        parsed_feed = feedparser.parse(feed_content)

        if parsed_feed.bozo and hasattr(parsed_feed, "bozo_exception"):
            logger.debug(
                f"Feed parser warning for {feed_title}: {parsed_feed.bozo_exception}"
            )

        # Calculate cutoff date
        now = datetime.now(timezone.utc)
        cutoff_date = now - timedelta(days=config.RECENT_DAYS)

        entries = []

        for entry in parsed_feed.entries:
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
            if not published or published < cutoff_date or published > now:
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
                    feed_url=feed_url,
                    feed_home_url=parsed_feed.feed.link,
                    tags=[tag for tag in tags if tag is not None],
                )
            )

        # Sort by publication date (newest first) and take top N
        entries.sort(key=lambda x: x.published, reverse=True)
        entries = entries[: config.MAX_FEED_ENTRIES]

        logger.debug(f"Extracted {len(entries)} recent entries from {feed_title}")
        return entries

    except Exception as e:
        logger.warning(f"Failed to parse feed {feed_title}: {e}")
        return []


def process_single_feed(feed_info: Tuple[str, str], use_cache: bool) -> List[FeedEntry]:
    """
    Process a single feed: fetch and parse it.

    Args:
        feed_info: Tuple of (feed_title, feed_url).
        use_cache: Whether to use cached content.

    Returns:
        List of FeedEntry objects.
    """
    feed_title, feed_url = feed_info

    cache_key = hashlib.sha256(feed_url.encode()).hexdigest()
    cache_file = config.CACHE_DIR / cache_key

    if use_cache and cache_file.exists():
        logger.debug(f"Using cached content for: {feed_url}")
        content = cache_file.read_text(encoding="utf-8")
    else:
        # Fetch feed content
        content = fetch_feed_content(feed_url)
        if not content:
            return []

    # Parse feed content
    entries = parse_feed(feed_title, feed_url, content)

    if use_cache and len(entries) > 0:
        # Save content to cache
        generate_feed(
            feed_url=feed_url,
            feed_title=entries[0].feed_title,
            author_name=entries[0].feed_title,
            feed_home_url=entries[0].feed_home_url,
            feed_subtitle=None,
            entries=entries,
            output_path=cache_file,
        )
        logger.debug(f"Cached content for: {feed_url}")

    logger.info(f"Processed {feed_title}: {len(entries)} entries")
    return entries


def fetch_all_feeds(feeds: List[Tuple[str, str]], use_cache: bool) -> List[FeedEntry]:
    """
    Fetch and parse all feeds concurrently.

    Args:
        feeds: List of (feed_title, feed_url) tuples.
        use_cache: Whether to use cached content.

    Returns:
        Combined list of all feed entries.
    """
    if len(feeds) == 0:
        return []

    logger.info(f"Processing {len(feeds)} feeds with {config.MAX_WORKERS} workers")

    all_entries = []

    with ThreadPoolExecutor(max_workers=config.MAX_WORKERS) as executor:
        # Submit all feed processing tasks
        future_to_feed = {
            executor.submit(process_single_feed, feed_info, use_cache): feed_info
            for feed_info in feeds
        }

        # Collect results as they complete
        for future in as_completed(future_to_feed):
            feed_info = future_to_feed[future]
            feed_title = feed_info[0]

            try:
                entries = future.result()
                all_entries.extend(entries)
            except Exception as e:
                logger.error(f"Failed to process {feed_title}: {e}")

    close_sessions()
    return all_entries
