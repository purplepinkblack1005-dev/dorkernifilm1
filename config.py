import os


TELEGRAM_BOT_TOKEN = "8778919018:AAGyuOj7rfK8C9ve3_-1gjifX4OemeXnexs"
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
DORKS_FILE = "dorks.txt"
SITES_FILE = "sites.txt"
PROXIES_FILE = "proxies.txt"
