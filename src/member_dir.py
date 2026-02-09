"""
Member directory page generation for IWCB website.
"""

from __future__ import annotations

import hashlib
import json
import logging
import random
from concurrent.futures import ThreadPoolExecutor
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


def get_favicon_from_site(site_url: str) -> str | None:
    """Get favicon URL from site using favicon library, checking hotlink is allowed."""
    try:
        import favicon

        icons = favicon.get(site_url, timeout=5)
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
        logger.debug(f"Failed to get favicon from site {site_url}: {e}")
        return None


def get_name_key(feed: FeedInfo) -> str:
    return feed.title.lower()


def generate_members_page(feeds: list[FeedInfo], output_dir: Path):
    """
    Generate the members directory page.

    Args:
        feeds: List of FeedInfo objects from the OPML file.
        output_dir: Path where HTML file should be written.
    """
    logger.info(f"Generating members page with {len(feeds)} feeds")

    seen_names: set[str] = set()
    unique_feeds: list[FeedInfo] = []

    for feed in feeds:
        name_key = get_name_key(feed)
        if name_key in seen_names:
            continue
        seen_names.add(name_key)
        unique_feeds.append(feed)

    cache_file = config.CACHE_DIR / "favicons.json"
    cache: dict[str, str] = {}
    if cache_file.exists():
        logger.debug("Using cache for favicons")
        with cache_file.open() as file:
            cache = json.load(file)

    def build_member(feed: FeedInfo) -> tuple[FeedInfo, str]:
        name_key = get_name_key(feed)
        if name_key in cache:
            icon_url = cache[name_key]
        elif "mataroa.blog" in feed.html_url:
            icon_url = MATAROA_FAVICON
        elif "bearblog.dev" in feed.html_url:
            icon_url = BEARBLOG_FAVICON
        else:
            email_hash = hashlib.md5(feed.html_url.lower().encode()).hexdigest()
            icon_url = (
                get_ddg_favicon_url(feed.html_url)
                or get_favicon_from_site(feed.html_url)
                or f"https://seccdn.libravatar.org/avatar/{email_hash}?s=80&d=identicon"
            )
            logger.info(f"Fetched favicon for website: {feed.html_url}")

        return feed, icon_url

    with ThreadPoolExecutor(max_workers=config.MAX_WORKERS) as executor:
        members = list(executor.map(build_member, unique_feeds))

    session_manager.close_all()
    logger.debug(f"Got favicons for {len(members)} websites")

    try:
        if random.random() < 0.01:
            if cache_file.exists():
                cache_file.unlink()
                logger.debug("Deleted cached favicons")
        else:
            cache = {}
            for feed, icon_url in members:
                cache[get_name_key(feed)] = icon_url
            with cache_file.open("w") as file:
                json.dump(cache, file)
                logger.debug("Cached favicons")
    except Exception as e:
        logger.warn(f"Failed to cache favicons: {e}")

    members.sort(key=lambda m: get_name_key(m[0]))
    members_template = read_template("members.html")

    ctx = [
        {
            "feed": feed,
            "icon_url": icon_url,
            "html_url_utm": add_utm_params(feed.html_url, "website", "members"),
        }
        for (feed, icon_url) in members
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
