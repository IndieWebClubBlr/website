from pathlib import Path
from zoneinfo import ZoneInfo

# Configuration constants
REQUEST_TIMEOUT = 30  # seconds
MAX_CONTENT_LENGTH = 5 * 1024 * 1024  # 5MB
MAX_SHOWN_ENTRIES = 1
MAX_FEED_ENTRIES = 10
MAX_SHOWN_TAGS = 5
MAX_SHOWN_EVENTS = 10
MAX_WORKERS = 10  # concurrent feed fetches
RECENT_DAYS = 182  # half year
UA = "IndieWebClub BLR website generator"
SITE_URL = "https://blr.indiewebclub.org/"
WEBCAL_URL = SITE_URL.replace("https", "webcal")
EVENTS_TZ = ZoneInfo("Asia/Kolkata")
BLOGROLL_FEED_FILE = "blogroll.atom"
EVENTS_FEED_FILE = "events.atom"
EVENTS_CAL_FILE = "events.ics"
CACHE_DIR = Path(".cache")
ASSETS = ["style.css", "indiewebcamp-button.svg", "CNAME"]
LOG_FORMAT = "%(asctime)s [%(levelname)s] (%(threadName)s) %(message)s"
