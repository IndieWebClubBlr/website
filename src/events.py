from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TypedDict, cast, final
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup
from dateutil import parser as date_parser
from feedgen.feed import FeedGenerator
from icalendar import Calendar, Timezone, TimezoneStandard
from icalendar import Event as CalEvent

from src import config

# Configure logging
logging.basicConfig(level=logging.INFO, format=config.LOG_FORMAT)
logger = logging.getLogger(__name__)


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
        return self.start_at.strftime("%d %b %Y, %I:%M %p IST")

    def start_at_machine(self) -> str:
        return self.start_at.isoformat()

    def end_at_human(self) -> str:
        return self.end_at.strftime("%d %b %Y, %I:%M %p IST")

    def end_at_machine(self) -> str:
        return self.end_at.isoformat()

    def created_at_human(self) -> str:
        return self.created_at.strftime("%d %b %Y")

    def created_at_machine(self) -> str:
        return self.created_at.isoformat()


DiscourseTopic = TypedDict(
    "DiscourseTopic", {"id": int, "created_at": str, "title": str, "slug": str}
)
DiscoureSearchResults = TypedDict(
    "DiscoureSearchResults", {"topics": list[DiscourseTopic]}
)
DiscoursePostEvent = TypedDict("DiscoursePostEvent", {"starts_at": str, "ends_at": str})
DiscoursePost = TypedDict("DiscoursePost", {"cooked": str, "event": DiscoursePostEvent})
DiscoursePostStream = TypedDict("DiscoursePostStream", {"posts": list[DiscoursePost]})
DiscourseTopicPosts = TypedDict(
    "DiscourseTopicPosts", {"post_stream": DiscoursePostStream}
)


def make_event(base_url: str, topic: DiscourseTopic, post: DiscoursePost) -> Event:
    event = post["event"]

    # url = event["url"]
    # parsed_url = urlparse(url)
    # if not parsed_url.scheme or not parsed_url.netloc:
    #     district_url = None
    # else:
    #     district_url = url

    return Event(
        id=topic["id"],
        title=topic["title"].replace(" with Ankur and Tanvi", ""),
        slug=topic["slug"],
        created_at=date_parser.parse(topic["created_at"]),
        start_at=date_parser.parse(event["starts_at"]),
        end_at=date_parser.parse(event["ends_at"]),
        details=post["cooked"],
        underline_url=f"{base_url}/t/{topic['slug']}",
        district_url="https://district.in/indiewebclub-meetup/event",
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
      topic: topic JSON returned from Discourse Search API.
      use_cache: Whether to use cached content instead of fetching.
      cache_fallback: Whether to fall back to cached content on fetch failure
          and update the cache on success.

    Returns:
      IWCB Event, None if fetch failed.
    """
    url = f"{base_url}/t/{topic['id']}.json"
    cache_key = hashlib.sha256(url.encode()).hexdigest()
    cache_file = config.CACHE_DIR / cache_key

    if use_cache and cache_file.exists():
        logger.debug(f"Using cached content for: {url}")
        post = cast(DiscoursePost, json.loads(cache_file.read_text(encoding="utf-8")))
        return make_event(base_url, topic, post)

    try:
        logger.info(f"Fetching event details: {url}")
        response = session.get(url, timeout=config.REQUEST_TIMEOUT, stream=True)
        response.raise_for_status()

        topic_posts = cast(DiscourseTopicPosts, response.json())
        post = topic_posts["post_stream"]["posts"][0]
        if use_cache or cache_fallback:
            _ = cache_file.write_text(json.dumps(post), encoding="utf-8")
            logger.debug(f"Cached content for: {url}")

        return make_event(base_url, topic, post)
    except requests.exceptions.Timeout:
        logger.warning(f"Timeout fetching event details: {url}")
    except requests.exceptions.HTTPError as e:
        logger.warning(f"HTTP error fetching event details {url}: {e}")
    except requests.exceptions.RequestException as e:
        logger.warning(f"Request error fetching event details {url}: {e}")
    except Exception as e:
        logger.error(f"Unexpected error fetching event details {url}: {e}")

    if cache_fallback and cache_file.exists():
        logger.debug(f"Using cached content as fallback for: {url}")
        post = cast(DiscoursePost, json.loads(cache_file.read_text(encoding="utf-8")))
        return make_event(base_url, topic, post)

    return None


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
    cache_key = hashlib.sha256(url.encode()).hexdigest()
    cache_file = config.CACHE_DIR / cache_key

    now = datetime.now(timezone.utc)

    with requests.Session() as session:
        session.headers.update({"User-Agent": config.UA, "Accept": "application/json"})

        response_json = None
        if use_cache and cache_file.exists():
            logger.debug(f"Using cached content for: {url}")
            response_json = cast(
                DiscoureSearchResults,
                json.loads(cache_file.read_text(encoding="utf-8")),
            )
        else:
            try:
                logger.info("Fetching events")

                response = session.get(url, timeout=config.REQUEST_TIMEOUT, stream=True)
                response.raise_for_status()

                response_json = cast(DiscoureSearchResults, response.json())

                if use_cache or cache_fallback:
                    cache_key = hashlib.sha256(url.encode()).hexdigest()
                    cache_file = config.CACHE_DIR / cache_key
                    _ = cache_file.write_text(
                        json.dumps(response_json), encoding="utf-8"
                    )
                    logger.debug(f"Cached content for: {url}")
            except requests.exceptions.Timeout:
                logger.warning(f"Timeout fetching events: {url}")
            except requests.exceptions.HTTPError as e:
                logger.warning(f"HTTP error fetching events {url}: {e}")
            except requests.exceptions.RequestException as e:
                logger.warning(f"Request error fetching events {url}: {e}")
            except Exception as e:
                logger.error(f"Unexpected error fetching events {url}: {e}")

    if response_json is None:
        if cache_fallback and cache_file.exists():
            logger.debug(f"Using cached content as fallback for: {url}")
            response_json = cast(
                DiscoureSearchResults,
                json.loads(cache_file.read_text(encoding="utf-8")),
            )
        else:
            return []

    events: list[Event] = []
    previous_count = 0
    for topic in response_json["topics"]:
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
    output_path = output_dir.joinpath(config.EVENTS_FEED_FILE)

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
        fe.content(event.details)

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

    output_path = output_dir.joinpath(config.EVENTS_CAL_FILE)
    with open(output_path, "wb") as f:
        _ = f.write(cal.to_ical())

    logger.info(f"Events calendar written to: {output_path}")
