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

from __future__ import annotations
from collections import defaultdict
from copy import deepcopy
from datetime import datetime, timezone
from events import Event, fetch_events
from feedgen.feed import FeedGenerator
from feeds import FeedEntry, parse_opml_file, fetch_all_feeds, generate_feed
from icalendar import Calendar, Event as CalEvent
from pathlib import Path
from typing import List
import argparse
import config
import logging
import pystache
import shutil
import sys

# Configure logging
logging.basicConfig(
    level=logging.INFO, format=config.LOG_FORMAT
)
logger = logging.getLogger(__name__)


def generate_html(entries: List[FeedEntry], events: List[Event], output_dir: Path):
    """
    Generate HTML file from feed entries using Mustache templating.

    Args:
        entries: List of FeedEntry objects to include.
        output_path: Path where HTML file should be written.
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
            deepcopy(entry) for entry in group_entries[: config.MAX_SHOWN_ENTRIES]
        ]  # Top 3 from this feed group

        for entry in group_entries:
            entry.tags = entry.tags[: config.MAX_SHOWN_TAGS]

        recent_entries.extend(group_entries)

    # Sort all entries globally by publication date for overall stats
    recent_entries.sort(key=lambda x: x.published, reverse=True)

    now = datetime.now(timezone.utc)
    previous_events = [event for event in events if event.start_at <= now]
    upcoming_events = [event for event in events if event.start_at > now]
    upcoming_event = upcoming_events[-1] if len(upcoming_events) > 0 else None

    # Prepare template data
    template_data = {
        "site_url": config.SITE_URL,
        "webcal_url": config.WEBCAL_URL,
        "upcoming_event": upcoming_event,
        "previous_events": previous_events,
        "total_entries": len(recent_entries),
        "total_feeds": len(feed_groups),
        "entries": recent_entries,
        "generated_date": now.astimezone(config.EVENTS_TZ).strftime(
            "%d %b %Y %I:%M %p IST"
        ),
    }

    try:
        with open("index.html") as index_tpl:
            html_template = index_tpl.read()
    except FileNotFoundError:
        logger.error("Template file index.html not found.")
        raise

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


def generate_blogroll_feed(entries: List[FeedEntry], output_dir: Path):
    """
    Creates an Atom feed from a list of FeedEntry objects.

    Args:
        entries: A list of FeedEntry objects to include in the feed.
        output_dir: Directory where Atom file should be written.
    """
    logger.info(f"Generating blogroll feed with {len(entries)} entries")
    output_path = output_dir.joinpath(config.BLOGROLL_FEED_FILE)
    feed_url = config.SITE_URL + output_path.name

    generate_feed(
        feed_url=feed_url,
        feed_title="IndieWebClub Bangalore Blogroll",
        author_name="IndieWebClub Bangalore",
        feed_home_url=config.SITE_URL,
        feed_subtitle="Recent posts by IndieWebClub Bangalore folks.",
        entries=entries,
        output_path=output_path,
    )

    logger.info(f"Blogroll feed written to: {output_path}")


def generate_events_feed(events: List[Event], output_dir: Path):
    """
    Creates an Atom feed from a list of Event objects.

    Args:
        events: A list of Event objects to include in the calender.
        output_path: Path where Atom file should be written.

    """
    logger.info(f"Generating events feed with {len(events)} events")
    output_path = output_dir.joinpath(config.EVENTS_FEED_FILE)

    feed_url = config.SITE_URL + output_path.name
    fg = FeedGenerator()

    fg.id(feed_url)
    fg.title("IndieWebClub Bangalore Events")
    fg.author(name="IndieWebClub Bangalore")
    fg.link(href=feed_url, rel="self")
    fg.link(href=config.SITE_URL, rel="alternate")
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


def generate_events_calendar(events: List[Event], output_dir: Path):
    """
    Creates an Calendar from a list of Event objects.

    Args:
        events: A list of Event objects to include in the feed.
        output_path: Path where Calendar file should be written.

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

    output_path = output_dir.joinpath(config.EVENTS_CAL_FILE)
    with open(output_path, "wb") as f:
        f.write(cal.to_ical())

    logger.info(f"Events calendar written to: {output_path}")


def generate_website(opml_path: Path, output_dir: Path, use_cache: bool):
    for asset in config.ASSETS:
        shutil.copyfile(asset, output_dir.joinpath(asset))

    # Copy OPML file
    shutil.copyfile(opml_path, output_dir.joinpath(opml_path))

    # Parse OPML file
    feeds = parse_opml_file(opml_path)

    if not feeds:
        logger.warning("No feeds found in OPML file")

    # Fetch and parse all feeds
    entries = fetch_all_feeds(feeds, use_cache=use_cache) if len(feeds) > 0 else []

    # Fetch all events
    events = fetch_events(use_cache=use_cache)

    generate_html(entries, events, output_dir)
    generate_blogroll_feed(entries, output_dir)
    generate_events_feed(events, output_dir)
    generate_events_calendar(events, output_dir)

    logger.info("Website generation completed successfully")


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
    parser.add_argument(
        "--cache", action="store_true", help="Enable caching of fetched feeds"
    )

    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    opml_path = Path(args.opml_file)
    output_dir = Path(args.output_dir)

    if not opml_path.exists():
        logger.error(f"OPML file does not exist: {opml_path}")
        sys.exit(1)

    output_dir.mkdir(exist_ok=True)

    if args.cache:
        logger.info("Caching enabled")
        config.CACHE_DIR.mkdir(exist_ok=True)

    try:
        generate_website(opml_path, output_dir, args.cache)
    except KeyboardInterrupt:
        logger.info("Process interrupted by user")
        sys.exit(130)
    except Exception as e:
        logger.error(f"Fatal error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
