"""Newsletter archive fetching and page generation."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

import feedparser
import requests

from src import config
from src.feeds import parse_feed_date
from src.utils import (
    make_renderer,
    read_template,
    render_and_save_html,
)

logging.basicConfig(level=logging.INFO, format=config.LOG_FORMAT)
logger = logging.getLogger(__name__)


def load_newsletter_archive() -> list[dict[str, str]]:
    """Load newsletter archive URLs from RSS feed with caching."""
    cache_file = config.CACHE_DIR / "newsletter_archive.xml"
    now = datetime.now(timezone.utc)

    if cache_file.exists():
        cache_age = (
            now - datetime.fromtimestamp(cache_file.stat().st_mtime, timezone.utc)
        ).total_seconds()
        if cache_age < config.NEWSLETTER_CACHE_EXPIRY * 24 * 60 * 60:
            logger.debug("Using cached newsletter archive")
            content = cache_file.read_text(encoding="utf-8")
        else:
            content = None
    else:
        content = None

    if not content:
        try:
            logger.info(
                f"Fetching newsletter archive from {config.NEWSLETTER_ARCHIVE_URL}"
            )
            response = requests.get(
                config.NEWSLETTER_ARCHIVE_URL,
                timeout=config.REQUEST_TIMEOUT,
                headers={"User-Agent": config.UA},
            )
            response.raise_for_status()
            content = response.text
            cache_file.write_text(content, encoding="utf-8")
            logger.debug("Fetched and cached newsletter archive")
        except requests.RequestException as e:
            logger.warning(f"Failed to fetch newsletter archive: {e}")
            if cache_file.exists():
                logger.info("Using cached newsletter archive as fallback")
                content = cache_file.read_text(encoding="utf-8")
            else:
                return []

    if not content:
        return []

    parsed = feedparser.parse(content)
    items = []

    for entry in parsed.entries:
        if entry.get("title") == "IndieWebClub Bangalore Blogroll Digest":
            pub_date = None
            if hasattr(entry, "published") and entry.published:
                pub_date = parse_feed_date(entry.published)
            if pub_date:
                items.append({"url": entry.link, "date": pub_date})

    items.sort(key=lambda x: x["date"], reverse=True)

    return [
        {"url": item["url"], "date": item["date"].strftime("%d %b %Y")}
        for item in items
    ]


def generate_newsletter_subscribe_page(output_dir: Path):
    """Generate the newsletter subscription page."""
    renderer = make_renderer()
    archive = load_newsletter_archive()

    render_and_save_html(
        html_content=renderer.render(
            read_template("nl-subsribe.html"),
            {"archive": archive, "has_archive": len(archive) > 0},
        ),
        output_dir=output_dir / "newsletter",
    )
    logger.info("Generated newsletter subscription page")
