import os
from zoneinfo import ZoneInfo

TIMEZONE = ZoneInfo(os.getenv("TZ", "Asia/Manila"))

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
MAX_RESULTS_PER_DORK = int(os.getenv("MAX_RESULTS_PER_DORK", "20"))
WORKERS = int(os.getenv("WORKERS", "2"))
PROGRESS_UPDATE_INTERVAL = int(os.getenv("PROGRESS_UPDATE_INTERVAL", "25"))
OWNER_ID = int(os.getenv("OWNER_ID", "5703245194"))
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "30"))

PROXY_ENABLED = os.getenv("PROXY_ENABLED", "false").lower() == "true"
PROXY = os.getenv("PROXY", "")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DORKS_FILE = os.path.join(BASE_DIR, "dorks.txt")
SITES_FILE = os.path.join(BASE_DIR, "sites.txt")
PROXIES_FILE = os.path.join(BASE_DIR, "proxies.txt")

DORKS_URL = os.getenv("DORKS_URL", "")

# Memory safety caps
MAX_SITES_IN_MEMORY = int(os.getenv("MAX_SITES_IN_MEMORY", "200000"))
MAX_PROXY_ATTEMPTS_PER_DORK = int(os.getenv("MAX_PROXY_ATTEMPTS_PER_DORK", "10"))
