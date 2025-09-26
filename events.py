from __future__ import annotations
from datetime import datetime
from dateutil import parser as date_parser
from typing import Dict, List, Optional
import config
import hashlib
import json
import logging
import requests

# Configure logging
logging.basicConfig(
    level=logging.INFO, format=config.LOG_FORMAT
)
logger = logging.getLogger(__name__)


class Event:
    """Represents an IndieWebClub BLR event"""

    def __init__(
        self,
        id: int,
        title: str,
        slug: str,
        created_at: datetime,
        start_at: datetime,
        end_at: datetime,
        details: Optional[str],
        underline_url: str,
        district_url: str,
    ):
        self.id = id
        self.title = title
        self.slug = slug
        self.created_at = created_at
        self.start_at = start_at
        self.end_at = end_at
        self.details = details
        self.underline_url = underline_url
        self.district_url = district_url

    def start_at_human(self) -> str:
        return self.start_at.astimezone(config.EVENTS_TZ).strftime(
            "%d %b %Y %I:%M %p IST"
        )

    def start_at_machine(self) -> str:
        return self.start_at.isoformat()


def make_event(base_url: str, topic: Dict, post: Dict, event: Dict) -> Event:
    return Event(
        id=topic["id"],
        title=topic["title"],
        slug=topic["slug"],
        created_at=date_parser.parse(topic["created_at"]),
        start_at=date_parser.parse(event["starts_at"]),
        end_at=date_parser.parse(event["ends_at"]),
        details=post["post_stream"]["posts"][0]["cooked"],
        underline_url=f'{base_url}/t/{topic["slug"]}',
        district_url=event["url"],
    )


def fetch_event_detail(base_url: str, topic: Dict, use_cache: bool) -> Optional[Event]:
    """Fetch details of IWCB event.

    Args:
      base_url: URL of Underline Center Discourse. Default: https://underline.center/.
      topic: topic JSON returned from Discourse Search API.
      use_cache: Whether to use cached content.

    Returns:
      IWCB Event, None if fetch failed.
    """
    url = f'{base_url}/t/{topic["id"]}.json'
    cache_key = hashlib.sha256(url.encode()).hexdigest()
    cache_file = config.CACHE_DIR / cache_key

    if use_cache and cache_file.exists():
        logger.debug(f"Using cached content for: {url}")
        post = json.loads(cache_file.read_text(encoding="utf-8"))
        event = post["post_stream"]["posts"][0]["event"]
        return make_event(base_url, topic, post, event)

    try:
        logger.info(f"Fetching event details: {url}")
        headers = {
            "User-Agent": config.UA,
            "Accept": "application/json",
        }
        response = requests.get(
            url, headers=headers, timeout=config.REQUEST_TIMEOUT, stream=True
        )
        response.raise_for_status()

        post = response.json()

        if use_cache:
            cache_file.write_text(json.dumps(post), encoding="utf-8")
            logger.debug(f"Cached content for: {url}")

        event = post["post_stream"]["posts"][0]["event"]
        return make_event(base_url, topic, post, event)
    except requests.exceptions.Timeout:
        logger.warning(f"Timeout fetching event details: {url}")
    except requests.exceptions.HTTPError as e:
        logger.warning(f"HTTP error fetching event details {url}: {e}")
    except requests.exceptions.RequestException as e:
        logger.warning(f"Request error fetching event details {url}: {e}")
    except Exception as e:
        logger.error(f"Unexpected error fetching event details {url}: {e}")

    return None


def fetch_events(
    base_url: str = "https://underline.center",
    use_cache: bool = False,
) -> List[Event]:
    """
    Fetch IWCB events from Underline Center Discourse API.

    Args:
      use_cache: Whether to use cached content.
      base_url: URL of Underline Center Discourse. Default: https://underline.center/.

    Returns:
      IWCB Event as a list, empty if fetch failed.
    """
    url = f"{base_url}/search?q=indieweb%20%23calendar%20order%3Alatest_topic&page=1"

    if use_cache:
        cache_key = hashlib.sha256(url.encode()).hexdigest()
        cache_file = config.CACHE_DIR / cache_key
        if cache_file.exists():
            logger.debug(f"Using cached content for: {url}")
            response_json = json.loads(cache_file.read_text(encoding="utf-8"))
            events = [
                event
                for topic in response_json["topics"]
                if (event := fetch_event_detail(base_url, topic, use_cache)) is not None
            ]
            logger.info(f"Extracted {len(events)} recent events from cache")
            return events

    try:
        logger.info("Fetching events")
        headers = {
            "User-Agent": config.UA,
            "Accept": "application/json",
        }
        response = requests.get(
            url, headers=headers, timeout=config.REQUEST_TIMEOUT, stream=True
        )
        response.raise_for_status()

        response_json = response.json()

        if use_cache:
            cache_key = hashlib.sha256(url.encode()).hexdigest()
            cache_file = config.CACHE_DIR / cache_key
            cache_file.write_text(json.dumps(response_json), encoding="utf-8")
            logger.debug(f"Cached content for: {url}")

        events = [
            event
            for topic in response_json["topics"]
            if (event := fetch_event_detail(base_url, topic, use_cache)) is not None
        ]

        events.sort(key=lambda x: x.start_at, reverse=True)
        logger.info(f"Extracted {len(events)} recent events")
        return events
    except requests.exceptions.Timeout:
        logger.warning(f"Timeout fetching events: {url}")
    except requests.exceptions.HTTPError as e:
        logger.warning(f"HTTP error fetching events {url}: {e}")
    except requests.exceptions.RequestException as e:
        logger.warning(f"Request error fetching events {url}: {e}")
    except Exception as e:
        logger.error(f"Unexpected error fetching events {url}: {e}")

    return []
