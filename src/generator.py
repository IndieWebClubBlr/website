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

import argparse
import logging
import random
import shutil
import sys
from collections import defaultdict
from concurrent.futures import Future, ThreadPoolExecutor, wait
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import markdown
import pystache
from feedgen.feed import FeedGenerator
from icalendar import Calendar
from icalendar import Event as CalEvent

# Add parent directory to path so we can import src modules
sys.path.insert(0, str(Path(__file__).parent.parent))

from src import config
from src.events import Event, fetch_events
from src.feeds import (
    FailedFeedInfo,
    FailureReason,
    FeedEntry,
    FeedInfo,
    fetch_all_feeds,
    generate_feed,
    parse_opml_file,
)

# Configure logging
logging.basicConfig(level=logging.INFO, format=config.LOG_FORMAT)
logger = logging.getLogger(__name__)


def group_feed_entries(entries: list[FeedEntry]) -> list[FeedEntry]:
    """
    Group and sort feed entries by feed title, keeping only the most recent entries per feed.

    Args:
        entries: List of FeedEntry objects to group.

    Returns:
        List of FeedEntry objects grouped by feed title and sorted by publication date.
    """
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


def read_template(file_name: str) -> str:
    """
    Read a template file.

    Args:
        file_name: Name of the template file to read.

    Returns:
        Template file contents as string.
    """
    try:
        template_path = Path("templates") / file_name
        with open(template_path) as index_tpl:
            return index_tpl.read()
    except FileNotFoundError:
        logger.error(f"Template file {file_name} not found.")
        raise


def markdown_to_html(markdown_file: Path) -> str:
    """
    Convert a Markdown file to HTML.

    Args:
        markdown_file: Path to the Markdown file.

    Returns:
        HTML string.
    """
    try:
        markdown_content = markdown_file.read_text(encoding="utf-8")
        return markdown.markdown(
            markdown_content,
            extensions=["fenced_code", "admonition", "codehilite", "smarty"],
        )
    except Exception as e:
        logger.error(f"Failed to convert markdown from {markdown_file}: {e}")
        raise


def save_html(content: str, output_file: str, output_dir: Path):
    output_path = output_dir.joinpath(output_file)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    _ = output_path.write_text(content, encoding="utf-8")
    logger.info(f"HTML file written to: {output_path}")


def render_and_save_html(html_content: str, output_dir: Path):
    """
    Render HTML content with default template and save to file.

    Args:
        html_content: The HTML content to render.
        output_file: Output HTML filename.
        output_dir: Path where HTML file should be written.
    """
    try:
        now = datetime.now(timezone.utc)
        template_data = {
            "site_url": config.SITE_URL,
            "generated_date": now.astimezone(config.EVENTS_TZ).strftime(
                "%d %b %Y, %I:%M %p IST"
            ),
            "content": html_content,
        }
        default_template = read_template("default.html")
        renderer = pystache.Renderer()
        content = renderer.render(default_template, template_data)
        save_html(content, "index.html", output_dir)

    except Exception as e:
        logger.error(f"Failed to render and save HTML to {output_dir}/index.html: {e}")
        raise


def generate_homepage(
    entries: list[FeedEntry],
    events: list[Event],
    failed_feeds: list[FailedFeedInfo],
    output_dir: Path,
):
    """
    Generate homepage from feed entries using Mustache templating.

    Args:
        entries: List of FeedEntry objects to include.
        events: List of Event objects to include.
        failed_feeds: List of FailedFeedInfo objects for failed feeds.
        output_dir: Path where homepage file should be written.
    """
    logger.info(
        f"Generating the homepage with {len(entries)} entries, {len(events)} events, and {len(failed_feeds)} failed feeds"
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
                    for w in [
                        "week's",
                        "week’s",
                        "weekend",
                        "biweek",
                        "midweek",
                        "semiweek",
                        "yesterweek",
                    ]
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
        "webcal_url": config.WEBCAL_URL,
        "upcoming_events": upcoming_events,
        "has_upcoming_events": len(upcoming_events) > 0,
        "previous_events": previous_events[: config.MAX_SHOWN_EVENTS],
        "entries": group_feed_entries(other_entries),
        "week_notes": group_feed_entries(week_notes)[: config.MAX_SHOWN_WEEK_NOTES],
        "failed_feeds": failed_feeds,
        "has_failed_feeds": len(failed_feeds) != 0,
    }

    index_template = read_template("index.html")
    try:
        renderer = pystache.Renderer()
        # Generate index.html
        render_and_save_html(
            html_content=renderer.render(index_template, template_data),
            output_dir=output_dir,
        )
    except Exception as e:
        logger.error(f"Failed to generate the homepage: {e}")
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

    feed_info = FeedInfo(
        title="IndieWebClub Bangalore Blogroll",
        xml_url=feed_url,
        html_url=config.SITE_URL,
    )

    generate_feed(
        feed_info=feed_info,
        author_name="IndieWebClub Bangalore",
        feed_subtitle="Recent posts by IndieWebClub Bangalore folks.",
        entries=entries,
        output_path=output_path,
    )

    logger.info(f"Blogroll feed written to: {output_path}")


def generate_events_feed(events: list[Event], output_dir: Path):
    """
    Creates an Atom feed from a list of Event objects.

    Args:
        events: A list of Event objects to include in the feed.
        output_dir: Path where Atom file should be written.
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
    Creates a Calendar from a list of Event objects.

    Args:
        events: A list of Event objects to include in the calendar.
        output_dir: Path where Calendar file should be written.
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


def generate_webring(
    entries: list[FeedEntry],
    failed_feeds: list[FailedFeedInfo],
    output_dir: Path,
):
    """
    Generate webring redirect files.

    Selects two random feeds from those with entries or filtered entries.

    Args:
        entries: List of FeedEntry objects.
        failed_feeds: List of FailedFeedInfo objects.
        output_dir: Path where HTML files should be written.
    """
    # Collect all feeds with entries
    feeds_with_entries: dict[str, FeedInfo] = {}
    for entry in entries:
        feeds_with_entries[entry.feed_home_url] = FeedInfo(
            title=entry.feed_title,
            xml_url=entry.feed_url,
            html_url=entry.feed_home_url,
        )

    # Add failed feeds that were filtered (had entries but all filtered out)
    for failed in failed_feeds:
        if failed.reason == FailureReason.ALL_FILTERED:
            feeds_with_entries[failed.feed_info.html_url] = failed.feed_info

    # Need at least 2 feeds for webring
    if len(feeds_with_entries) < 2:
        logger.warning(
            f"Not enough feeds for webring (need 2, have {len(feeds_with_entries)})"
        )
        return

    # Select 2 random feeds
    [prev_link, next_link] = random.sample(list(feeds_with_entries.values()), 2)

    template_content = read_template("webring-redirect.html")
    renderer = pystache.Renderer()

    save_html(
        renderer.render(
            template_content, {"title": prev_link.title, "url": prev_link.html_url}
        ),
        "webring/previous.html",
        output_dir,
    )
    logger.info(f"Generated webring previous link: {prev_link.html_url}")

    save_html(
        renderer.render(
            template_content, {"title": next_link.title, "url": next_link.html_url}
        ),
        "webring/next.html",
        output_dir,
    )
    logger.info(f"Generated webring previous link: {next_link.html_url}")


def generate_website(opml_path: Path, output_dir: Path, use_cache: bool):
    """
    Generate the complete website from OPML feeds, events, and static pages.

    Args:
        opml_path: Path to the OPML file containing feed URLs.
        output_dir: Path where generated artifacts should be written.
        use_cache: Whether to use cached feeds.
    """
    with ThreadPoolExecutor() as executor:
        futures: list[Future[Any]] = []

        # Copy OPML file
        futures.append(
            executor.submit(shutil.copyfile, opml_path, output_dir.joinpath(opml_path))
        )

        # Copy assets
        def copy_asset(asset_path: str) -> None:
            src = Path(asset_path)
            dst = output_dir.joinpath(src.name)
            if src.exists():
                shutil.copyfile(src, dst)
                logger.debug(f"Copied asset: {src} -> {dst}")

        futures.extend((executor.submit(copy_asset, asset) for asset in config.ASSETS))

        # Generate static pages from markdown files
        markdown_files = sorted(Path("./pages/").glob("*.md"))
        for md_file in markdown_files:
            futures.append(
                executor.submit(
                    render_and_save_html,
                    markdown_to_html(md_file),
                    output_dir / md_file.stem,
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
        failed_feeds.sort(key=lambda f: f.feed_info.title.lower())
        futures.append(executor.submit(generate_blogroll_feed, entries, output_dir))
        futures.append(
            executor.submit(generate_webring, entries, failed_feeds, output_dir)
        )

        events = events_future.result()
        generate_homepage(entries, events, failed_feeds, output_dir)

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
