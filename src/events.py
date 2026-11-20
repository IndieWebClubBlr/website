from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal, TypeVar, final
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import markdown
import requests
import yaml
from bs4 import BeautifulSoup
from feedgen.feed import FeedGenerator
from icalendar import Calendar, Timezone, TimezoneStandard
from icalendar import Event as CalEvent
from markdown.extensions.toc import slugify
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from src import config, ssrf
from src.utils import (
    cache_path,
    make_renderer,
    read_template,
    render_and_save_html,
    write_file,
)

# Configure logging
logging.basicConfig(level=logging.INFO, format=config.LOG_FORMAT)
logger = logging.getLogger("events")

TicketStatus = Literal["available", "coming_soon", "not_required"]

UNDERLINE_LOCATION_NAME = "Underline Center"
UNDERLINE_MAP_URL = "https://maps.app.goo.gl/qa4yF16LJ7cGSE6EA"


@final
class Event:
    """Represents an IndieWebClub BLR event"""

    def __init__(
        self,
        id: str,
        title: str,
        slug: str,
        created_at: datetime,
        start_at: datetime,
        end_at: datetime,
        details: str | None,
        event_url: str | None,
        ticket_url: str | None,
        ticket_status: TicketStatus,
        rsvp_url: str | None,
        location_name: str,
        location_url: str | None,
    ):
        """Parse a Discourse event topic into an Event.

        The details HTML is reduced to an agenda summary: everything before
        the "Agenda" heading and the "What is IndieWebClub?" blurb are
        stripped; summary is None if no "Agenda" heading is found.

        Args:
            id: Discourse topic id, or a derived string id for YAML events.
            title: Topic title as given by the caller (make_event strips
                event-series suffixes before calling this).
            slug: Topic slug used in the Underline URL.
            created_at: When the topic was created.
            start_at: Event start, converted to the site timezone.
            end_at: Event end, converted to the site timezone.
            details: Raw HTML of the first post.
            event_url: The event's own page, or None if it has none.
            ticket_url: Buy-ticket URL when tickets are available.
            ticket_status: One of "available", "coming_soon", or
                "not_required".
            rsvp_url: RSVP URL for events that do not require tickets.
            location_name: Venue name.
            location_url: Map URL for the venue, or None.
        """
        self.id = id
        self.title = title
        self.slug = slug
        self.created_at = created_at
        self.start_at = start_at.astimezone(config.EVENTS_TZ)
        self.end_at = end_at.astimezone(config.EVENTS_TZ)
        self.details = details
        self.event_url = event_url
        self.ticket_url = ticket_url
        self.ticket_status = ticket_status
        self.rsvp_url = rsvp_url
        self.location_name = location_name
        self.location_url = location_url
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

    @property
    def permalink(self) -> str:
        """Canonical URL: the event page, or its anchor on the events page."""
        return self.event_url or f"{config.SITE_URL}{self.page_url[1:]}"

    @property
    def page_url(self) -> str:
        return f"/events/{slugify(self.title, "-")}/"

    @property
    def tickets_available(self) -> bool:
        return self.ticket_status == "available"

    @property
    def tickets_coming_soon(self) -> bool:
        return self.ticket_status == "coming_soon"

    @property
    def tickets_not_required(self) -> bool:
        return self.ticket_status == "not_required"

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


_MARKDOWN_EXTENSIONS = ["fenced_code", "admonition", "codehilite", "smarty"]


def _markdown_block(text: str) -> str:
    """Render Markdown to block-level HTML (same extensions as data pages)."""
    return markdown.markdown(text, extensions=_MARKDOWN_EXTENSIONS)


class YamlEvent(BaseModel):
    """One hand-added event from data/events.yaml."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    title: str = Field(min_length=1)
    start: datetime
    end: datetime
    location: str = Field(min_length=1)
    location_url: str | None = None
    url: str | None = None
    ticket_status: TicketStatus = "coming_soon"
    ticket_url: str | None = None
    rsvp_url: str | None = None
    description: str = ""

    @field_validator("start", "end", mode="after")
    @classmethod
    def _to_events_tz(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=config.EVENTS_TZ)
        return value.astimezone(config.EVENTS_TZ)

    @model_validator(mode="after")
    def _end_not_before_start(self) -> YamlEvent:
        if self.end < self.start:
            raise ValueError("end must not be before start")
        return self

    @model_validator(mode="after")
    def _ticket_fields_consistent(self) -> YamlEvent:
        if self.ticket_status != "available" and self.ticket_url:
            raise ValueError(
                "ticket_url is only allowed when ticket_status is available"
            )
        if self.ticket_status != "not_required" and self.rsvp_url:
            raise ValueError(
                "rsvp_url is only allowed when ticket_status is not_required"
            )
        return self


class EventsFile(BaseModel):
    """The top-level shape of data/events.yaml."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    events: list[YamlEvent] = Field(default_factory=list)


def _yaml_event_to_event(item: YamlEvent) -> Event:
    """Convert a validated YAML event into the Event used by renderers."""
    slug = slugify(item.title, "-")
    event_id = slug
    description_html = (
        _markdown_block(item.description) if item.description.strip() else ""
    )
    event = Event(
        id=event_id,
        title=item.title,
        slug=slug,
        created_at=item.start,
        start_at=item.start,
        end_at=item.end,
        details=description_html,
        event_url=item.url,
        ticket_url=item.ticket_url,
        ticket_status=item.ticket_status,
        rsvp_url=item.rsvp_url,
        location_name=item.location,
        location_url=item.location_url,
    )
    event.summary = description_html or None
    return event


def load_events_yaml(path: Path) -> list[Event]:
    """Load hand-added events from a YAML file.

    An absent or empty file yields an empty list. A malformed file raises
    pydantic's ValidationError so the build fails loudly.
    """
    if not path.exists():
        return []
    data: Any = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    events_file = EventsFile.model_validate(data)
    return [_yaml_event_to_event(item) for item in events_file.events]


def merge_events(api_events: list[Event], yaml_events: list[Event]) -> list[Event]:
    """Merge API and YAML events, deduping by event URL and capping past events.

    Events without an event URL are always kept. Ordering is newest first,
    matching fetch_events(). Upcoming events are uncapped; previous events are
    limited to config.MAX_SHOWN_EVENTS.
    """
    merged = list(api_events)
    seen_urls = {event.event_url for event in merged if event.event_url}
    for event in yaml_events:
        if event.event_url is not None:
            if event.event_url in seen_urls:
                continue
            seen_urls.add(event.event_url)
        merged.append(event)

    return sorted(merged, key=lambda event: event.start_at, reverse=True)


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

    ticket_url = None
    url = event.location
    parsed_url = urlparse(url)
    if parsed_url.scheme and parsed_url.netloc:
        ticket_url = url

    return Event(
        id=str(topic.id),
        title=topic.title.replace(" with Ankur and Tanvi", ""),
        slug=topic.slug,
        created_at=topic.created_at,
        start_at=event.starts_at,
        end_at=event.ends_at,
        details=post.cooked,
        event_url=f"{base_url}/t/{topic.slug}",
        ticket_url=ticket_url,
        ticket_status="available" if ticket_url else "coming_soon",
        rsvp_url=None,
        location_name=UNDERLINE_LOCATION_NAME,
        location_url=UNDERLINE_MAP_URL,
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
            response = ssrf.safe_get(
                session, url, timeout=config.REQUEST_TIMEOUT, stream=True
            )
            response.raise_for_status()

            raw = response.json()
            topic_posts = DiscourseTopicPosts.model_validate(raw)
            posts = topic_posts.post_stream.posts
            if not posts:
                logger.warning(f"No posts in topic: {url}")
            else:
                if use_cache or cache_fallback:
                    write_file(cache_file, json.dumps(raw["post_stream"]["posts"][0]))
                    logger.debug(f"Cached content for: {url}")
                post = posts[0]
        except ValidationError as e:
            logger.warning(f"Invalid event data for {url}: {e.error_count()} errors")
        except requests.exceptions.Timeout:
            logger.warning(f"Timeout fetching event details: {url}")
        except ssrf.SSRFError as e:
            logger.warning(f"Refusing to fetch event details {url}: {e}")
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
    yaml_path: Path = Path("./data/events.yaml"),
) -> list[Event]:
    """
    Fetch IWCB events from Underline Center and merge hand-added YAML events.

    Args:
      base_url: URL of Underline Center Discourse. Default: https://underline.center/.
      use_cache: Whether to use cached content instead of fetching.
      cache_fallback: Whether to fall back to cached content on fetch failure
          and update the cache on success.
      yaml_path: YAML file with hand-added events. Missing or empty files are
          ignored.

    Returns:
      IWCB events as a list, sorted newest first. YAML events are included even
      when the Discourse fetch fails.
    """
    url = f"{base_url}/search/query?term=indieweb%20%23calendar%20order%3Alatest_topic&page=1"
    cache_file = cache_path(url)

    now = datetime.now(timezone.utc)
    yaml_events = load_events_yaml(yaml_path)

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

                response = ssrf.safe_get(
                    session, url, timeout=config.REQUEST_TIMEOUT, stream=True
                )
                response.raise_for_status()

                raw = response.json()
                response_json = DiscourseSearchResults.model_validate(raw)

                if use_cache or cache_fallback:
                    write_file(cache_file, json.dumps(raw))
                    logger.debug(f"Cached content for: {url}")
            except ValidationError as e:
                logger.warning(
                    f"Invalid events response {url}: {e.error_count()} errors"
                )
            except requests.exceptions.Timeout:
                logger.warning(f"Timeout fetching events: {url}")
            except ssrf.SSRFError as e:
                logger.warning(f"Refusing to fetch events {url}: {e}")
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
            logger.info(f"Extracted {len(yaml_events)} YAML events")
            return merge_events([], yaml_events)

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

    events = merge_events(events, yaml_events)
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

    renderer = make_renderer()
    template = read_template(file_name="event-entry.html")

    feed_updated = None
    for event in events:
        fe = fg.add_entry(order="append")

        fe.id(event.permalink)
        fe.title(event.title)
        fe.link(href=event.permalink, rel="alternate")
        fe.published(event.created_at)
        fe.updated(event.created_at)
        fe.content(renderer.render(template, event), type="html")

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
        event.add("url", event_data.permalink)
        event.add("location", event_data.location_name)
        event.add("dtstamp", now)
        event.start = event_data.start_at
        event.end = event_data.end_at
        event.uid = f"indiewebclubblr-event-{event_data.id}"

        cal.add_component(event)

    output_path = output_dir.joinpath("events.ics")
    write_file(output_path, cal.to_ical())

    logger.info(f"Events calendar written to: {output_path}")


def split_events(events: list[Event]) -> tuple[list[Event], list[Event]]:
    """Split events into upcoming and previous lists.

    Args:
        events: Events sorted newest first.

    Returns:
        A tuple of (upcoming, previous): upcoming events in chronological
        order, previous events in reverse-chronological order.
    """
    now = datetime.now(timezone.utc)
    previous_events = [event for event in events if event.start_at <= now]
    upcoming_events = [event for event in events if event.start_at > now]
    upcoming_events.reverse()
    return upcoming_events, previous_events


def generate_events_page(events: list[Event], output_dir: Path):
    """Generate the /events/ page with upcoming and previous events.

    Args:
        events: A list of Event objects to include on the page.
        output_dir: Directory where the events/ page should be written.
    """
    logger.info(f"Generating the events page with {len(events)} events")

    upcoming_events, previous_events = split_events(events)
    template_data = {
        "webcal_url": config.WEBCAL_URL,
        "upcoming_events": upcoming_events,
        "has_upcoming_events": len(upcoming_events) > 0,
        "previous_events": previous_events[: config.MAX_SHOWN_EVENTS],
    }

    renderer = make_renderer()
    render_and_save_html(
        html_content=renderer.render(read_template("events.html"), template_data),
        page_url="events/",
        output_dir=output_dir / "events",
    )
