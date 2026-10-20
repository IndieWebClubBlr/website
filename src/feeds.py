from __future__ import annotations

import hashlib
import logging
import re
import xml.etree.ElementTree as ET
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, final
from urllib.parse import quote, urlparse, urlsplit, urlunsplit

import feedparser
import requests
from bs4 import BeautifulSoup
from dateutil import parser as date_parser
from feedgen.feed import FeedGenerator

from src import config
from src.utils import SessionManager, add_ref_param, cache_path

# Configure logging
logging.basicConfig(level=logging.INFO, format=config.LOG_FORMAT)
logger = logging.getLogger("feeds")


@dataclass
class FeedInfo:
    """Represents feed information from OPML."""

    title: str
    xml_url: str
    html_url: str
    webring: bool = False


class FailureReason(Enum):
    """Enum for feed failure reasons."""

    ERROR = "error"  # Network/parsing errors
    NO_ENTRIES = "no_entries"  # Feed has no entries
    ALL_FILTERED = "all_filtered"  # Entries exist but all filtered out


@dataclass
class FailedFeedInfo:
    """Represents a feed that failed with reason."""

    feed_info: FeedInfo
    reason: FailureReason


session_manager = SessionManager(
    {
        "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml",
    }
)


@final
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
        tags: list[str],
        summary: str,
        content: str = "",
    ):
        """Initialize a normalized feed entry.

        Args:
            title: Entry title.
            link: Absolute URL of the entry.
            published: Publication time in UTC.
            feed_title: Title of the feed the entry came from.
            feed_url: XML URL of the feed the entry came from.
            feed_home_url: Home URL of the feed the entry came from.
            tags: Entry tags.
            summary: First paragraph of the entry, wrapped in guillemets.
            content: Raw HTML content of the entry, if any.
        """
        self.title = title
        self.link = link
        self.published = published
        self.feed_title = feed_title
        self.feed_url = feed_url
        self.feed_home_url = feed_home_url
        self.tags = tags
        self.summary = summary
        self.content = content

    def published_human(self) -> str:
        """Format the publication date as e.g. "05 Oct 2026"."""
        return self.published.strftime("%d %b %Y")

    def published_machine(self) -> str:
        """Return the publication time as an ISO 8601 string."""
        return self.published.isoformat()

    def __repr__(self) -> str:
        """Return a developer-friendly representation of the entry."""
        return f"""FeedEntry(title={self.title!r},
          link={self.link!r},
          published={self.published!r},
          feed_title={self.feed_title!r},
          summary={self.summary!r})"""


def entry_ctx(entry: FeedEntry) -> dict[str, str | bool]:
    """Build a template context dict for a FeedEntry.

    Args:
        entry: Entry to build context for.

    Returns:
        Dict with entry_id, title, link_utm, feed_title, feed_home_url_utm,
        published_machine, published_human, and summary keys.
    """
    entry_id = hashlib.sha256(entry.link.encode()).hexdigest()[:16]
    return {
        "entry_id": entry_id,
        "title": entry.title,
        "link_utm": add_ref_param(entry.link),
        "feed_title": entry.feed_title,
        "feed_home_url_utm": add_ref_param(entry.feed_home_url),
        "published_machine": entry.published_machine(),
        "published_human": entry.published_human(),
        "summary": entry.summary,
    }


def parse_opml_file(opml_path: Path) -> list[FeedInfo]:
    """
    Parse OPML file and extract feed URLs with their titles and home page URLs.

    Args:
        opml_path: Path to the OPML file.

    Returns:
        List of FeedInfo objects.

    Raises:
        FileNotFoundError: If OPML file doesn't exist.
        ET.ParseError: If OPML file is malformed.
    """
    logger.info(f"Parsing OPML file: {opml_path}")

    try:
        tree = ET.parse(opml_path)
        root = tree.getroot()

        feeds: list[FeedInfo] = []

        # Look for outline elements with xmlUrl attribute
        for outline in root.iter("outline"):
            xml_url = outline.get("xmlUrl")
            if xml_url:
                title = outline.get("title") or outline.get("text")
                html_url = outline.get("htmlUrl")
                if title is None:
                    logger.error(f"OPML feed {xml_url} does not have title or text")
                    raise
                if html_url is None:
                    logger.warning(
                        f"OPML feed {title} does not have htmlUrl, using feed domain"
                    )
                    parsed_url = urlparse(xml_url)
                    html_url = f"{parsed_url.scheme}://{parsed_url.netloc}/"
                webring = outline.get("webring", "").lower() == "true"
                feeds.append(
                    FeedInfo(
                        title=title, xml_url=xml_url, html_url=html_url, webring=webring
                    )
                )
                logger.debug(f"Found feed: {title} -> {xml_url} -> {html_url}")

        logger.info(f"Found {len(feeds)} feeds in OPML file")
        return feeds

    except FileNotFoundError:
        logger.error(f"OPML file not found: {opml_path}")
        raise
    except ET.ParseError as e:
        logger.error(f"Failed to parse OPML file: {e}")
        raise


def generate_feed(
    feed_info: FeedInfo,
    author_name: str | None,
    feed_subtitle: str | None,
    entries: list[FeedEntry],
    output_path: Path,
    add_content: bool = False,
):
    """
    Creates an Atom feed from a list of FeedEntry objects.

    Args:
        feed_info: FeedInfo object containing feed title, URL, and home URL.
        author_name: Author name (optional).
        feed_subtitle: Feed subtitle (optional).
        entries: A list of FeedEntry objects to include in the feed.
        output_path: Path where Atom file should be written.
        add_content: If True, include each entry's HTML content instead of a summary.
    """
    fg = FeedGenerator()

    fg.id(feed_info.xml_url)
    fg.title(feed_info.title)
    if author_name is not None:
        fg.author(name=author_name)
    fg.link(href=feed_info.xml_url, rel="self")
    fg.link(href=feed_info.html_url, rel="alternate")
    if feed_subtitle is not None:
        fg.subtitle(feed_subtitle)

    feed_updated = None
    for entry in sorted(
        entries, key=lambda entry: (entry.published, entry.link), reverse=True
    ):
        if entry.link.startswith("http"):
            fe = fg.add_entry(order="append")

            fe.id(entry.link)
            fe.title(entry.title)
            fe.link(href=add_ref_param(entry.link), rel="alternate")
            fe.published(entry.published)
            fe.updated(entry.published)
            fe.author(name=entry.feed_title, uri=entry.feed_url)
            if add_content and entry.content:
                fe.content(content=entry.content, type="html")
            elif entry.summary:
                fe.summary(summary=entry.summary, type="text")

            for tag in entry.tags:
                fe.category(term=tag)

            if feed_updated is None or feed_updated < entry.published:
                feed_updated = entry.published

    fg.updated(feed_updated or datetime.now())
    fg.atom_file(output_path, pretty=True)


def fetch_feed_content(url: str) -> str | None:
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

        response = session_manager.get().get(
            url, timeout=config.REQUEST_TIMEOUT, stream=True
        )
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


def _fetch_feed_content_cached(
    feed_url: str, use_cache: bool, cache_fallback: bool
) -> str | None:
    """Fetch feed content, reading from and writing to the fetch cache.

    Args:
        feed_url: Feed URL to fetch.
        use_cache: Whether to use cached content instead of fetching.
            A cache miss fetches live and warms the cache, so repeated
            `--cache` runs only need the network on their first run.
        cache_fallback: Whether to fall back to cached content on fetch
            failure and update the cache on success.

    Returns:
        Feed content, or None if it could not be obtained.
    """
    cache_file = cache_path(feed_url)

    if use_cache and cache_file.exists():
        logger.debug(f"Using cached content for: {feed_url}")
        return cache_file.read_text(encoding="utf-8")

    content = fetch_feed_content(feed_url)
    if content and (cache_fallback or use_cache):
        cache_file.write_text(content, encoding="utf-8")
    if not content:
        if cache_fallback and cache_file.exists():
            logger.info(f"Using cached content as fallback for: {feed_url}")
            return cache_file.read_text(encoding="utf-8")
        return None
    return content


def prepend_fediverse_creator(
    entries: list[FeedEntry], fediverse_creators: dict[str, str]
) -> list[FeedEntry]:
    """Prepend fediverse creator to entry summaries if available.

    Args:
        entries: Entries to annotate (mutated in place).
        fediverse_creators: Mapping of feed home URL to creator handle.

    Returns:
        The same list of entries, with summaries prefixed where a creator
        handle exists and the summary is not already prefixed with it.
    """
    for entry in entries:
        creator = fediverse_creators.get(entry.feed_home_url)
        if creator and not entry.summary.startswith(f"{creator}: "):
            entry.summary = f"{creator}: {entry.summary}"
    return entries


MATAROA_DOMAIN = "mataroa.blog"


def is_mataroa_host(host: str | None) -> bool:
    """Return True if host is mataroa.blog or one of its subdomains.

    Args:
        host: Hostname to check, or None.

    Returns:
        True for "mataroa.blog" and any "*.mataroa.blog" host.
    """
    h = (host or "").lower()
    return h == MATAROA_DOMAIN or h.endswith("." + MATAROA_DOMAIN)


def normalize_link(link: str, feed_url: str) -> str:
    """
    Canonicalize an entry link: absolute, percent-encoded, and free of
    syndication tracking parameters (ref, utm_*).

    Stripping at this single choke point keeps `entry.link` canonical no
    matter the source (raw member feed, the normalized Atom our cache
    writer produces, or an archive snapshot whose hrefs carry our ref
    param), so link dedupe and entry ids are idempotent across builds.
    `%` is kept safe so already-percent-encoded links are neither
    double-encoded (`%20` -> `%2520`) nor grow a `25` layer per pass —
    the archive self-loop re-ingests our own hrefs every build.

    Args:
        link: The link to normalize (can be absolute or relative).
        feed_url: The feed URL to extract the domain from.

    Returns:
        Absolute URL with percent-encoded characters and tracking
        parameters removed.
    """
    if not link:
        return link

    if (
        link.startswith("http://") or link.startswith("https://")
    ) and not link.startswith("http://localhost:"):
        return strip_tracking_params(quote(link, safe=":/?#[]@!$&'()*+,;=%"))

    parsed = urlparse(feed_url)
    link_parts = urlsplit(link)
    absolute_url = link_parts._replace(
        scheme=parsed.scheme,
        netloc=parsed.netloc,
        path=link_parts.path.removeprefix(parsed.netloc),
    ).geturl()
    return strip_tracking_params(
        quote(absolute_url, safe=":/?#[]@!$&'()*+,;=%").strip()
    )


def strip_tracking_params(url: str) -> str:
    """Strip syndication tracking parameters (ref, utm_*) from a URL.

    Our own generated feeds add `ref=` to every entry href, and seed
    archives may carry other campaign parameters; stripping them keeps
    links canonical so archive round-trips deduplicate against the clean
    links member feeds publish. Idempotent: an already-canonical URL is
    returned unchanged.

    Args:
        url: URL to canonicalize.

    Returns:
        URL with ref and utm_* query parameters removed.
    """
    parts = urlsplit(url)
    kept = [
        pair
        for pair in parts.query.split("&")
        if pair and not pair.startswith(("ref=", "utm_"))
    ]
    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path, "&".join(kept), parts.fragment)
    )


def parse_feed_date(date_string: str) -> datetime | None:
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


def strip_html(text: str) -> str:
    """Remove HTML tags from text.

    Args:
        text: Text, possibly containing HTML tags.

    Returns:
        The text content of the markup, stripped of surrounding whitespace.
    """
    if not text:
        return ""
    return BeautifulSoup(text, "html.parser").get_text().strip()


def get_first_para_text(html: str) -> str:
    """Extract text from the first paragraph in HTML, or first para if plain text.

    Args:
        html: HTML or plain text to extract from.

    Returns:
        Text of the first non-empty paragraph, or "" if there is none.
    """
    if not html:
        return ""
    soup = BeautifulSoup(html, "html.parser")
    paragraphs = soup.find_all("p")
    for p in paragraphs:
        text = p.get_text().strip()
        if text:
            return text

    paragraphs = html.strip().split("\n\n")
    first_para = paragraphs[0] if paragraphs else html
    return strip_html(first_para)


def truncate_at_word(text: str, max_length: int) -> str:
    """Truncate text at word boundary, adding ellipsis if truncated.

    Args:
        text: Text to truncate.
        max_length: Length to cut the text at before the word-boundary trim.

    Returns:
        The text unchanged if it fits, otherwise cut at the last word
        boundary within the first max_length characters (or at max_length
        if there is no word boundary) followed by "…". The result can be
        one character longer than max_length because of the ellipsis.
    """
    if len(text) <= max_length:
        return text
    truncated = text[:max_length]
    last_space = truncated.rfind(" ")
    if last_space > 0:
        truncated = truncated[:last_space]
    return truncated + "…"


def extract_summary(entry, feed_title: str, title: str, link: str) -> str:
    """Extract and normalize summary from a feed entry.

    Args:
        entry: Feedparser entry to extract the summary from.
        feed_title: Unused.
        title: Unused.
        link: Unused.

    Returns:
        First paragraph of the entry truncated at config.MAX_SUMMARY_LENGTH,
        or "" if the entry has no textual content.
    """
    html_content = None

    if hasattr(entry, "summary"):
        html_content = entry.summary

    if not html_content and hasattr(entry, "content") and entry.content:
        content = entry.content[0]
        if hasattr(content, "value"):
            html_content = content.value

    if not html_content:
        return ""

    plain = get_first_para_text(html_content)

    if not plain:
        return ""

    # Peel accumulated @handle: prefixes and «» layers back to raw text
    # before truncating. prepend_fediverse_creator adds one @handle: prefix
    # on every publish, and the site re-ingests its own feed as an archive
    # source, so summaries can arrive as `«@a: @b: «text»»`. Normalizing
    # here (before the length check) keeps re-processing byte-for-byte
    # stable: the same post yields the same summary from its member feed
    # and from any archive round trip.
    while True:
        stripped = re.sub(r"^(?:@[^:]+:\s*)+", "", plain)
        if stripped != plain:
            plain = stripped
            continue
        if len(plain) >= 2 and plain.startswith("«") and plain.endswith("»"):
            plain = plain[1:-1]
            continue
        break

    if len(plain) > config.MAX_SUMMARY_LENGTH:
        plain = truncate_at_word(plain, config.MAX_SUMMARY_LENGTH)

    return plain


def wrap_summary_once(summary: str) -> str:
    """Wrap a summary in «» exactly once.

    Tolerates a leading fediverse creator prefix (``@handle: …``): the
    published ``blogroll.atom`` is itself an archive source, so a summary
    that already carries a prefix and guillemets must pass through
    unchanged — otherwise the self-loop stacks one wrap and one prepend
    per build.

    Args:
        summary: Plain-text summary, possibly creator-prefixed.

    Returns:
        The summary wrapped in «» unless it is already wrapped (behind at
        most one creator prefix).
    """
    if not summary:
        return summary
    body = re.sub(r"^@[^:]+:\s*", "", summary, count=1)
    if not body or body.startswith("«"):
        return summary
    return f"«{summary}»"


def extract_content(entry) -> str:
    """Extract HTML content from a feed entry. Empty if the entry has no content field.

    Args:
        entry: Feedparser entry to extract content from.

    Returns:
        Entry's content (or summary as a fallback), "" if neither exists.
    """
    if hasattr(entry, "content") and entry.content:
        content = entry.content[0]
        if hasattr(content, "value") and content.value:
            return content.value

    if hasattr(entry, "summary"):
        return entry.summary

    return ""


def parse_feed(
    feed_title: str, feed_url: str, feed_content: str
) -> tuple[list[FeedEntry], bool | None]:
    """
    Parse feed content and extract recent entries.

    Args:
        feed_title: Title of the feed.
        feed_url: XML URL of the feed, used to resolve relative links and
            to filter entries from other mataroa blogs.
        feed_content: Raw feed content.

    Returns:
        List of FeedEntry objects, and if the feed had any entries originally.
    """
    try:
        logger.debug(f"Parsing feed: {feed_title}")

        # Parse with feedparser
        parsed_feed = feedparser.parse(feed_content)

        if parsed_feed.bozo and hasattr(parsed_feed, "bozo_exception"):
            logger.debug(
                f"Feed parser warning for {feed_title}: {parsed_feed.bozo_exception}"
            )

        # Calculate recency cutoff
        now = datetime.now(timezone.utc)
        early_cutoff_time = now - timedelta(hours=config.MIN_FEED_ENTRY_AGE_HOURS)

        feed_host = urlparse(feed_url).hostname
        is_mataroa = is_mataroa_host(feed_host)

        entries: list[FeedEntry] = []

        has_entries = False
        for entry in parsed_feed.entries:
            has_entries = True
            # Extract and normalize entry data
            title = getattr(entry, "title", None)
            link = getattr(entry, "link", None)
            if title:
                title = title.strip()

            # Validate title
            if not title:
                logger.warning(
                    f"Feed '{feed_title}': Skipping entry with empty title (link: {link})"
                )
                continue

            link = normalize_link(link, feed_url)
            # Validate link
            if not link:
                logger.warning(
                    f"Feed '{feed_title}': Skipping entry '{title}' with empty link"
                )
                continue

            # Mataroa feeds intermittently include entries from a different
            # mataroa blog; drop entries that are not from this feed's blog.
            if is_mataroa and urlparse(link).hostname != feed_host:
                logger.warning(
                    f"Feed '{feed_title}': Dropping entry from foreign blog"
                    f" (link: {link})"
                )
                continue

            # Parse publication date
            published = None
            for date_field in ["published", "updated", "created"]:
                date_value = getattr(entry, date_field, None)
                if date_value:
                    published = parse_feed_date(date_value)
                    if published:
                        break

            if not published:
                logger.warning(
                    f"Feed '{feed_title}': Entry '{title}' has no valid date"
                )

            # Skip entries without valid dates or still too recent
            if not published or published > early_cutoff_time:
                continue

            tags = [
                tag.get("label") or tag.get("term")
                for tag in getattr(entry, "tags", [])
            ]

            summary = extract_summary(entry, feed_title, title, link)
            content = extract_content(entry)

            entries.append(
                FeedEntry(
                    title=title,
                    link=link,
                    published=published,
                    feed_title=feed_title,
                    feed_url=feed_url,
                    feed_home_url=parsed_feed.feed.link,
                    tags=[tag for tag in tags if tag is not None],
                    summary=wrap_summary_once(summary),
                    content=content,
                )
            )

        # Sort by publication date (newest first) and take top N
        entries.sort(key=lambda x: x.published, reverse=True)
        entries = entries[: config.MAX_FEED_ENTRIES]

        logger.debug(f"Extracted {len(entries)} recent entries from {feed_title}")
        return entries, has_entries

    except Exception as e:
        logger.warning(f"Failed to parse feed {feed_title}: {e}")
        return [], None


def process_single_feed(
    feed_info: FeedInfo, use_cache: bool, cache_fallback: bool
) -> tuple[list[FeedEntry], FailureReason | None]:
    """
    Process a single feed: fetch and parse it.

    Args:
        feed_info: FeedInfo object containing feed metadata.
        use_cache: Whether to use cached content instead of fetching.
        cache_fallback: Whether to fall back to cached content on fetch failure
            and update the cache on success.

    Returns:
        Tuple of (entries list, failure_reason). failure_reason is None if successful.
    """
    feed_title = feed_info.title
    feed_url = feed_info.xml_url
    home_url = feed_info.html_url

    content = _fetch_feed_content_cached(feed_url, use_cache, cache_fallback)
    if content is None:
        return [], FailureReason.ERROR

    # Parse feed content
    entries, has_entries = parse_feed(feed_title, feed_url, content)

    if len(entries) == 0:
        logger.info(f"Processed {feed_title}: 0 entries")
        if has_entries is None:
            return [], FailureReason.ERROR
        elif has_entries:
            return [], FailureReason.ALL_FILTERED
        else:
            return [], FailureReason.NO_ENTRIES

    for entry in entries:
        entry.feed_home_url = home_url

    if use_cache:
        # Save content to cache
        generate_feed(
            feed_info=feed_info,
            author_name=entries[0].feed_title,
            feed_subtitle=None,
            entries=entries,
            output_path=cache_path(feed_url),
        )
        logger.debug(f"Cached content for: {feed_url}")

    logger.info(f"Processed {feed_title}: {len(entries)} entries")
    return entries, None


def attribute_archive_entry(
    entry: Any,
    link: str,
    member_feeds: list[FeedInfo],
    by_xml: dict[str, FeedInfo],
    by_domain: dict[str, FeedInfo],
    by_title: dict[str, FeedInfo],
) -> FeedInfo | None:
    """Attribute one archive entry to an OPML member feed.

    Cascade (first hit wins, link must already be canonicalized):
      1. exact match of the entry's author uri against an OPML feed url
      2. longest OPML site-url prefix of the entry link (boundary-guarded
         so `/posts` can never claim a link to `/postsmith`)
      3. link domain (first OPML feed wins)
      4. author name against member titles (last OPML feed wins)
    Returns None when no tier matches; the caller drops the entry.

    Args:
        entry: feedparser entry (provides author_detail/author).
        link: Canonicalized entry link.
        member_feeds: OPML member feeds in file order (tie-breaks tier 2).
        by_xml: Feed-url -> member, from tier 1.
        by_domain: Host -> member, from tier 3.
        by_title: Lowercased title -> member, from tier 4.

    Returns:
        The attributed member feed, or None if unattributed.
    """
    # Tier 1: exact feed-url match from the entry's author uri.
    author_detail = getattr(entry, "author_detail", None)
    href = author_detail.get("href") if author_detail else None
    if href and href in by_xml:
        return by_xml[href]

    # Tier 2: longest OPML html_url prefix of the entry link.
    best: FeedInfo | None = None
    best_base = ""
    for member in member_feeds:
        base = member.html_url
        if base.endswith("/"):
            hit = link == base or link.startswith(base)
        else:
            hit = (
                link == base
                or link.startswith(base + "/")
                or link.startswith(base + "?")
            )
        if hit and len(base) > len(best_base):
            best, best_base = member, base
    if best is not None:
        return best

    # Tier 3: link domain.
    host = (urlparse(link).hostname or "").removeprefix("www.")
    member = by_domain.get(host) if host else None
    if member is not None:
        return member

    # Tier 4: author title.
    author = getattr(entry, "author", None)
    if author:
        member = by_title.get(author.strip().lower())
        if member is not None:
            return member

    return None


def process_archive_feed(
    feed_info: FeedInfo,
    member_feeds: list[FeedInfo],
    use_cache: bool,
    cache_fallback: bool,
) -> tuple[list[FeedEntry], FailureReason | None]:
    """Fetch an archive feed and attribute its entries to blogroll members.

    Archive feeds (the site's own blogroll feed, plus optional seed
    archives) are snapshots of member posts. Entry links are canonicalized
    by stripping syndication tracking parameters so they deduplicate
    against the clean links member feeds publish, and each entry is
    attributed to an OPML member by the cascade in
    `attribute_archive_entry` (exact feed-url match, then longest site-url
    prefix of the link, then link domain, then author name). Entries that
    match no member are dropped. Entries are built like member entries
    (same summary handling and recency filter, no per-feed cap) so the
    merged corpus is a fixed point across builds. Fetch failures are the
    caller's concern; this never touches the member-feed failure list.

    Args:
        feed_info: Archive feed to process.
        member_feeds: OPML member feeds used for per-entry attribution.
        use_cache: Whether to use cached content instead of fetching.
        cache_fallback: Whether to fall back to cached content on fetch
            failure and update the cache on success.

    Returns:
        Tuple of (entries list, failure_reason). failure_reason is None
        if successful.
    """
    content = _fetch_feed_content_cached(feed_info.xml_url, use_cache, cache_fallback)
    if content is None:
        return [], FailureReason.ERROR

    parsed_feed = feedparser.parse(content)
    if parsed_feed.bozo and hasattr(parsed_feed, "bozo_exception"):
        logger.debug(
            f"Feed parser warning for {feed_info.title}:"
            f" {parsed_feed.bozo_exception}"
        )

    by_xml = {member.xml_url: member for member in member_feeds}
    by_domain: dict[str, FeedInfo] = {}
    for member in member_feeds:
        host = (urlparse(member.html_url).hostname or "").removeprefix("www.")
        if host:
            by_domain.setdefault(host, member)
    by_title = {member.title.strip().lower(): member for member in member_feeds}

    early_cutoff_time = datetime.now(timezone.utc) - timedelta(
        hours=config.MIN_FEED_ENTRY_AGE_HOURS
    )

    entries: list[FeedEntry] = []
    dropped_unattributed = 0
    for entry in parsed_feed.entries:
        title = getattr(entry, "title", None)
        raw_link = getattr(entry, "link", None)
        if title:
            title = title.strip()

        if not title or not raw_link:
            logger.warning(
                f"Archive entry missing title or link in"
                f" {feed_info.title}: {getattr(entry, 'link', 'no link')}"
            )
            continue

        link = normalize_link(raw_link, feed_info.xml_url)

        member = attribute_archive_entry(
            entry, link, member_feeds, by_xml, by_domain, by_title
        )
        if member is None:
            dropped_unattributed += 1
            if dropped_unattributed <= 5:
                logger.warning(
                    f"Archive '{feed_info.title}': Dropping unattributed"
                    f" entry (title: {title!r}, link: {link!r},"
                    f" author: {getattr(entry, 'author', None)!r})"
                )
            continue
        feed_title = member.title
        feed_url = member.xml_url
        feed_home_url = member.html_url

        published = None
        for date_field in ["published", "updated", "created"]:
            date_value = getattr(entry, date_field, None)
            if date_value:
                published = parse_feed_date(date_value)
                if published:
                    break
        if not published or published > early_cutoff_time:
            continue

        tags = [
            tag.get("label") or tag.get("term") for tag in getattr(entry, "tags", [])
        ]
        summary = extract_summary(entry, feed_title, title, link)
        content_html = extract_content(entry)

        entries.append(
            FeedEntry(
                title=title,
                link=link,
                published=published,
                feed_title=feed_title,
                feed_url=feed_url,
                feed_home_url=feed_home_url,
                tags=[tag for tag in tags if tag is not None],
                summary=wrap_summary_once(summary),
                content=content_html,
            )
        )

    logger.info(
        f"Processed archive {feed_info.title}: {len(entries)} entries"
        f" ({dropped_unattributed} dropped unattributed)"
    )
    return entries, None


def _link_host_matches_site(entry: FeedEntry) -> bool:
    """True when the entry's link host is the author's own site host.

    `feed_home_url` is the member's OPML site URL for both member and
    attributed archive entries, so this identifies the canonical copy of
    a post when several historical URL forms exist (for example an old
    pages.dev host versus the member's current domain).
    """
    link_host = urlparse(entry.link).hostname
    home_host = urlparse(entry.feed_home_url).hostname
    if link_host is None or home_host is None:
        return False
    return link_host.removeprefix("www.") == home_host.removeprefix("www.")


def deduplicate_feed_entries(entries: list[FeedEntry]) -> list[FeedEntry]:
    """Deduplicate entries by link, then by post identity across URL forms.

    First pass: entries with an identical link collapse to one, preferring
    an entry whose feed home matches the link's hostname (www-insensitive,
    via `_link_host_matches_site`); equal priority keeps the first.

    Second pass: entries sharing (feed_title, title, published) are the
    same post published under a different historical link form (scheme
    change, www prefix, path or slug migration) or syndicated across two
    feeds of the same author. The surviving copy is the one whose link
    host matches the author's site URL (see
    `_link_host_matches_site`); when neither or both match, member slots
    precede archive slots, so the first — the current member-feed form —
    is kept. `feed_title` is the author's identity (the same convention
    the member directory and the attribution cascade's title tier use),
    which also joins a person's multiple OPML feeds.

    Args:
        entries: Entries to deduplicate.

    Returns:
        One entry per distinct link and per distinct (feed, title,
        timestamp).
    """
    entries_by_link: dict[str, FeedEntry] = {}

    for entry in entries:
        existing_entry = entries_by_link.get(entry.link)
        if existing_entry is None:
            entries_by_link[entry.link] = entry
            continue

        if _link_host_matches_site(entry) and not _link_host_matches_site(
            existing_entry
        ):
            entries_by_link[entry.link] = entry

    best_by_post: dict[tuple[str, str, datetime], FeedEntry] = {}
    collapsed = 0
    for entry in entries_by_link.values():
        post_key = (entry.feed_title, entry.title, entry.published)
        existing = best_by_post.get(post_key)
        if existing is None:
            best_by_post[post_key] = entry
            continue
        collapsed += 1
        if _link_host_matches_site(entry) and not _link_host_matches_site(existing):
            best_by_post[post_key] = entry
            dropped, kept = existing, entry
        else:
            dropped, kept = entry, existing
        logger.debug(
            f"Collapsed {dropped.feed_title!r} post {dropped.title!r} ({dropped.published}): kept {kept.link}, dropped {dropped.link}"
        )

    if collapsed:
        logger.info(
            f"Collapsed {collapsed} entries repeating a post already in the corpus under a different URL form"
        )

    return list(best_by_post.values())


def fetch_all_feeds(
    feeds: list[FeedInfo], use_cache: bool, cache_fallback: bool
) -> tuple[list[FeedEntry], list[FailedFeedInfo]]:
    """
    Fetch and parse all feeds concurrently.

    Member feeds are fetched first-class; the site's own blogroll feed and
    any configured seed archives are fetched concurrently as archive
    sources whose entries fill gaps the rolling member windows have lost.
    Archive entries are slotted after member entries so the link dedupe
    keeps the current member entry on duplicates, and archive failures are
    logged without joining the member-feed failure list.

    Args:
        feeds: List of FeedInfo objects.
        use_cache: Whether to use cached content instead of fetching.
        cache_fallback: Whether to fall back to cached content on fetch failure.

    Returns:
        A tuple containing:
        - Combined, deduplicated list of all feed entries (by link, then
          by author/title/timestamp across URL forms).
        - List of member feeds that failed to be fetched.
    """
    if len(feeds) == 0:
        return [], []

    archive_feeds = [
        FeedInfo(
            title="Blogroll Archive (self)",
            xml_url=config.SITE_URL + BLOGROLL_FEED_FILE,
            html_url=config.SITE_URL,
        ),
        *(
            FeedInfo(
                title=f"Blogroll Archive ({urlparse(url).netloc})",
                xml_url=url,
                html_url=url,
            )
            for url in config.ARCHIVE_SEED_FEED_URLS
        ),
    ]
    member_count = len(feeds)

    logger.info(
        f"Processing {member_count} feeds and {len(archive_feeds)} archive feeds"
        f" with {config.MAX_WORKERS} workers"
    )

    # Member entries first, then archive entries: results land in
    # index-addressed slots so completion order never affects dedupe order.
    entries_by_feed: list[list[FeedEntry]] = [
        [] for _ in range(member_count + len(archive_feeds))
    ]
    failed_feeds: list[FailedFeedInfo] = []

    with ThreadPoolExecutor(
        max_workers=config.MAX_WORKERS, thread_name_prefix="Fetcher"
    ) as executor:
        # Submit all feed processing tasks
        future_to_feed = {
            executor.submit(
                process_single_feed, feed_info, use_cache, cache_fallback
            ): (index, feed_info)
            for index, feed_info in enumerate(feeds)
        }
        future_to_feed.update(
            {
                executor.submit(
                    process_archive_feed,
                    feed_info,
                    feeds,
                    use_cache,
                    cache_fallback,
                ): (index, feed_info)
                for index, feed_info in enumerate(archive_feeds, start=member_count)
            }
        )

        # Collect results as they complete
        for future in as_completed(future_to_feed):
            feed_index, feed_info = future_to_feed[future]

            try:
                entries, failure_reason = future.result()
                if failure_reason is not None:
                    if feed_index >= member_count:
                        logger.warning(
                            f"Archive feed {feed_info.title} failed"
                            f" ({failure_reason.name}); skipping"
                        )
                    else:
                        failed_feeds.append(
                            FailedFeedInfo(feed_info=feed_info, reason=failure_reason)
                        )
                else:
                    entries_by_feed[feed_index] = entries

            except Exception as e:
                if feed_index >= member_count:
                    logger.error(f"Failed to process archive {feed_info.title}: {e}")
                else:
                    logger.error(f"Failed to process {feed_info.title}: {e}")
                    failed_feeds.append(
                        FailedFeedInfo(feed_info=feed_info, reason=FailureReason.ERROR)
                    )

    session_manager.close_all()
    all_entries = [entry for feed_entries in entries_by_feed for entry in feed_entries]
    return deduplicate_feed_entries(all_entries), failed_feeds


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
        group_entries.sort(key=lambda x: (x.published, x.link), reverse=True)
        group_entries = [
            deepcopy(entry)
            for entry in group_entries[: config.MAX_SHOWN_POSTS_PER_FEED]
        ]

        for entry in group_entries:
            entry.tags = entry.tags[: config.MAX_SHOWN_TAGS]

        res_entries.extend(group_entries)

    # Sort result entries globally by publication date for overall stats
    res_entries.sort(key=lambda x: (x.published, x.link), reverse=True)
    return res_entries


WEEKNOTE_PATTERN = re.compile(
    r"(\b(?:day|week|month|quarter|year)(?:ly|'s|’s)?[-_ ]?notes?\b)|(notes\s+\(?\d+(-|-)\d+\)?\s*-?\s*\d+)",
    re.IGNORECASE,
)

WEEK_EXCLUSIONS = frozenset(
    [
        "week's",
        "week’s",
        "weekend",
        "weekday",
        "biweek",
        "midweek",
        "mid week",
        "mid-week",
        "semiweek",
        "semi-week",
        "yesterweek",
    ]
)


def separate_weeknote_entries(
    entries: list[FeedEntry],
) -> tuple[list[FeedEntry], list[FeedEntry]]:
    """Split entries into weeknote-like posts and everything else.

    A post is treated as a weeknote if any of:
      - a tag matches WEEKNOTE_PATTERN (week/month/quarter/year + "note")
      - the title matches WEEKNOTE_PATTERN
      - the summary matches WEEKNOTE_PATTERN
      - the title contains "week" and none of WEEK_EXCLUSIONS

    Args:
        entries: Entries to split.

    Returns:
        Tuple of (weeknote entries, all other entries).
    """
    weeknote_entries: list[FeedEntry] = []
    other_entries: list[FeedEntry] = []

    for entry in entries:
        title = entry.title.lower()
        if (
            any(WEEKNOTE_PATTERN.search(tag) for tag in entry.tags)
            or WEEKNOTE_PATTERN.search(title)
            or WEEKNOTE_PATTERN.search(entry.summary.lower())
            or ("week" in title and all(w not in title for w in WEEK_EXCLUSIONS))
        ):
            weeknote_entries.append(entry)
        else:
            other_entries.append(entry)

    return (weeknote_entries, other_entries)


# Subscriber-facing blogroll feed filename — also fetched as the site's own
# archive source in fetch_all_feeds(). Changing it breaks existing
# subscribers and the feedback loop.
BLOGROLL_FEED_FILE = "blogroll.atom"


def generate_blogroll_feed(
    entries: list[FeedEntry],
    feed_name: str,
    feed_subtitle: str,
    output_path: Path,
    add_content: bool = False,
):
    """
    Creates an Atom feed from a list of FeedEntry objects.

    Args:
        entries: A list of FeedEntry objects to include in the feed.
        feed_name: Name of generator feed.
        feed_subtitle: Subtitle of the generated feed.
        output_path: The path of the Atom file to be written.
        add_content: If True, include each entry's HTML content instead of a summary.
    """
    logger.info(f"Generating {feed_name} feed with {len(entries)} entries")
    feed_url = config.SITE_URL + output_path.name

    feed_info = FeedInfo(
        title=f"IndieWebClub Bangalore {feed_name}",
        xml_url=feed_url,
        html_url=config.SITE_URL,
    )

    generate_feed(
        feed_info=feed_info,
        author_name="IndieWebClub Bangalore",
        feed_subtitle=feed_subtitle,
        entries=entries,
        output_path=output_path,
        add_content=add_content,
    )

    logger.info(f"{feed_name} feed written to: {output_path}")
