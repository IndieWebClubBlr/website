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
from concurrent.futures import Future, ThreadPoolExecutor, wait
from copy import deepcopy
from datetime import datetime, timezone
from events import Event, fetch_events
from feedgen.feed import FeedGenerator
from feeds import FeedEntry, FailedFeed, parse_opml_file, fetch_all_feeds, generate_feed
from icalendar import Calendar, Event as CalEvent
from pathlib import Path
from typing import Any
import argparse
import config
import logging
import pystache
import shutil
import sys

# Configure logging
logging.basicConfig(level=logging.INFO, format=config.LOG_FORMAT)
logger = logging.getLogger(__name__)


def group_feed_entries(entries: list[FeedEntry]) -> list[FeedEntry]:
    # Group entries by OPML feed title
    feed_groups: defaultdict[str, list[FeedEntry]] = defaultdict(list)

    for entry in entries:
        feed_groups[entry.feed_title].append(entry)

    res_entries: list[FeedEntry] = []
    for feed_title in feed_groups.keys():
        group_entries = feed_groups[feed_title]
        group_entries.sort(key=lambda x: x.published, reverse=True)
        group_entries = [
            deepcopy(entry) for entry in group_entries[: config.MAX_SHOWN_ENTRIES]
        ]

        for entry in group_entries:
            entry.tags = entry.tags[: config.MAX_SHOWN_TAGS]

        res_entries.extend(group_entries)

    # Sort result entries globally by publication date for overall stats
    res_entries.sort(key=lambda x: x.published, reverse=True)
    return res_entries


def generate_html(
    entries: list[FeedEntry],
    events: list[Event],
    failed_feeds: list[FailedFeed],
    output_dir: Path,
):
    """
    Generate HTML file from feed entries using Mustache templating.

    Args:
        entries: List of FeedEntry objects to include.
        events: List of Event objects to include.
        failed_feeds: List of FailedFeed objects to include.
        output_dir: Path where HTML file should be written.
    """
    logger.info(
        f"Generating HTML with {len(entries)} entries, {len(events)} events, and {len(failed_feeds)} failed feeds"
    )

    # Separate week notes from other entries
    week_notes: list[FeedEntry] = []
    other_entries: list[FeedEntry] = []

    for entry in entries:
        title = entry.title.lower()
        if (
            "weeknote" in title
            or (
                "week" in title
                and all(
                    w not in title
                    for w in ["weekend", "biweek", "midweek", "semiweek", "yesterweek"]
                )
            )
            or any("weeknote" in tag.lower() for tag in entry.tags)
        ):
            week_notes.append(entry)
        else:
            other_entries.append(entry)

    now = datetime.now(timezone.utc)
    previous_events = [event for event in events if event.start_at <= now]
    upcoming_events = [event for event in events if event.start_at > now]
    upcoming_events.reverse()

    # Prepare template data
    template_data = {
        "site_url": config.SITE_URL,
        "webcal_url": config.WEBCAL_URL,
        "upcoming_events": upcoming_events,
        "has_upcoming_events": len(upcoming_events) > 0,
        "previous_events": previous_events[: config.MAX_SHOWN_EVENTS],
        "entries": group_feed_entries(other_entries),
        "week_notes": group_feed_entries(week_notes)[: config.MAX_SHOWN_WEEK_NOTES],
        "failed_feeds": failed_feeds,
        "has_failed_feeds": len(failed_feeds) != 0,
        "generated_date": now.astimezone(config.EVENTS_TZ).strftime(
            "%d %b %Y, %I:%M %p IST"
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
        _ = output_path.write_text(html_content, encoding="utf-8")
        logger.info(f"HTML file written to: {output_path}")

    except Exception as e:
        logger.error(f"Failed to generate HTML: {e}")
        raise


def generate_blogroll_feed(entries: list[FeedEntry], output_dir: Path):
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


def generate_events_feed(events: list[Event], output_dir: Path):
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


def generate_events_calendar(events: list[Event], output_dir: Path):
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
        _ = f.write(cal.to_ical())

    logger.info(f"Events calendar written to: {output_path}")


def generate_website(opml_path: Path, output_dir: Path, use_cache: bool):
    with ThreadPoolExecutor() as executor:
        futures: list[Future[Any]] = []

        # Copy OPML file
        futures.append(
            executor.submit(shutil.copyfile, opml_path, output_dir.joinpath(opml_path))
        )

        # Copy assets
        futures.extend(
            (
                executor.submit(shutil.copyfile, asset, output_dir.joinpath(asset))
                for asset in config.ASSETS
            )
        )

        def generate_events_files(events_future: Future[list[Event]]):
            events = events_future.result()
            futures.extend(
                (
                    executor.submit(generate_events_feed, events, output_dir),
                    executor.submit(generate_events_calendar, events, output_dir),
                )
            )

        # Fetch all events
        events_future = executor.submit(fetch_events, use_cache=use_cache)
        events_future.add_done_callback(generate_events_files)

        # Parse OPML file
        feeds = parse_opml_file(opml_path)
        if not feeds:
            logger.warning("No feeds found in OPML file")

        # Fetch and parse all feeds
        entries, failed_feeds = fetch_all_feeds(feeds, use_cache=use_cache)
        futures.append(executor.submit(generate_blogroll_feed, entries, output_dir))

        events = events_future.result()
        generate_html(entries, events, failed_feeds, output_dir)
        _ = wait(futures)

    logger.info("Website generation completed successfully")


def main():
    """Main function to orchestrate the feed aggregation process."""
    parser = argparse.ArgumentParser(
        description="Generate HTML from OPML feeds with recent entries"
    )
    _ = parser.add_argument("opml_file", help="Input OPML file path")
    _ = parser.add_argument(
        "output_dir", help="The directory to output the built artifacts."
    )
    _ = parser.add_argument(
        "--verbose", "-v", action="store_true", help="Enable verbose logging"
    )
    _ = parser.add_argument(
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
