#!/usr/bin/env python3
"""
IWCB website generator

This script processes an OPML file containing RSS/Atom feed URLs, fetches the feeds,
parses them, and generates an HTML page with the latest N entries from each feed
published within the last year, sorted by publication date.

It also pull events from Underline Center Discourse API and shows them.

Usage:
    python generator.py blogroll.opml _site
"""

from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from dateutil import parser as date_parser
from dateutil import zoneinfo
from feedgen.feed import FeedGenerator
from icalendar import Calendar, Event as CalEvent
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlparse
from zoneinfo import ZoneInfo
import argparse
import feedparser
import logging
import pystache
import requests
import shutil
import sys
import xml.etree.ElementTree as ET

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
UA = "IndieWebClub BLR website generator"
SITE_URL = "https://indiewebclubblr.github.io/website/"
WEBCAL_URL = SITE_URL.replace("https", "webcal")
EVENTS_TZ = ZoneInfo("Asia/Kolkata")
BLOGROLL_FEED_FILE = "blogroll.atom"
EVENTS_FEED_FILE = "events.atom"
EVENTS_CAL_FILE = "events.ics"


class FeedEntry:
    """Represents a single feed entry with normalized fields."""

    def __init__(
        self,
        title: str,
        link: str,
        published: datetime,
        feed_title: str,
        feed_url: str,
        tags: list[str],
    ):
        self.title = title
        self.link = link
        self.published = published
        self.feed_title = feed_title
        self.feed_url = feed_url
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
            "User-Agent": UA,
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


def parse_feed(feed_title: str, feed_url: str, feed_content: str) -> List[FeedEntry]:
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
                    feed_url=feed_url,
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
    return parse_feed(feed_title, feed_url, content)


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


class Event:
    """Represents an IndieWebClub BLR event"""

    def __init__(
        self,
        id: int,
        title: str,
        slug: str,
        created_at: datetime,
        start_at: datetime,
        end_at: datetime,
        details: str | None,
        underline_url: str,
        district_url: str,
    ):
        self.id = id
        self.title = title
        self.slug = slug
        self.created_at = created_at
        self.start_at = start_at
        self.end_at = end_at
        self.details = details
        self.underline_url = underline_url
        self.district_url = district_url

    def start_at_human(self):
        return self.start_at.astimezone(EVENTS_TZ).strftime("%d %b %Y %I:%M %p IST")

    def start_at_machine(self):
        return self.start_at.isoformat()


def fetch_event_detail(
    topic: Dict, base_url: str = "https://underline.center"
) -> Event | None:
    """Fetch details of IWCB event

    Args:
      base_url: URL of Underline Center Discourse. Default: https://underline.center/
      topic: topic JSON returned from Discourse Search API

    Returns:
      IWCB Event, None if fetch failed
    """
    url = base_url + "/t/" + str(topic["id"]) + ".json"

    try:
        logger.info(f"Fetching event details: {url}")
        headers = {
            "User-Agent": UA,
            "Accept": "application/json",
        }
        response = requests.get(
            url, headers=headers, timeout=REQUEST_TIMEOUT, stream=True
        )
        response.raise_for_status()

        post = response.json()["post_stream"]["posts"][0]
        event = post["event"]

        return Event(
            id=topic["id"],
            title=topic["title"],
            slug=topic["slug"],
            created_at=date_parser.parse(topic["created_at"]),
            start_at=date_parser.parse(event["starts_at"]),
            end_at=date_parser.parse(event["ends_at"]),
            details=post["cooked"],
            underline_url=base_url + "/t/" + topic["slug"],
            district_url=event["url"],
        )
    except requests.exceptions.Timeout:
        logger.warning(f"Timeout fetching event details: {url}")
    except requests.exceptions.HTTPError as e:
        logger.warning(f"HTTP error fetching event details {url}: {e}")
    except requests.exceptions.RequestException as e:
        logger.warning(f"Request error fetching event details {url}: {e}")
    except Exception as e:
        logger.error(f"Unexpected error fetching event details {url}: {e}")

    return None


def fetch_events(base_url: str = "https://underline.center") -> list[Event]:
    """Fetch IWCB events from Underline Center Discourse API

    Args:
      base_url: URL of Underline Center Discourse. Default: https://underline.center/

    Returns:
      IWCB Event as a list, empty if fetch failed
    """
    url = base_url + "/search?q=indieweb%20%23calendar%20order%3Alatest_topic&page=1"
    try:
        logger.info("Fetching events")
        headers = {
            "User-Agent": UA,
            "Accept": "application/json",
        }
        response = requests.get(
            url, headers=headers, timeout=REQUEST_TIMEOUT, stream=True
        )
        response.raise_for_status()

        events = [
            event
            for topic in response.json()["topics"]
            if (event := fetch_event_detail(topic, base_url)) is not None
        ]

        logger.info(f"Extracted {len(events)} recent events")
        return events
    except requests.exceptions.Timeout:
        logger.warning(f"Timeout fetching events: {url}")
    except requests.exceptions.HTTPError as e:
        logger.warning(f"HTTP error fetching events {url}: {e}")
    except requests.exceptions.RequestException as e:
        logger.warning(f"Request error fetching events {url}: {e}")
    except Exception as e:
        logger.error(f"Unexpected error fetching events {url}: {e}")

    return []


def generate_html(entries: List[FeedEntry], events: List[Event], output_dir: Path):
    """
    Generate HTML file from feed entries using Mustache templating.

    Args:
        entries: List of FeedEntry objects to include
        output_path: Path where HTML file should be written
    """
    logger.info(f"Generating HTML with {len(entries)} entries and {len(events)} events")

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

    now = datetime.now(timezone.utc)
    previous_events = [event for event in events if event.start_at <= now]
    upcoming_events = [event for event in events if event.start_at > now]
    upcoming_event = upcoming_events[-1] if len(upcoming_events) > 0 else None

    # Prepare template data
    template_data = {
        "site_url": SITE_URL,
        "webcal_url": WEBCAL_URL,
        "upcoming_event": upcoming_event,
        "previous_events": previous_events,
        "total_entries": len(recent_entries),
        "total_feeds": len(feed_groups),
        "entries": recent_entries,
        "generated_date": now.astimezone(EVENTS_TZ).strftime("%d %b %Y %I:%M %p IST"),
    }

    # HTML template
    with open("./index.html") as index_tpl:
        html_template = index_tpl.read()

    # Render template
    try:
        renderer = pystache.Renderer()
        html_content = renderer.render(html_template, template_data)

        # Write to file
        output_path = output_dir.joinpath("index.html")
        output_path.write_text(html_content, encoding="utf-8")
        logger.info(f"HTML file written to: {output_path}")

    except Exception as e:
        logger.error(f"Failed to generate HTML: {e}")
        raise


def generate_blogroll_feed(entries: list[FeedEntry], output_dir: Path):
    """
    Creates an Atom feed from a list of FeedEntry objects.

    Args:
        entries: A list of FeedEntry objects to include in the feed.
        output_path: Path where Atom file should be written

    """
    logger.info(f"Generating blogroll feed with {len(entries)} entries")
    output_path = output_dir.joinpath(BLOGROLL_FEED_FILE)

    FEED_URL = SITE_URL + output_path.name
    fg = FeedGenerator()

    fg.id(FEED_URL)
    fg.title("IndieWebClub Bangalore Blogroll")
    fg.author(name="IndieWebClub Bangalore")
    fg.link(href=FEED_URL, rel="self")
    fg.link(href=SITE_URL, rel="alternate")
    fg.subtitle("Recent posts by IndieWebClub Bangalore folks.")

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
    logger.info(f"Blogroll feed written to: {output_path}")


def generate_events_feed(events: list[Event], output_dir: Path):
    """
    Creates an Atom feed from a list of Event objects.

    Args:
        events: A list of Event objects to include in the calender.
        output_path: Path where Atom file should be written

    """
    logger.info(f"Generating events feed with {len(events)} events")
    output_path = output_dir.joinpath(EVENTS_FEED_FILE)

    FEED_URL = SITE_URL + output_path.name
    fg = FeedGenerator()

    fg.id(FEED_URL)
    fg.title("IndieWebClub Bangalore Events")
    fg.author(name="IndieWebClub Bangalore")
    fg.link(href=FEED_URL, rel="self")
    fg.link(href=SITE_URL, rel="alternate")
    fg.subtitle("Events by IndieWebClub Bangalore.")

    for event in events:
        fe = fg.add_entry(order="append")

        fe.id(event.underline_url)
        fe.title(event.title)
        fe.link(href=event.underline_url, rel="alternate")
        fe.published(event.created_at)
        fe.updated(event.created_at)
        fe.content(event.details)

    fg.atom_file(output_path, pretty=True)
    logger.info(f"Events feed written to: {output_path}")


def generate_events_calendar(events: list[Event], output_dir: Path):
    """
    Creates an Calendar from a list of Event objects.

    Args:
        events: A list of Event objects to include in the feed.
        output_path: Path where Calendar file should be written

    """
    logger.info(f"Generating events calendar with {len(events)} events")

    cal = Calendar()
    cal.calendar_name = "IndieWebClub Bangalore Events"
    cal.description = "Events by IndieWebClub Bangalore"

    for event_data in events:
        event = CalEvent()
        event.add("summary", event_data.title)
        event.add("url", event_data.underline_url)
        event.start = event_data.start_at
        event.end = event_data.end_at
        event.uid = f"indiewebclubblr-event-{event_data.id}"

        cal.add_component(event)

    output_path = output_dir.joinpath(EVENTS_CAL_FILE)
    with open(output_path, "wb") as f:
        f.write(cal.to_ical())

    logger.info(f"Events calendar written to: {output_path}")


def main():
    """Main function to orchestrate the feed aggregation process."""
    parser = argparse.ArgumentParser(
        description="Generate HTML from OPML feeds with recent entries"
    )
    parser.add_argument("opml_file", help="Input OPML file path")
    parser.add_argument(
        "output_dir", help="The directory to output the built artifacts."
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true", help="Enable verbose logging"
    )

    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    opml_path = Path(args.opml_file)
    output_dir = Path(args.output_dir)

    try:
        # Copy OPML file
        shutil.copyfile(opml_path, output_dir.joinpath(opml_path))

        # Parse OPML file
        feeds = parse_opml_file(opml_path)

        if not feeds:
            logger.warning("No feeds found in OPML file")

        # Fetch and parse all feeds
        entries = fetch_all_feeds(feeds) if len(feeds) > 0 else []

        # Fetch all events
        events = fetch_events()

        generate_html(entries, events, output_dir)
        generate_blogroll_feed(entries, output_dir)
        generate_events_feed(events, output_dir)
        generate_events_calendar(events, output_dir)

        logger.info("Website generation completed successfully")

    except KeyboardInterrupt:
        logger.info("Process interrupted by user")
        sys.exit(130)
    except Exception as e:
        logger.error(f"Fatal error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
