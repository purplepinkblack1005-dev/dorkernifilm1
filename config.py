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


# /adddorks URL fetch timeout customization
#
# The existing command passes all arguments after /adddorks as one string.
# This wrapper lets users append a timeout to the URL without changing the
# command handler:
#   /adddorks https://example.test/dorks.txt 100
#   /adddorks https://example.test/dorks.txt 130
#   /adddorks https://example.test/dorks.txt none
#
# A numeric value is the total fetch timeout in seconds. "none" disables the
# practical cap by using a very large deadline. A URL without a suffix keeps
# the existing 60-second default.

def _install_adddorks_timeout_support():
    from searcher import SearchManager

    original = SearchManager.fetch_dorks_from_url_with_hooks

    def fetch_with_custom_timeout(
        self,
        url,
        on_attempt=None,
        on_response=None,
        on_lines=None,
        on_partial=None,
        timeout=None,
        total_timeout=None,
        retries=1,
    ):
        requested_timeout = None
        parts = str(url).strip().rsplit(None, 1)
        if len(parts) == 2:
            suffix = parts[1].strip().lower()
            if suffix == "none":
                url = parts[0]
                requested_timeout = 10**9
            else:
                try:
                    value = int(suffix)
                except ValueError:
                    value = None
                if value is not None and value > 0:
                    url = parts[0]
                    requested_timeout = value

        if requested_timeout is not None:
            total_timeout = requested_timeout

        return original(
            self,
            url,
            on_attempt=on_attempt,
            on_response=on_response,
            on_lines=on_lines,
            on_partial=on_partial,
            timeout=timeout if timeout is not None else 1,
            total_timeout=total_timeout if total_timeout is not None else 60,
            retries=retries,
        )

    SearchManager.fetch_dorks_from_url_with_hooks = fetch_with_custom_timeout


_install_adddorks_timeout_support()
