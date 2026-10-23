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
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

# Add parent directory to path so we can import src modules
sys.path.insert(0, str(Path(__file__).parent.parent))

from pygments import highlight
from pygments.formatters import HtmlFormatter
from pygments.lexers import HtmlLexer

from src import config
from src.archive import (
    generate_archive_index,
    generate_archive_year,
    group_entries_by_year,
)
from src.build import Build
from src.data_pages import (
    PromptsPage,
    TopicsPage,
    generate_data_page_feed,
    load_data_page,
    render_data_page,
)
from src.events import (
    Event,
    fetch_events,
    generate_events_calendar,
    generate_events_feed,
)
from src.feeds import (
    BLOGROLL_FEED_FILE,
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
from src.search import generate_search_page
from src.utils import (
    add_ref_param,
    make_renderer,
    markdown_to_html,
    read_template,
    render_and_save_html,
    save_html,
    write_file,
)

# Configure logging
logging.basicConfig(level=logging.INFO, format=config.LOG_FORMAT)
# Named explicitly: the script runs as __main__, which would not tell the
# log lines apart from the modules it calls.
logger = logging.getLogger("generator")

# List of static assets to be copied to the output directory.
ASSETS = [
    "assets/style.css",
    "assets/indiewebcamp-button.svg",
    "assets/preview.png",
    "assets/favicon.svg",
    "assets/archive.js",
    "assets/search.js",
    "assets/.nojekyll",
    "CNAME",
]


def generate_homepage(
    all_entries: list[FeedEntry],
    weeknote_entries: list[FeedEntry],
    other_entries: list[FeedEntry],
    on_this_day_entries: list[FeedEntry],
    events: list[Event],
    failed_feeds: list[FailedFeedInfo],
    output_dir: Path,
    has_search: bool = False,
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
        has_search: Whether the /search/ page is part of this build.
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
        """Build entry context with extra "N years ago" fields for on-this-day.

        Args:
            entry: Entry from a previous year.

        Returns:
            Entry context with years_ago and years_text keys added.
        """
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
        "has_search": has_search,
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
    """Collect the feeds that contributed content to this build.

    Feeds whose entries were all filtered out are still included, so they
    keep appearing in the member directory.

    Args:
        entries: All fetched entries.
        failed_feeds: Feeds that failed or had all entries filtered out.
        feeds: Feeds parsed from the OPML file, for webring membership.

    Returns:
        Deduplicated feeds with entries in entry order, followed by the
        feeds whose entries were all filtered out in failed_feeds order.
    """
    # Collect all feeds with entries
    webring_sites = {f.html_url for f in feeds if f.webring}

    feeds_with_entries: dict[str, FeedInfo] = {}
    for entry in entries:
        feeds_with_entries[entry.feed_home_url] = FeedInfo(
            title=entry.feed_title,
            xml_url=entry.feed_url,
            html_url=entry.feed_home_url,
            webring=entry.feed_home_url in webring_sites,
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
    webring_prev_next: dict[str, tuple[FeedInfo, FeedInfo, str]] = field(
        default_factory=dict
    )
    webring_legacy: dict[str, FeedInfo] = field(default_factory=dict)
    data_pages: dict[str, TopicsPage | PromptsPage] = field(default_factory=dict)
    search_page_built: bool = False


def register_pages(
    pages_path: Path,
    data_path: Path,
    data_page_classes: dict[str, type[TopicsPage | PromptsPage]],
) -> dict[str, Path]:
    page_sources: dict[str, Path] = {}

    def _register_page(target: str, src_file: Path) -> None:
        if (existing := page_sources.get(target)) is not None:
            raise ValueError(
                f"Duplicate page target {target}: {existing} and {src_file}"
            )
        page_sources[target] = src_file

    for f in pages_path.glob("*.md"):
        _register_page(f"page:{f.stem}", f)
    data_stems: set[str] = set()
    for f in data_path.glob("*.yaml"):
        data_stems.add(f.stem)
        if f.stem in data_page_classes:
            _register_page(f"page:{f.stem}", f)
    for f in pages_path.glob("*/index.html"):
        _register_page(f"page:{f.relative_to(pages_path)}", f)
    if missing := sorted(set(data_page_classes) - data_stems):
        raise ValueError(
            "data_page_classes entries without a YAML file: "
            + ", ".join(f"data/{name}.yaml" for name in missing)
        )
    return page_sources


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
    data_path = Path("./data/")
    data_page_classes: dict[str, type[TopicsPage | PromptsPage]] = {
        "topics": TopicsPage,
        "prompts": PromptsPage,
    }

    page_sources = register_pages(pages_path, data_path, data_page_classes)
    page_targets = list(page_sources)

    @build.rule("blogroll_opml")
    def _(_target: str):
        """Copy the source OPML file into the output directory."""
        _ = shutil.copyfile(opml_path, output_dir / opml_path.name)

    @build.rule("asset:*")
    def _(asset: str):
        """Copy one static asset into the output directory."""
        src = Path(asset)
        if src.exists():
            dst = output_dir / src.name
            _ = shutil.copyfile(src, dst)
            logger.info(f"Copied asset: {src} -> {dst}")
        else:
            logger.warning(f"Asset does not exist: {src}")

    @build.rule("page:*/index.html")
    def _(page_name: str):
        """Copy a static HTML page into the output directory."""
        src = Path(f"./pages/{page_name}")
        if src.exists():
            dst = output_dir / page_name
            dst.parent.mkdir(exist_ok=True)
            _ = shutil.copyfile(src, dst)
            logger.info(f"Copied page: {src} -> {dst}")

    @build.rule("data_page:*")
    def _(page_name: str):
        """Load and validate one data page's YAML file."""
        cache.data_pages[page_name] = load_data_page(
            data_path / f"{page_name}.yaml", data_page_classes[page_name]
        )
        logger.info(f"Loaded data page: {page_name}")

    @build.rule("page:*")
    def _(page_name: str):
        """Render a Markdown or data (YAML) page to HTML."""
        src = page_sources[f"page:{page_name}"]
        if src.suffix == ".yaml":
            build.need(f"data_page:{page_name}")
            html_content = render_data_page(
                cache.data_pages[page_name], f"{page_name}.html"
            )
        else:
            html_content = markdown_to_html(src)
        render_and_save_html(
            html_content=html_content,
            page_url=f"{page_name}/",
            output_dir=output_dir / page_name,
        )

    @build.rule("webring_page")
    def _(_target: str):
        """Generate the /webring/ tool page."""
        webring_template = read_template("webring.html")
        webring_content = webring_renderer.render(
            webring_template, {"site_url": config.SITE_URL}
        )
        render_and_save_html(
            html_content=webring_content,
            page_url="webring/",
            output_dir=output_dir / "webring",
        )

    @build.rule("search_page")
    def _(_target: str):
        """Generate the /search/ full-text search page."""
        build.need("feeds")
        cache.search_page_built = (
            generate_search_page(cache.entries, output_dir) is not None
        )

    @build.rule("parsed_opml")
    def _(_target: str):
        """Parse the OPML file into the build cache."""
        cache.feeds = parse_opml_file(opml_path)

    @build.rule("events")
    def _(_target: str):
        """Fetch events from Discourse into the build cache."""
        cache.events = fetch_events(use_cache=use_cache, cache_fallback=cache_fallback)

    @build.rule("feeds")
    def _(_target: str):
        """Fetch all feeds and classify entries into the build cache."""
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
        """Generate the events Atom feed."""
        build.need("events")
        generate_events_feed(cache.events, output_dir)

    @build.rule("events_calendar")
    def _(_target: str):
        """Generate the events iCalendar file."""
        build.need("events")
        generate_events_calendar(cache.events, output_dir)

    @build.rule("topics_feed")
    def _(_target: str):
        """Generate the topics Atom feed."""
        build.need("data_page:topics")
        generate_data_page_feed(
            page=cache.data_pages["topics"],
            feed_title="IndieWebClub Bangalore Topics",
            feed_subtitle="Discussion topics from IndieWebClub Bangalore.",
            output_path=output_dir / "topics.atom",
            page_url="topics/",
        )

    @build.rule("prompts_feed")
    def _(_target: str):
        """Generate the writing-prompts Atom feed."""
        build.need("data_page:prompts")
        generate_data_page_feed(
            page=cache.data_pages["prompts"],
            feed_title="IndieWebClub Bangalore Writing Prompts",
            feed_subtitle="Writing prompts from IndieWebClub Bangalore.",
            output_path=output_dir / "prompts.atom",
            page_url="prompts/",
        )

    @build.rule("blogroll_feed")
    def _(_target: str):
        """Generate the all-posts blogroll Atom feed."""
        build.need("feeds", "members_dir")
        entries = prepend_fediverse_creator(cache.entries, cache.fediverse_creators)
        generate_blogroll_feed(
            entries=entries,
            feed_name="Blogroll (All posts)",
            feed_subtitle="All posts by IndieWebClub Bangalore folks",
            output_path=output_dir.joinpath(BLOGROLL_FEED_FILE),
        )

    @build.rule("blogroll_latest_feed")
    def _(_target: str):
        """Generate the recent-posts blogroll Atom feed."""
        build.need("feeds", "members_dir")
        entries = sorted(cache.entries, key=lambda e: e.published, reverse=True)[
            : config.MAX_LATEST_FEED_ENTRIES
        ]
        entries = prepend_fediverse_creator(entries, cache.fediverse_creators)
        generate_blogroll_feed(
            entries=entries,
            feed_name="Blogroll (Recent posts)",
            feed_subtitle="Recent posts by IndieWebClub Bangalore folks",
            output_path=output_dir.joinpath("blogroll-latest.atom"),
            add_content=True,
        )

    @build.rule("weeknotes_blogroll_feed")
    def _(_target: str):
        """Generate the weeknotes-only blogroll Atom feed."""
        build.need("feeds", "members_dir")
        entries = prepend_fediverse_creator(
            cache.weeknote_entries, cache.fediverse_creators
        )
        generate_blogroll_feed(
            entries=entries,
            feed_name="Week Notes Blogroll",
            feed_subtitle="Week Notes by IndieWebClub Bangalore folks",
            output_path=output_dir.joinpath("blogroll-weeknotes.atom"),
        )

    @build.rule("members_dir")
    def _(_target: str):
        """Generate the member directory and cache fediverse creators."""
        build.need("feeds", "parsed_opml")
        cache.fediverse_creators = generate_members_page(
            cache.feeds_with_entries, cache.feeds, output_dir
        )

    webring_template = read_template("webring-redirect.html")
    webring_renderer = make_renderer()

    @build.rule("webring")
    def _(_target: str):
        """Shuffle the webring chain and demand its per-member targets."""
        build.need("feeds")
        feeds = [f for f in cache.feeds_with_entries if f.webring]
        if len(feeds) < 2:
            logger.warning(
                f"Not enough webring-enabled feeds (need 2, have {len(feeds)})"
            )
            return

        seen_slugs: set[str] = set()
        unique_feeds: list[FeedInfo] = []
        for feed in feeds:
            slug = urlparse(feed.html_url).netloc.replace(".", "-")
            if slug not in seen_slugs:
                seen_slugs.add(slug)
                unique_feeds.append(feed)
        random.shuffle(unique_feeds)
        feeds = unique_feeds
        n = len(feeds)
        targets = []
        for i, feed in enumerate(feeds):
            prev_feed = feeds[(i - 1) % n]
            next_feed = feeds[(i + 1) % n]
            slug = urlparse(feed.html_url).netloc.replace(".", "-")
            cache.webring_prev_next[slug] = (prev_feed, next_feed, feed.html_url)
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
        """Generate previous/next redirect pages for one webring member."""
        prev_feed, next_feed, feed_url = cache.webring_prev_next[slug]
        for feed, name in [(prev_feed, "previous"), (next_feed, "next")]:
            save_html(
                webring_renderer.render(
                    webring_template,
                    {
                        "title": feed.title,
                        "url": feed.html_url,
                        "url_utm": add_ref_param(
                            feed.html_url, urlparse(feed_url).netloc
                        ),
                    },
                ),
                f"webring/{slug}/{name}.html",
                output_dir,
            )

    @build.rule("webring_embed:*")
    def _(slug: str):
        """Generate pygments-highlighted embed code for one webring member."""
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
        """Generate a legacy random prev/next webring redirect page."""
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
        """Generate the newsletter subscription page."""
        generate_newsletter_page(output_dir)

    def get_archive_years(entries_by_year: dict[int, list[FeedEntry]]) -> list[int]:
        """Return the archive years at or after config.ARCHIVE_MIN_YEAR.

        Args:
            entries_by_year: Entries grouped by publication year.

        Returns:
            Sorted list of years that have entries and are archivable.
        """
        return [
            year
            for year in sorted(entries_by_year.keys())
            if year >= config.ARCHIVE_MIN_YEAR
        ]

    @build.rule("archive")
    def _(_target: str):
        """Demand the archive index and every per-year archive page."""
        build.need("feeds")
        archive_year_targets = [
            f"archive_year:{year}" for year in get_archive_years(cache.entries_by_year)
        ]
        build.need("archive_index", *archive_year_targets)

    @build.rule("archive_index")
    def _(_target: str):
        """Generate the archive index page with the monthly bar chart."""
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
        """Generate one year's archive page."""
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
        """Generate the homepage."""
        build.need("feeds", "events", "search_page")
        generate_homepage(
            cache.entries,
            cache.weeknote_entries,
            cache.other_entries,
            cache.on_this_day_entries,
            cache.events,
            cache.failed_feeds,
            output_dir,
            has_search=cache.search_page_built,
        )

    @build.rule("sitemap")
    def _(_target: str):
        """Generate sitemap.xml from every built index.html."""
        build.need(
            "members_dir",
            "webring",
            "webring_page",
            "search_page",
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
        write_file(sitemap_path, "\n".join(lines) + "\n")
        logger.info(f"Generated sitemap with {len(pages)} URLs")

    @build.rule("website")
    def _(_target: str):
        """Top-level target: demand every other target in the build."""
        build.need(
            "blogroll_opml",
            "members_dir",
            "events_feed",
            "events_calendar",
            "blogroll_feed",
            "blogroll_latest_feed",
            "weeknotes_blogroll_feed",
            "webring",
            "webring_page",
            "search_page",
            "newsletter",
            "archive",
            "homepage",
            "sitemap",
            *[f"asset:{asset}" for asset in ASSETS],
            *page_targets,
            *[f"{page_name}_feed" for page_name in data_page_classes],
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
