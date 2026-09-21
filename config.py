import os
from zoneinfo import ZoneInfo

TIMEZONE = ZoneInfo(os.getenv("TZ", "Asia/Manila"))

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
# Search settings
MAX_RESULTS_PER_DORK = int(os.getenv("MAX_RESULTS_PER_DORK", "100"))
WORKERS = int(os.getenv("WORKERS", "5"))
PROGRESS_UPDATE_INTERVAL = int(os.getenv("PROGRESS_UPDATE_INTERVAL", "25"))
OWNER_ID = int(os.getenv("OWNER_ID", "5703245194"))
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "100"))

# Proxy (optional)
# Set PROXY_ENABLED=true and PROXY=http://user:pass@host:port to use a single proxy
# OR upload proxies.txt to use multiple rotating proxies
PROXY_ENABLED = os.getenv("PROXY_ENABLED", "false").lower() == "true"
PROXY = os.getenv("PROXY", "")

# File paths (relative to the app directory)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DORKS_FILE = os.path.join(BASE_DIR, "dorks.txt")
SITES_FILE = os.path.join(BASE_DIR, "sites.txt")
PROXIES_FILE = os.path.join(BASE_DIR, "proxies.txt")

# Optional remote dork source (fetched on /start and via /adddork <url>)
DORKS_URL = os.getenv("DORKS_URL", "")
