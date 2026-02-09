"""
Member directory page generation for IWCB website.
"""

from __future__ import annotations

import hashlib
import json
import logging
import random
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from pathlib import Path

import pystache

from src import config
from src.feeds import FeedInfo
from src.utils import (
    SessionManager,
    add_utm_params,
    read_template,
    render_and_save_html,
)

logging.basicConfig(level=logging.INFO, format=config.LOG_FORMAT)
logger = logging.getLogger(__name__)

session_manager = SessionManager()


MATAROA_FAVICON = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAACAAAAAgCAYAAABzenr0AAAACXBIWXMAAAsTAAALEwEAmpwYAAAAAXNSR0IArs4c6QAAAARnQU1BAACxjwv8YQUAAAEwSURBVHgB7ZVBTsMwEEX/2GolQEjkBrkBPQI5ATlKd12SLrtr78EinACOkBuQG7QSEiClzuBpWYFUTxxY1W8ZfWe+7fEfIJFInDuEgbwtyjsQlUTm3q/Oj1+58b9qOueW2apuMQC1ge28vJlcmQcwzU8KmdfdR7/M1vUOf2XgUPzSPHv5DCq46d77QmPCQMFh5+riAs0mF7JGoQwJtosyn1j7igjYUXG9enw5pQmegDW2QizkypAkaMAYvkUkZPxLGWvAd/2Au/9FHhKomvA/0RhoEQtLQI00wOifEAvReANwtkYkEs0hTdCAvGMGNhgI99ho5oKqCfdTVx0Hjrp8s/+UNWFUBrKq3nXTvtCchOxcOweEweNYolnS0ZAE1HdGMFpm36xs61D0JhKJxE++AMI7Z3YRUW4wAAAAAElFTkSuQmCC"
BEARBLOG_FAVICON = "data:image/svg+xml,%3Csvg%20xmlns='http://www.w3.org/2000/svg'%20viewBox='0%200%20100%20100'%3E%3Ctext%20y='.9em'%20font-size='90'%3E%F0%9F%90%BC%3C/text%3E%3C/svg%3E"


@dataclass
class IndieWebFeatures:
    h_card: bool = False
    webmention: bool = False
    indieauth: bool = False
    rel_me: bool = False
    opengraph: bool = False


def _has_h_card(soup: object) -> bool:
    from bs4 import BeautifulSoup

    assert isinstance(soup, BeautifulSoup)
    result = soup.find(class_="h-card")
    return result is not None


def _has_webmention(soup: object, headers: dict[str, str]) -> bool:
    from bs4 import BeautifulSoup

    assert isinstance(soup, BeautifulSoup)
    result = soup.find("link", rel="webmention")
    if result is not None:
        return True
    link_header = headers.get("Link", "")
    return 'rel="webmention"' in link_header


def _has_indieauth(soup: object, headers: dict[str, str]) -> bool:
    from bs4 import BeautifulSoup

    assert isinstance(soup, BeautifulSoup)
    result = soup.find("link", rel="authorization_endpoint")
    if result is not None:
        return True
    link_header = headers.get("Link", "")
    return 'rel="authorization_endpoint"' in link_header


def _has_rel_me(soup: object) -> bool:
    from bs4 import BeautifulSoup

    assert isinstance(soup, BeautifulSoup)
    result = soup.find("link", rel="me")
    return result is not None


OG_PROP_RE = re.compile("og:\\w+")


def _has_opengraph(soup: object) -> bool:
    from bs4 import BeautifulSoup

    assert isinstance(soup, BeautifulSoup)
    result = soup.find("meta", property=OG_PROP_RE)
    return result is not None


def check_indieweb_features(soup: object, headers: dict[str, str]) -> IndieWebFeatures:
    return IndieWebFeatures(
        h_card=_has_h_card(soup),
        webmention=_has_webmention(soup, headers),
        indieauth=_has_indieauth(soup, headers),
        rel_me=_has_rel_me(soup),
        opengraph=_has_opengraph(soup),
    )


def fetch_site_html(url: str) -> tuple[str, str, dict[str, str]] | None:
    """Fetch a website's HTML. Returns (final URL, HTML, headers) or None on failure."""
    try:
        response = session_manager.get().get(url, timeout=config.REQUEST_TIMEOUT)
        response.raise_for_status()
        return response.url, response.text, dict(response.headers)
    except Exception as e:
        logger.debug(f"Failed to fetch HTML for {url}: {e}")
        return None


def get_ddg_favicon_url(site_url: str) -> str | None:
    """Get the DuckDuckGo favicon proxy URL for a website, or None if not found."""
    from urllib.parse import urlparse

    domain = urlparse(site_url).netloc
    url = f"https://icons.duckduckgo.com/ip3/{domain}.ico"

    try:
        response = session_manager.get().head(url, timeout=5)
        if response.status_code == 200:
            return url
    except Exception as e:
        logger.debug(f"Failed to check DDG favicon for {site_url}: {e}")

    return None


def check_hotlink_allowed(url: str) -> bool:
    """Check if a URL allows hotlinking by making a HEAD request."""
    try:
        response = session_manager.get().head(url, timeout=5, allow_redirects=True)
        return response.status_code == 200
    except Exception:
        return False


def get_favicon_from_html(url: str, html: str) -> str | None:
    """Get favicon URL from already-fetched HTML, checking hotlink is allowed."""
    try:
        import favicon

        icons = list(
            favicon.tags(url, html)  # pyright: ignore[reportAttributeAccessIssue]
        )
        if not icons:
            return None

        def icon_score(icon: favicon.Icon) -> int:
            size = icon.width or icon.height or 0
            if 16 <= size <= 64:
                return 1000 - abs(size - 32)
            elif size > 64:
                return 500 - size
            else:
                return size

        icons.sort(key=icon_score, reverse=True)

        for icon in icons:
            if check_hotlink_allowed(icon.url):
                return icon.url

        return None
    except Exception as e:
        logger.debug(f"Failed to get favicon from HTML for {url}: {e}")
        return None


def get_favicon_default(url: str) -> str | None:
    """Check for /favicon.ico at the site root."""
    try:
        import favicon

        icon = favicon.default(  # pyright: ignore[reportAttributeAccessIssue]
            url, timeout=5
        )
        if icon and check_hotlink_allowed(icon.url):
            return icon.url
        return None
    except Exception as e:
        logger.debug(f"Failed to get default favicon for {url}: {e}")
        return None


def get_name_key(feed: FeedInfo) -> str:
    return feed.title.lower()


def generate_members_page(
    entry_feeds: list[FeedInfo], opml_feeds: list[FeedInfo], output_dir: Path
):
    """
    Generate the members directory page.

    Args:
        entry_feeds: List of FeedInfo objects that have entries.
        opml_feeds: Original ordered list of FeedInfo from the OPML file,
            used to determine the preferred URL for members with multiple feeds.
        output_dir: Path where HTML file should be written.
    """
    logger.info(f"Generating members page with {len(entry_feeds)} feeds")

    # Build a priority map from the OPML order: for each member name,
    # the first feed listed in the OPML is the preferred one.
    opml_priority: dict[str, str] = {}
    for feed in opml_feeds:
        name_key = get_name_key(feed)
        if name_key not in opml_priority:
            opml_priority[name_key] = feed.html_url

    # Deduplicate feeds by member name, preferring the OPML-first URL.
    feeds_by_name: dict[str, FeedInfo] = {}
    for feed in entry_feeds:
        name_key = get_name_key(feed)
        if name_key not in feeds_by_name:
            feeds_by_name[name_key] = feed
        elif feed.html_url == opml_priority.get(name_key):
            feeds_by_name[name_key] = feed

    unique_feeds = list(feeds_by_name.values())

    favicon_cache_file = config.CACHE_DIR / "favicons.json"
    favicon_cache: dict[str, str] = {}
    if favicon_cache_file.exists():
        logger.debug("Using cache for favicons")
        with favicon_cache_file.open() as file:
            favicon_cache = json.load(file)

    indieweb_cache_file = config.CACHE_DIR / "indieweb.json"
    indieweb_cache: dict[str, dict[str, bool]] = {}
    if indieweb_cache_file.exists():
        logger.debug("Using cache for indieweb features")
        with indieweb_cache_file.open() as file:
            indieweb_cache = json.load(file)

    def build_member(feed: FeedInfo) -> tuple[FeedInfo, str, IndieWebFeatures]:
        name_key = get_name_key(feed)
        has_favicon = name_key in favicon_cache
        has_indieweb = name_key in indieweb_cache

        # Fetch the site HTML once for both favicon and IndieWeb checks.
        site_result = fetch_site_html(feed.html_url)

        # IndieWeb features
        if has_indieweb:
            features = IndieWebFeatures(**indieweb_cache[name_key])
        elif site_result:
            from bs4 import BeautifulSoup

            _, html, headers = site_result
            soup = BeautifulSoup(html, "html.parser")
            features = check_indieweb_features(soup, headers)
        else:
            features = IndieWebFeatures()

        # Favicon
        if has_favicon:
            icon_url = favicon_cache[name_key]
        elif "mataroa.blog" in feed.html_url:
            icon_url = MATAROA_FAVICON
        elif "bearblog.dev" in feed.html_url:
            icon_url = BEARBLOG_FAVICON
        else:
            icon_url = get_ddg_favicon_url(feed.html_url)
            if not icon_url and site_result:
                response_url, html, _ = site_result
                icon_url = get_favicon_from_html(response_url, html)
            if not icon_url and site_result:
                response_url, _, _ = site_result
                icon_url = get_favicon_default(response_url)
            if not icon_url:
                email_hash = hashlib.md5(feed.html_url.lower().encode()).hexdigest()
                icon_url = f"https://seccdn.libravatar.org/avatar/{email_hash}?s=80&d=identicon"
            logger.info(f"Fetched favicon for website: {feed.html_url}")

        return feed, icon_url, features

    with ThreadPoolExecutor(max_workers=config.MAX_WORKERS) as executor:
        members = list(executor.map(build_member, unique_feeds))

    session_manager.close_all()
    logger.debug(f"Got favicons for {len(members)} websites")

    try:
        if random.random() < 0.01:
            if favicon_cache_file.exists():
                favicon_cache_file.unlink()
                logger.debug("Deleted cached favicons")
            if indieweb_cache_file.exists():
                indieweb_cache_file.unlink()
                logger.debug("Deleted cached indieweb features")
        else:
            favicon_cache = {}
            indieweb_cache = {}
            for feed, icon_url, features in members:
                key = get_name_key(feed)
                favicon_cache[key] = icon_url
                indieweb_cache[key] = asdict(features)
            with favicon_cache_file.open("w") as file:
                json.dump(favicon_cache, file)
                logger.debug("Cached favicons")
            with indieweb_cache_file.open("w") as file:
                json.dump(indieweb_cache, file)
                logger.debug("Cached indieweb features")
    except Exception as e:
        logger.warn(f"Failed to cache data: {e}")

    members.sort(key=lambda m: get_name_key(m[0]))
    members_template = read_template("members.html")

    ctx = [
        {
            "feed": feed,
            "icon_url": icon_url,
            "html_url_utm": add_utm_params(feed.html_url, "website", "members"),
            "has_h_card": features.h_card,
            "has_webmention": features.webmention,
            "has_indieauth": features.indieauth,
            "has_rel_me": features.rel_me,
            "has_opengraph": features.opengraph,
        }
        for (feed, icon_url, features) in members
    ]
    try:
        renderer = pystache.Renderer()
        render_and_save_html(
            html_content=renderer.render(members_template, {"members": ctx}),
            output_dir=output_dir / "members",
        )
    except Exception as e:
        logger.error(f"Failed to generate members page: {e}")
        raise
