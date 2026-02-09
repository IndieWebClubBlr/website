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
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import pystache
from feedgen.feed import FeedGenerator
from icalendar import Calendar
from icalendar import Event as CalEvent

# Add parent directory to path so we can import src modules
sys.path.insert(0, str(Path(__file__).parent.parent))

from src import config
from src.build import Build
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
from src.member_dir import generate_members_page
from src.utils import (
    add_utm_params,
    markdown_to_html,
    read_template,
    render_and_save_html,
    save_html,
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

    def entry_ctx(entry: FeedEntry) -> dict[str, str]:
        return {
            "title": entry.title,
            "link_utm": add_utm_params(entry.link, "website", "blogroll"),
            "feed_title": entry.feed_title,
            "feed_home_url_utm": add_utm_params(
                entry.feed_home_url, "website", "blogroll"
            ),
            "published_machine": entry.published_machine(),
            "published_human": entry.published_human(),
        }

    # Prepare template data
    template_data = {
        "webcal_url": config.WEBCAL_URL,
        "upcoming_events": upcoming_events,
        "has_upcoming_events": len(upcoming_events) > 0,
        "previous_events": previous_events[: config.MAX_SHOWN_EVENTS],
        "entries": [entry_ctx(e) for e in group_feed_entries(other_entries)],
        "week_notes": [
            entry_ctx(e)
            for e in group_feed_entries(week_notes)[: config.MAX_SHOWN_WEEK_NOTES]
        ],
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


def get_feeds_with_entries(
    entries: list[FeedEntry], failed_feeds: list[FailedFeedInfo]
) -> list[FeedInfo]:
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

    return list(feeds_with_entries.values())


def generate_webring(feeds_with_entries: list[FeedInfo], output_dir: Path):
    """
    Generate webring redirect files.

    Selects two random feeds from those with entries or filtered entries.

    Args:
        entries: List of FeedEntry objects.
        failed_feeds: List of FailedFeedInfo objects.
        output_dir: Path where HTML files should be written.
    """

    # Need at least 2 feeds for webring
    if len(feeds_with_entries) < 2:
        logger.warning(
            f"Not enough feeds for webring (need 2, have {len(feeds_with_entries)})"
        )
        return

    # Select 2 random feeds
    [prev_link, next_link] = random.sample(list(feeds_with_entries), 2)

    template_content = read_template("webring-redirect.html")
    renderer = pystache.Renderer()

    save_html(
        renderer.render(
            template_content,
            {
                "title": prev_link.title,
                "url": prev_link.html_url,
                "url_utm": add_utm_params(prev_link.html_url, "website", "webring"),
            },
        ),
        "webring/previous.html",
        output_dir,
    )
    logger.info(f"Generated webring previous link: {prev_link.html_url}")

    save_html(
        renderer.render(
            template_content,
            {
                "title": next_link.title,
                "url": next_link.html_url,
                "url_utm": add_utm_params(next_link.html_url, "website", "webring"),
            },
        ),
        "webring/next.html",
        output_dir,
    )
    logger.info(f"Generated webring previous link: {next_link.html_url}")


@dataclass
class BuildCache:
    feeds: list[FeedInfo] = field(default_factory=list)
    entries: list[FeedEntry] = field(default_factory=list)
    failed_feeds: list[FailedFeedInfo] = field(default_factory=list)
    feeds_with_entries: list[FeedInfo] = field(default_factory=list)
    events: list[Event] = field(default_factory=list)


def generate_website(opml_path: Path, output_dir: Path, use_cache: bool):
    """
    Generate the complete website from OPML feeds, events, and static pages.

    Args:
        opml_path: Path to the OPML file containing feed URLs.
        output_dir: Path where generated artifacts should be written.
        use_cache: Whether to use cached feeds.
    """
    cache = BuildCache()
    build = Build()

    @build.rule("copy_opml")
    def _(_target: str):
        _ = shutil.copyfile(opml_path, output_dir / opml_path.name)

    @build.rule("copy_assets:*")
    def _(target: str):
        asset = target.split(":", 1)[1]
        src = Path(asset)
        if src.exists():
            dst = output_dir / src.name
            _ = shutil.copyfile(src, dst)
            logger.debug(f"Copied asset: {src} -> {dst}")

    @build.rule("render_page:*")
    def _(target: str):
        page_name = target.split(":", 1)[1]
        md_file = Path(f"./pages/{page_name}.md")
        render_and_save_html(markdown_to_html(md_file), output_dir / page_name)

    @build.rule("parse_opml")
    def _(_target: str):
        cache.feeds = parse_opml_file(opml_path)

    @build.rule("fetch_events")
    def _(_target: str):
        cache.events = fetch_events(use_cache=use_cache)

    @build.rule("fetch_feeds")
    def _(_target: str):
        build.need("parse_opml")
        cache.entries, cache.failed_feeds = fetch_all_feeds(
            cache.feeds, use_cache=use_cache
        )
        cache.failed_feeds.sort(key=lambda f: f.feed_info.title.lower())

    @build.rule("generate_events_feed")
    def _(_target: str):
        build.need("fetch_events")
        generate_events_feed(cache.events, output_dir)

    @build.rule("generate_events_calendar")
    def _(_target: str):
        build.need("fetch_events")
        generate_events_calendar(cache.events, output_dir)

    @build.rule("generate_blogroll")
    def _(_target: str):
        build.need("fetch_feeds")
        generate_blogroll_feed(cache.entries, output_dir)

    @build.rule("get_feeds_with_entries")
    def _(_target: str):
        build.need("fetch_feeds")
        cache.feeds_with_entries = get_feeds_with_entries(
            cache.entries, cache.failed_feeds
        )

    @build.rule("generate_members")
    def _(_target: str):
        build.need("get_feeds_with_entries")
        generate_members_page(cache.feeds_with_entries, output_dir)

    @build.rule("generate_webring")
    def _(_target: str):
        build.need("get_feeds_with_entries")
        generate_webring(cache.feeds_with_entries, output_dir)

    @build.rule("generate_homepage")
    def _(_target: str):
        build.need("fetch_feeds", "fetch_events")
        generate_homepage(cache.entries, cache.events, cache.failed_feeds, output_dir)

    @build.rule("website")
    def _(_target: str):
        asset_targets = [f"copy_assets:{asset}" for asset in config.ASSETS]
        page_targets = [f"render_page:{f.stem}" for f in Path("./pages/").glob("*.md")]

        build.need(
            "copy_opml",
            "generate_members",
            "generate_events_feed",
            "generate_events_calendar",
            "generate_blogroll",
            "generate_webring",
            "generate_homepage",
            *asset_targets,
            *page_targets,
        )
        logger.info("Website generation completed successfully")

    build.run("website")


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
