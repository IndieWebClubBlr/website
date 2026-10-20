from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, TypeVar, final
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup
from feedgen.feed import FeedGenerator
from icalendar import Calendar, Timezone, TimezoneStandard
from icalendar import Event as CalEvent
from pydantic import BaseModel, ValidationError, field_validator

from src import config
from src.utils import cache_path

# Configure logging
logging.basicConfig(level=logging.INFO, format=config.LOG_FORMAT)
logger = logging.getLogger("events")


@final
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
        details: str | None,
        underline_url: str,
        district_url: str | None,
    ):
        """Parse a Discourse event topic into an Event.

        The details HTML is reduced to an agenda summary: everything before
        the "Agenda" heading and the "What is IndieWebClub?" blurb are
        stripped; summary is None if no "Agenda" heading is found.

        Args:
            id: Discourse topic id.
            title: Topic title as given by the caller (make_event strips
                event-series suffixes before calling this).
            slug: Topic slug used in the Underline URL.
            created_at: When the topic was created.
            start_at: Event start, converted to the site timezone.
            end_at: Event end, converted to the site timezone.
            details: Raw HTML of the first post.
            underline_url: URL of the topic on Underline Center.
            district_url: Venue URL if the event location is a URL, else None.
        """
        self.id = id
        self.title = title
        self.slug = slug
        self.created_at = created_at
        self.start_at = start_at.astimezone(config.EVENTS_TZ)
        self.end_at = end_at.astimezone(config.EVENTS_TZ)
        self.details = details
        self.underline_url = underline_url
        self.district_url = district_url
        soup = BeautifulSoup(details, "html.parser")
        agenda = soup.find(string="Agenda")
        if agenda is not None:
            agenda_header = agenda.parent
            for s in agenda_header.previous_siblings:
                s.decompose()
            agenda_header.name = "h3"
            blurb = soup.find(string="What is IndieWebClub?")
            if blurb is not None:
                blurb_header = blurb.parent
                for s in blurb_header.next_siblings:
                    s.decompose()
                blurb_header.decompose()
            self.summary = str(agenda_header.parent)
        else:
            self.summary = None

    def start_at_human(self) -> str:
        """Format the start time as e.g. "05 Oct 2026, 06:30 PM IST"."""
        return self.start_at.strftime("%d %b %Y, %I:%M %p IST")

    def start_at_machine(self) -> str:
        """Return the start time as an ISO 8601 string."""
        return self.start_at.isoformat()

    def end_at_human(self) -> str:
        """Format the end time as e.g. "05 Oct 2026, 08:30 PM IST"."""
        return self.end_at.strftime("%d %b %Y, %I:%M %p IST")

    def end_at_machine(self) -> str:
        """Return the end time as an ISO 8601 string."""
        return self.end_at.isoformat()

    def created_at_human(self) -> str:
        """Format the creation date as e.g. "01 Oct 2026"."""
        return self.created_at.strftime("%d %b %Y")

    def created_at_machine(self) -> str:
        """Return the creation time as an ISO 8601 string."""
        return self.created_at.isoformat()


class DiscourseTopic(BaseModel):
    """Topic summary from the Discourse Search API."""

    id: int
    created_at: datetime
    title: str
    slug: str


class DiscoursePostEvent(BaseModel):
    """Event metadata attached to a Discourse post."""

    starts_at: datetime
    ends_at: datetime
    location: str = ""


class DiscoursePost(BaseModel):
    """First post of a topic, carrying the event metadata and HTML."""

    cooked: str
    event: DiscoursePostEvent | None = None


class DiscoursePostStream(BaseModel):
    posts: list[DiscoursePost] = []


class DiscourseTopicPosts(BaseModel):
    post_stream: DiscoursePostStream


class DiscourseSearchResults(BaseModel):
    """Search results; malformed topics are dropped instead of fatal."""

    topics: list[DiscourseTopic] = []

    @field_validator("topics", mode="before")
    @classmethod
    def _drop_malformed_topics(cls, value: Any) -> list[Any]:
        if not isinstance(value, list):
            logger.warning(
                f"Expected a topics list, got {type(value).__name__}; treating as empty"
            )
            return []
        valid: list[Any] = []
        for item in value:
            try:
                DiscourseTopic.model_validate(item)
            except ValidationError as e:
                logger.warning(
                    f"Dropping malformed search topic: {e.error_count()} errors"
                )
            else:
                valid.append(item)
        return valid


T = TypeVar("T", bound=BaseModel)


def _load_cached(cache_file: Path, model_cls: type[T]) -> T | None:
    """Load a cached JSON payload as a model, or None if absent or invalid."""
    if not cache_file.exists():
        return None
    try:
        return model_cls.model_validate_json(cache_file.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        logger.warning(f"Ignoring invalid cache file {cache_file.name}: {e}")
        return None


def make_event(
    base_url: str, topic: DiscourseTopic, post: DiscoursePost
) -> Event | None:
    """Build an Event from a Discourse topic and its first post.

    Args:
        base_url: URL of the Underline Center Discourse instance.
        topic: Topic validated from the Discourse Search API.
        post: First post of the topic, carrying the event metadata.

    Returns:
        Event for the topic, or None if the post carries no event metadata.
    """
    event = post.event
    if event is None:
        return None

    district_url = None
    url = event.location
    parsed_url = urlparse(url)
    if parsed_url.scheme and parsed_url.netloc:
        district_url = url

    return Event(
        id=topic.id,
        title=topic.title.replace(" with Ankur and Tanvi", ""),
        slug=topic.slug,
        created_at=topic.created_at,
        start_at=event.starts_at,
        end_at=event.ends_at,
        details=post.cooked,
        underline_url=f"{base_url}/t/{topic.slug}",
        district_url=district_url,
    )


def fetch_event_detail(
    session: requests.Session,
    base_url: str,
    topic: DiscourseTopic,
    use_cache: bool,
    cache_fallback: bool,
) -> Event | None:
    """Fetch details of IWCB event.

    Args:
      session: requests.Session object.
      base_url: URL of Underline Center Discourse. Default: https://underline.center/.
      topic: Topic validated from the Discourse Search API.
      use_cache: Whether to use cached content instead of fetching.
      cache_fallback: Whether to fall back to cached content on fetch failure
          and update the cache on success.

    Returns:
      IWCB Event, None if fetch or validation failed or the topic carries no
      event metadata.
    """
    url = f"{base_url}/t/{topic.id}.json"
    cache_file = cache_path(url)

    post: DiscoursePost | None = None
    if use_cache:
        post = _load_cached(cache_file, DiscoursePost)
        if post is not None:
            logger.debug(f"Using cached content for: {url}")

    if post is None:
        try:
            logger.info(f"Fetching event details: {url}")
            response = session.get(url, timeout=config.REQUEST_TIMEOUT, stream=True)
            response.raise_for_status()

            raw = response.json()
            topic_posts = DiscourseTopicPosts.model_validate(raw)
            posts = topic_posts.post_stream.posts
            if not posts:
                logger.warning(f"No posts in topic: {url}")
            else:
                if use_cache or cache_fallback:
                    _ = cache_file.write_text(
                        json.dumps(raw["post_stream"]["posts"][0]), encoding="utf-8"
                    )
                    logger.debug(f"Cached content for: {url}")
                post = posts[0]
        except ValidationError as e:
            logger.warning(f"Invalid event data for {url}: {e.error_count()} errors")
        except requests.exceptions.Timeout:
            logger.warning(f"Timeout fetching event details: {url}")
        except requests.exceptions.HTTPError as e:
            logger.warning(f"HTTP error fetching event details {url}: {e}")
        except requests.exceptions.RequestException as e:
            logger.warning(f"Request error fetching event details {url}: {e}")
        except Exception as e:
            logger.error(f"Unexpected error fetching event details {url}: {e}")

    if post is None and cache_fallback:
        post = _load_cached(cache_file, DiscoursePost)
        if post is not None:
            logger.debug(f"Using cached content as fallback for: {url}")

    if post is None:
        return None
    return make_event(base_url, topic, post)


def fetch_events(
    base_url: str = "https://underline.center",
    use_cache: bool = False,
    cache_fallback: bool = False,
) -> list[Event]:
    """
    Fetch IWCB events from Underline Center Discourse API.

    Args:
      base_url: URL of Underline Center Discourse. Default: https://underline.center/.
      use_cache: Whether to use cached content instead of fetching.
      cache_fallback: Whether to fall back to cached content on fetch failure
          and update the cache on success.

    Returns:
      IWCB Event as a list, empty if fetch failed.
    """
    url = f"{base_url}/search/query?term=indieweb%20%23calendar%20order%3Alatest_topic&page=1"
    cache_file = cache_path(url)

    now = datetime.now(timezone.utc)

    with requests.Session() as session:
        session.headers.update({"User-Agent": config.UA, "Accept": "application/json"})

        response_json: DiscourseSearchResults | None = None
        if use_cache:
            response_json = _load_cached(cache_file, DiscourseSearchResults)
            if response_json is not None:
                logger.debug(f"Using cached content for: {url}")

        if response_json is None:
            try:
                logger.info("Fetching events")

                response = session.get(url, timeout=config.REQUEST_TIMEOUT, stream=True)
                response.raise_for_status()

                raw = response.json()
                response_json = DiscourseSearchResults.model_validate(raw)

                if use_cache or cache_fallback:
                    _ = cache_file.write_text(json.dumps(raw), encoding="utf-8")
                    logger.debug(f"Cached content for: {url}")
            except ValidationError as e:
                logger.warning(
                    f"Invalid events response {url}: {e.error_count()} errors"
                )
            except requests.exceptions.Timeout:
                logger.warning(f"Timeout fetching events: {url}")
            except requests.exceptions.HTTPError as e:
                logger.warning(f"HTTP error fetching events {url}: {e}")
            except requests.exceptions.RequestException as e:
                logger.warning(f"Request error fetching events {url}: {e}")
            except Exception as e:
                logger.error(f"Unexpected error fetching events {url}: {e}")

    if response_json is None:
        if cache_fallback:
            response_json = _load_cached(cache_file, DiscourseSearchResults)
            if response_json is not None:
                logger.debug(f"Using cached content as fallback for: {url}")
        if response_json is None:
            return []

    events: list[Event] = []
    previous_count = 0
    for topic in response_json.topics:
        event = fetch_event_detail(session, base_url, topic, use_cache, cache_fallback)
        if event is None:
            continue
        if event.start_at <= now:
            previous_count += 1
            if previous_count > config.MAX_SHOWN_EVENTS:
                continue
        events.append(event)

    events.sort(key=lambda x: x.start_at, reverse=True)
    logger.info(f"Extracted {len(events)} events")
    return events


def generate_events_feed(events: list[Event], output_dir: Path):
    """
    Creates an Atom feed from a list of Event objects.

    Args:
        events: A list of Event objects to include in the feed.
        output_dir: Path where Atom file should be written.
    """
    logger.info(f"Generating events feed with {len(events)} events")
    output_path = output_dir.joinpath("events.atom")

    feed_url = config.SITE_URL + output_path.name
    fg = FeedGenerator()

    fg.id(feed_url)
    fg.title("IndieWebClub Bangalore Events")
    fg.author(name="IndieWebClub Bangalore")
    fg.link(href=feed_url, rel="self")
    fg.link(href=config.SITE_URL, rel="alternate")
    fg.subtitle("Events by IndieWebClub Bangalore.")

    feed_updated = None
    for event in events:
        fe = fg.add_entry(order="append")

        fe.id(event.underline_url)
        fe.title(event.title)
        fe.link(href=event.underline_url, rel="alternate")
        fe.published(event.created_at)
        fe.updated(event.created_at)
        fe.content(event.details, type="html")

        if feed_updated is None or feed_updated < event.created_at:
            feed_updated = event.created_at

    fg.updated(feed_updated or datetime.now())
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
    cal.add("prodid", "-//IndieWebClub Bangalore//Events//EN")
    cal.add("version", "2.0")
    cal.calendar_name = "IndieWebClub Bangalore Events"
    cal.description = "Events by IndieWebClub Bangalore"

    tz = Timezone()
    tz.add("tzid", "Asia/Kolkata")
    tz_standard = TimezoneStandard()
    tz_standard.add("dtstart", datetime(1970, 1, 1))
    tz_standard.add("tzoffsetfrom", timedelta(hours=5, minutes=30))
    tz_standard.add("tzoffsetto", timedelta(hours=5, minutes=30))
    tz_standard.add("tzname", "IST")
    tz.add_component(tz_standard)
    cal.add_component(tz)

    now = datetime.now(tz=ZoneInfo("Asia/Kolkata"))

    for event_data in events:
        event = CalEvent()
        event.add("summary", event_data.title)
        event.add("url", event_data.underline_url)
        event.add("dtstamp", now)
        event.start = event_data.start_at
        event.end = event_data.end_at
        event.uid = f"indiewebclubblr-event-{event_data.id}"

        cal.add_component(event)

    output_path = output_dir.joinpath("events.ics")
    with open(output_path, "wb") as f:
        _ = f.write(cal.to_ical())

    logger.info(f"Events calendar written to: {output_path}")
