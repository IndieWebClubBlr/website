from pathlib import Path
from zoneinfo import ZoneInfo

# Configuration constants

# Timeout for network requests in seconds.
REQUEST_TIMEOUT = 30

# Number of concurrent workers to fetch feeds.
MAX_WORKERS = 10

# User-Agent string for feed fetching.
UA = "blr.indiewebclub.org generator"

# Maximum content length for fetched feeds in bytes.
MAX_CONTENT_LENGTH = 5 * 1024 * 1024

# Maximum age in days of recent entries to fetch from each feed.
MAX_FEED_ENTRY_AGE = 90

# Maximum number of recent entries to fetch from each feed.
MAX_FEED_ENTRIES = 10

# Maximum number of recent entries to show per feed.
MAX_SHOWN_ENTRIES = 1

# Maximum number of tags to show for each entry.
MAX_SHOWN_TAGS = 5

# Maximum number of previous events to show.
MAX_SHOWN_EVENTS = 10

# Maximum number of week notes to show.
MAX_SHOWN_WEEK_NOTES = 10

# Base URL of the website.
SITE_URL = "https://blr.indiewebclub.org/"

# Webcal URL derived from the site URL.
WEBCAL_URL = SITE_URL.replace("https", "webcal")

# Timezone for events.
EVENTS_TZ = ZoneInfo("Asia/Kolkata")

# Filename for the generated blogroll Atom feed.
BLOGROLL_FEED_FILE = "blogroll.atom"

# Filename for the generated events Atom feed.
EVENTS_FEED_FILE = "events.atom"

# Filename for the generated events iCalendar file.
EVENTS_CAL_FILE = "events.ics"

# Directory for caching fetched data.
CACHE_DIR = Path(".cache")

# List of static assets to be copied to the output directory.
ASSETS = [
    "assets/style.css",
    "assets/indiewebcamp-button.svg",
    "assets/preview.png",
    "assets/favicon.svg",
    "CNAME",
]

# Log format for the application.
LOG_FORMAT = "%(asctime)s [%(levelname)s] (%(threadName)s) %(message)s"
