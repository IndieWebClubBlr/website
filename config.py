from pathlib import Path
from zoneinfo import ZoneInfo

# Configuration constants
REQUEST_TIMEOUT = 30  # seconds
MAX_CONTENT_LENGTH = 5 * 1024 * 1024  # 5MB
MAX_SHOWN_ENTRIES = 2
MAX_FEED_ENTRIES = 10
MAX_SHOWN_TAGS = 5
MAX_WORKERS = 10  # concurrent feed fetches
RECENT_DAYS = 365  # one year
UA = "IndieWebClub BLR website generator"
SITE_URL = "https://indiewebclubblr.github.io/website/"
WEBCAL_URL = SITE_URL.replace("https", "webcal")
EVENTS_TZ = ZoneInfo("Asia/Kolkata")
BLOGROLL_FEED_FILE = "blogroll.atom"
EVENTS_FEED_FILE = "events.atom"
EVENTS_CAL_FILE = "events.ics"
CACHE_DIR = Path(".cache")
ASSETS = ["style.css"]
LOG_FORMAT = "%(asctime)s - %(levelname)s - %(message)s"
