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
from dataclasses import dataclass, field
from urllib.parse import urlparse
from datetime import datetime, timezone
from pathlib import Path

# Add parent directory to path so we can import src modules
sys.path.insert(0, str(Path(__file__).parent.parent))

from src import config
from pygments import highlight
from pygments.lexers import HtmlLexer
from pygments.formatters import HtmlFormatter

from src.archive import (
    generate_archive_index,
    generate_archive_year,
    group_entries_by_year,
)
from src.build import Build
from src.events import (
    Event,
    fetch_events,
    generate_events_calendar,
    generate_events_feed,
)
from src.feeds import (
    FailedFeedInfo,
    FailureReason,
    FeedEntry,
    FeedInfo,
    entry_ctx,
    fetch_all_feeds,
    generate_blogroll_feed,
    group_feed_entries,
    parse_opml_file,
    prepend_fediverse_creator,
    separate_weeknote_entries,
)
from src.member_dir import generate_members_page
from src.newsletter import generate_newsletter_page
from src.utils import (
    add_ref_param,
    make_renderer,
    markdown_to_html,
    read_template,
    render_and_save_html,
    save_html,
)

# Configure logging
logging.basicConfig(level=logging.INFO, format=config.LOG_FORMAT)
logger = logging.getLogger(__name__)


def generate_homepage(
    all_entries: list[FeedEntry],
    weeknote_entries: list[FeedEntry],
    other_entries: list[FeedEntry],
    on_this_day_entries: list[FeedEntry],
    events: list[Event],
    failed_feeds: list[FailedFeedInfo],
    output_dir: Path,
):
    """
    Generate homepage from feed entries using Mustache templating.

    Args:
        all_entries: List of all FeedEntry objects fetched from feeds.
        weeknote_entries: List of FeedEntry objects to include, which are week notes.
        other_entries: List of FeedEntry objects to include, which are not week notes.
        on_this_day_entries: List of FeedEntry objects from same day in previous years.
        events: List of Event objects to include.
        failed_feeds: List of FailedFeedInfo objects for failed feeds.
        output_dir: Path where homepage file should be written.
    """
    logger.info(
        f"Generating the homepage with {len(weeknote_entries) + len(other_entries)} entries, {len(on_this_day_entries)} on this day entries, {len(events)} events, and {len(failed_feeds)} failed feeds"
    )

    now = datetime.now(timezone.utc)
    previous_events = [event for event in events if event.start_at <= now]
    upcoming_events = [event for event in events if event.start_at > now]
    upcoming_events.reverse()

    current_year = datetime.now(config.EVENTS_TZ).year

    def past_entry_ctx(entry: FeedEntry) -> dict[str, str | bool]:
        ctx = entry_ctx(entry)
        years_ago = current_year - entry.published.year
        ctx.update(
            {
                "years_ago": f"{years_ago}",
                "years_text": (
                    f"{years_ago} year" if years_ago == 1 else f"{years_ago} years"
                ),
            }
        )
        return ctx

    # Prepare template data
    shown_entries = group_feed_entries(other_entries)[: config.MAX_SHOWN_POSTS]
    shown_weeknotes = group_feed_entries(weeknote_entries)[
        : config.MAX_SHOWN_WEEK_NOTES
    ]
    shown_on_this_day = on_this_day_entries

    all_shown = shown_entries + shown_weeknotes + shown_on_this_day
    shown_links = {e.link for e in all_shown}

    available_for_random = [e for e in all_entries if e.link not in shown_links]

    feeds_seen: set[str] = set()
    unique_feed_posts: list[FeedEntry] = []
    random.shuffle(available_for_random)
    for entry in available_for_random:
        if entry.feed_home_url not in feeds_seen:
            feeds_seen.add(entry.feed_home_url)
            unique_feed_posts.append(entry)
            if len(unique_feed_posts) >= config.MAX_SHOWN_RANDOM_POSTS:
                break

    random_posts = unique_feed_posts

    template_data = {
        "site_url": config.SITE_URL,
        "webcal_url": config.WEBCAL_URL,
        "upcoming_events": upcoming_events,
        "has_upcoming_events": len(upcoming_events) > 0,
        "previous_events": previous_events[: config.MAX_SHOWN_EVENTS],
        "entries": [entry_ctx(e) for e in shown_entries],
        "week_notes": [entry_ctx(e) for e in shown_weeknotes],
        "random_posts": [entry_ctx(e) for e in random_posts],
        "has_random_posts": len(random_posts) > 0,
        "on_this_day": [past_entry_ctx(e) for e in shown_on_this_day],
        "has_on_this_day": len(shown_on_this_day) > 0,
        "failed_feeds": failed_feeds,
        "has_failed_feeds": len(failed_feeds) != 0,
    }

    index_template = read_template("index.html")
    try:
        renderer = make_renderer()
        # Generate index.html
        render_and_save_html(
            html_content=renderer.render(index_template, template_data),
            page_url="",
            output_dir=output_dir,
        )
    except Exception as e:
        logger.error(f"Failed to generate the homepage: {e}")
        raise


def get_feeds_with_entries(
    entries: list[FeedEntry],
    failed_feeds: list[FailedFeedInfo],
    feeds: list[FeedInfo],
) -> list[FeedInfo]:
    # Collect all feeds with entries
    webring_lookup = {f.html_url: f.webring for f in feeds}

    feeds_with_entries: dict[str, FeedInfo] = {}
    for entry in entries:
        feeds_with_entries[entry.feed_home_url] = FeedInfo(
            title=entry.feed_title,
            xml_url=entry.feed_url,
            html_url=entry.feed_home_url,
            webring=webring_lookup.get(entry.feed_home_url, False),
        )

    # Add failed feeds that were filtered (had entries but all filtered out)
    for failed in failed_feeds:
        if failed.reason == FailureReason.ALL_FILTERED:
            feeds_with_entries[failed.feed_info.html_url] = failed.feed_info

    return list(feeds_with_entries.values())


@dataclass
class BuildCache:
    feeds: list[FeedInfo] = field(default_factory=list)
    entries: list[FeedEntry] = field(default_factory=list)
    weeknote_entries: list[FeedEntry] = field(default_factory=list)
    other_entries: list[FeedEntry] = field(default_factory=list)
    on_this_day_entries: list[FeedEntry] = field(default_factory=list)
    failed_feeds: list[FailedFeedInfo] = field(default_factory=list)
    feeds_with_entries: list[FeedInfo] = field(default_factory=list)
    events: list[Event] = field(default_factory=list)
    fediverse_creators: dict[str, str] = field(default_factory=dict)
    entries_by_year: dict[int, list[FeedEntry]] = field(default_factory=dict)
    webring_prev_next: dict[str, tuple[FeedInfo, FeedInfo]] = field(
        default_factory=dict
    )
    webring_legacy: dict[str, FeedInfo] = field(default_factory=dict)


def generate_website(
    opml_path: Path, output_dir: Path, use_cache: bool, cache_fallback: bool
):
    """
    Generate the complete website from OPML feeds, events, and static pages.

    Args:
        opml_path: Path to the OPML file containing feed URLs.
        output_dir: Path where generated artifacts should be written.
        use_cache: Whether to use cached feeds.
        cache_fallback: Whether to fall back to cached feeds on fetch failure.
    """
    cache = BuildCache()
    build = Build()
    pages_path = Path("./pages/")
    page_targets = [f"page:{f.stem}" for f in pages_path.glob("*.md")] + [
        f"page:{f.relative_to(pages_path)}" for f in pages_path.glob("*/index.html")
    ]

    @build.rule("blogroll_opml")
    def _(_target: str):
        _ = shutil.copyfile(opml_path, output_dir / opml_path.name)

    @build.rule("asset:*")
    def _(asset: str):
        src = Path(asset)
        if src.exists():
            dst = output_dir / src.name
            _ = shutil.copyfile(src, dst)
            logger.debug(f"Copied asset: {src} -> {dst}")
        else:
            logger.warn(f"Asset does not exist: {src}")

    @build.rule("page:*/index.html")
    def _(page_name: str):
        src = Path(f"./pages/{page_name}")
        if src.exists():
            dst = output_dir / page_name
            dst.parent.mkdir(exist_ok=True)
            _ = shutil.copyfile(src, dst)
            logger.debug(f"Copied page: {src} -> {dst}")

    @build.rule("page:*")
    def _(page_name: str):
        md_file = Path(f"./pages/{page_name}.md")
        render_and_save_html(
            html_content=markdown_to_html(md_file),
            page_url=f"{page_name}/",
            output_dir=output_dir / page_name,
        )

    @build.rule("webring_page")
    def _(_target: str):
        webring_template = read_template("webring.html")
        webring_content = webring_renderer.render(
            webring_template, {"site_url": config.SITE_URL}
        )
        render_and_save_html(
            html_content=webring_content,
            page_url="webring/",
            output_dir=output_dir / "webring",
        )

    @build.rule("parsed_opml")
    def _(_target: str):
        cache.feeds = parse_opml_file(opml_path)

    @build.rule("events")
    def _(_target: str):
        cache.events = fetch_events(use_cache=use_cache, cache_fallback=cache_fallback)

    @build.rule("feeds")
    def _(_target: str):
        build.need("parsed_opml")
        cache.entries, cache.failed_feeds = fetch_all_feeds(
            cache.feeds,
            use_cache=use_cache,
            cache_fallback=cache_fallback,
        )
        cache.failed_feeds.sort(key=lambda f: f.feed_info.title.lower())
        cache.weeknote_entries, cache.other_entries = separate_weeknote_entries(
            cache.entries
        )
        cache.feeds_with_entries = get_feeds_with_entries(
            cache.entries, cache.failed_feeds, cache.feeds
        )

        now = datetime.now(config.EVENTS_TZ)
        current_month, current_day = now.month, now.day
        cache.on_this_day_entries = [
            e
            for e in cache.entries
            if e.published.month == current_month
            and e.published.day == current_day
            and e.published.year != now.year
        ]
        cache.on_this_day_entries.sort(key=lambda e: e.published, reverse=True)

        cache.entries_by_year = group_entries_by_year(cache.entries)

    @build.rule("events_feed")
    def _(_target: str):
        build.need("events")
        generate_events_feed(cache.events, output_dir)

    @build.rule("events_calendar")
    def _(_target: str):
        build.need("events")
        generate_events_calendar(cache.events, output_dir)

    @build.rule("blogroll_feed")
    def _(_target: str):
        build.need("feeds", "members_dir")
        entries = prepend_fediverse_creator(cache.entries, cache.fediverse_creators)
        generate_blogroll_feed(
            entries=entries,
            feed_name="Blogroll",
            feed_subtitle="Recent posts by IndieWebClub Bangalore folks",
            output_path=output_dir.joinpath(config.BLOGROLL_FEED_FILE),
        )

    @build.rule("weeknotes_blogroll_feed")
    def _(_target: str):
        build.need("feeds", "members_dir")
        entries = prepend_fediverse_creator(
            cache.weeknote_entries, cache.fediverse_creators
        )
        generate_blogroll_feed(
            entries=entries,
            feed_name="Week Notes Blogroll",
            feed_subtitle="Week Notes by IndieWebClub Bangalore folks",
            output_path=output_dir.joinpath(config.WEEKNOTE_BLOGROLL_FEED_FILE),
        )

    @build.rule("members_dir")
    def _(_target: str):
        build.need("feeds", "parsed_opml")
        cache.fediverse_creators = generate_members_page(
            cache.feeds_with_entries, cache.feeds, output_dir
        )

    webring_template = read_template("webring-redirect.html")
    webring_renderer = make_renderer()

    @build.rule("webring")
    def _(_target: str):
        build.need("feeds")
        feeds = [f for f in cache.feeds_with_entries if f.webring]
        if len(feeds) < 2:
            logger.warning(
                f"Not enough webring-enabled feeds (need 2, have {len(feeds)})"
            )
            return

        random.shuffle(feeds)
        n = len(feeds)
        targets = []
        for i, feed in enumerate(feeds):
            prev_feed = feeds[(i - 1) % n]
            next_feed = feeds[(i + 1) % n]
            slug = urlparse(feed.html_url).netloc.replace(".", "-")
            cache.webring_prev_next[slug] = (prev_feed, next_feed)
            targets.append(f"webring_member:{slug}")
            targets.append(f"webring_embed:{slug}")

        prev_link, next_link = random.sample(feeds, 2)
        cache.webring_legacy["previous"] = prev_link
        cache.webring_legacy["next"] = next_link
        targets.append("webring_legacy:previous")
        targets.append("webring_legacy:next")

        build.need(*targets)

    @build.rule("webring_member:*")
    def _(slug: str):
        prev_feed, next_feed = cache.webring_prev_next[slug]
        for feed, name in [(prev_feed, "previous"), (next_feed, "next")]:
            save_html(
                webring_renderer.render(
                    webring_template,
                    {
                        "title": feed.title,
                        "url": feed.html_url,
                        "url_utm": add_ref_param(feed.html_url),
                    },
                ),
                f"webring/{slug}/{name}.html",
                output_dir,
            )

    @build.rule("webring_embed:*")
    def _(slug: str):
        prev_feed, next_feed = cache.webring_prev_next[slug]
        prev_url = f"{config.SITE_URL}webring/{slug}/previous.html"
        next_url = f"{config.SITE_URL}webring/{slug}/next.html"
        embed_html = (
            '<div class="webring">\n'
            f'  <a href="{prev_url}">← Previous</a>\n'
            f'  | <a href="{config.SITE_URL}">IndieWebClub Bangalore</a> |\n'
            f'  <a href="{next_url}">Next →</a>\n'
            "</div>"
        )
        highlighted = highlight(embed_html, HtmlLexer(), HtmlFormatter())
        highlighted = highlighted.replace('class="highlight"', 'class="codehilite"')
        save_html(highlighted, f"webring/{slug}/links_embed.html", output_dir)

    @build.rule("webring_legacy:*")
    def _(kind: str):
        feed = cache.webring_legacy[kind]
        save_html(
            webring_renderer.render(
                webring_template,
                {
                    "title": feed.title,
                    "url": feed.html_url,
                    "url_utm": add_ref_param(feed.html_url),
                },
            ),
            f"webring/{kind}.html",
            output_dir,
        )
        logger.info(f"Generated webring {kind} link: {feed.html_url}")

    @build.rule("newsletter")
    def _(_target: str):
        generate_newsletter_page(output_dir)

    def get_archive_years(entries_by_year: dict[int, list[FeedEntry]]) -> list[int]:
        return [
            year
            for year in sorted(entries_by_year.keys())
            if year >= config.ARCHIVE_MIN_YEAR
        ]

    @build.rule("archive")
    def _(_target: str):
        build.need("feeds")
        archive_year_targets = [
            f"archive_year:{year}" for year in get_archive_years(cache.entries_by_year)
        ]
        build.need("archive_index", *archive_year_targets)

    @build.rule("archive_index")
    def _(_target: str):
        build.need("feeds")
        generate_archive_index(
            [
                entry
                for entry in cache.entries
                if entry.published.year >= config.ARCHIVE_MIN_YEAR
            ],
            get_archive_years(cache.entries_by_year),
            output_dir,
        )

    @build.rule("archive_year:*")
    def _(target: str):
        build.need("feeds")
        year = int(target)
        generate_archive_year(
            year,
            cache.entries_by_year[year],
            get_archive_years(cache.entries_by_year),
            output_dir,
        )

    @build.rule("homepage")
    def _(_target: str):
        build.need("feeds", "events")
        generate_homepage(
            cache.entries,
            cache.weeknote_entries,
            cache.other_entries,
            cache.on_this_day_entries,
            cache.events,
            cache.failed_feeds,
            output_dir,
        )

    @build.rule("sitemap")
    def _(_target: str):
        build.need(
            "members_dir",
            "webring",
            "webring_page",
            "newsletter",
            "archive",
            "homepage",
            *page_targets,
        )

        pages = sorted(output_dir.rglob("index.html"))
        lines = [
            '<?xml version="1.0" encoding="UTF-8"?>',
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">',
        ]
        for page in pages:
            path = page.relative_to(output_dir).parent.as_posix()
            url = config.SITE_URL if path == "." else f"{config.SITE_URL}{path}/"
            lines.append(f"  <url><loc>{url}</loc></url>")
        lines.append("</urlset>")

        sitemap_path = output_dir / "sitemap.xml"
        sitemap_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        logger.info(f"Generated sitemap with {len(pages)} URLs")

    @build.rule("website")
    def _(_target: str):
        build.need(
            "blogroll_opml",
            "members_dir",
            "events_feed",
            "events_calendar",
            "blogroll_feed",
            "weeknotes_blogroll_feed",
            "webring",
            "webring_page",
            "newsletter",
            "archive",
            "homepage",
            "sitemap",
            *[f"asset:{asset}" for asset in config.ASSETS],
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
    _ = parser.add_argument(
        "--cache-fallback",
        action="store_true",
        help="Fall back to cached feeds on fetch failure and update cache on success",
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

    if args.cache and args.cache_fallback:
        logger.error("--use-cache and --cache-fallback options cannot be used together")
        sys.exit(1)
    if args.cache:
        logger.info("Caching enabled")
    if args.cache_fallback:
        logger.info("Cache fallback enabled")
    config.CACHE_DIR.mkdir(exist_ok=True)

    try:
        generate_website(opml_path, output_dir, args.cache, args.cache_fallback)
    except KeyboardInterrupt:
        logger.info("Process interrupted by user")
        sys.exit(130)
    except Exception as e:
        logger.error(f"Fatal error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
