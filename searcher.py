import asyncio
import logging
import os
import random
import time
from urllib.parse import urlparse, urlunparse, parse_qs
from typing import List, Dict, Optional, Set
from ddgs import DDGS

import config

logger = logging.getLogger(__name__)


def normalize_url(url: str) -> str:
    """
    Normalize a URL to avoid trivial duplicates:
    - lower scheme & host
    - remove default port
    - remove www.
    - remove fragment
    - keep query string for comparison
    - remove trailing slash from path
    """
    try:
        parsed = urlparse(url.strip())
        scheme = parsed.scheme.lower()
        netloc = parsed.netloc.lower()

        # Remove default ports
        if (scheme == "http" and netloc.endswith(":80")) or (
            scheme == "https" and netloc.endswith(":443")
        ):
            netloc = netloc.rsplit(":", 1)[0]

        # Remove leading "www."
        if netloc.startswith("www."):
            netloc = netloc[4:]

        path = parsed.path or "/"
        if len(path) > 1 and path.endswith("/"):
            path = path[:-1]

        # Keep query string for comparison
        query = parsed.query

        # Drop fragment
        return urlunparse((scheme, netloc, path, "", query, ""))
    except Exception:
        return url.strip().lower()


def get_domain_key(url: str) -> str:
    """
    Extract a domain key for deduplication.
    Returns: scheme://netloc (domain only, without path or query string)
    This ensures all URLs from the same domain are grouped together.
    """
    try:
        parsed = urlparse(url)
        scheme = parsed.scheme.lower()
        netloc = parsed.netloc.lower()
        
        # Remove www. for consistent domain matching
        if netloc.startswith("www."):
            netloc = netloc[4:]
            
        return f"{scheme}://{netloc}"
    except Exception:
        return url


def get_param_count(url: str) -> int:
    """
    Count the number of parameters in the query string.
    More parameters = longer/more complex URL.
    """
    try:
        parsed = urlparse(url)
        if not parsed.query:
            return 0
        params = parse_qs(parsed.query)
        return len(params)
    except Exception:
        return 0


def parse_proxy_line(line: str) -> Optional[str]:
    """Parse proxy in format: host:port:username:password"""
    line = line.strip()
    if not line or line.startswith("#"):
        return None

    parts = line.split(":")
    if len(parts) == 4:
        host, port, username, password = parts
        return f"http://{username}:{password}@{host}:{port}"
    elif len(parts) == 2:
        host, port = parts
        return f"http://{host}:{port}"
    else:
        logger.warning(f"Invalid proxy format: {line}")
        return None


def deduplicate_by_domain(urls: Set[str]) -> Set[str]:
    """
    Deduplicate URLs by domain (scheme://netloc).
    For each unique domain, keep the URL with the most parameters (longest query string).
    If no query strings, keep the base URL.
    If same parameter count, keep the longer URL.
    """
    domain_map: Dict[str, str] = {}

    for url in urls:
        domain_key = get_domain_key(url)
        param_count = get_param_count(url)

        if domain_key not in domain_map:
            # First time seeing this domain
            domain_map[domain_key] = url
        else:
            # Compare parameter counts
            existing_url = domain_map[domain_key]
            existing_param_count = get_param_count(existing_url)

            # Keep the URL with more parameters (longer query string)
            if param_count > existing_param_count:
                domain_map[domain_key] = url
            elif param_count == existing_param_count:
                # If same param count, keep the longer URL (more characters)
                if len(url) > len(existing_url):
                    domain_map[domain_key] = url

    return set(domain_map.values())


class SearchManager:
    def __init__(self):
        self.dorks: List[str] = []
        self.total: int = 0
        self.processed: int = 0
        self.failed: int = 0
        self.current_dork: Optional[str] = None
        self.unique_sites: Set[str] = set()

        self.proxies: List[str] = []
        self.current_proxy_index: int = 0

        self.running: bool = False
        self.search_task: Optional[asyncio.Task] = None

        self.lock = asyncio.Lock()
        self.file_lock = asyncio.Lock()
        self.last_update_time = time.time()

        # Load proxies and existing sites on startup
        self.load_proxies_from_file()
        self.load_sites_from_file()

    # -------------------------------
    # Load existing sites from disk
    # -------------------------------
    def load_sites_from_file(self, filename: str = config.SITES_FILE):
        """Load previously saved sites from sites.txt on startup."""
        try:
            with open(filename, "r", encoding="utf-8") as f:
                lines = [line.strip() for line in f if line.strip()]
            self.unique_sites = set(lines)
            # Apply domain deduplication on loaded sites
            self.unique_sites = deduplicate_by_domain(self.unique_sites)
            logger.info(f"Loaded {len(self.unique_sites)} existing sites from {filename}")
        except FileNotFoundError:
            logger.info(f"No existing sites file found. Starting fresh.")
            self.unique_sites = set()

    # -------------------------------
    # Dork loading
    # -------------------------------
    def load_dorks_from_file(self, filename: str = config.DORKS_FILE) -> int:
        """Read dorks from a text file, clean, deduplicate."""
        try:
            with open(filename, "r", encoding="utf-8") as f:
                lines = [line.strip() for line in f if line.strip() and not line.startswith("#")]
            return self.set_dorks(lines)
        except FileNotFoundError:
            logger.error(f"Dorks file '{filename}' not found.")
            return 0

    def set_dorks(self, dorks_list: List[str]) -> int:
        """Set new dork list, remove duplicates, return count."""
        self.dorks = list(dict.fromkeys(dorks_list))
        self.total = len(self.dorks)
        self.processed = 0
        self.failed = 0
        return self.total

    # -------------------------------
    # Proxy loading
    # -------------------------------
    def load_proxies_from_file(self, filename: str = config.PROXIES_FILE) -> int:
        """Read proxies from file, parse them, and store formatted URLs."""
        try:
            with open(filename, "r", encoding="utf-8") as f:
                lines = [line.strip() for line in f if line.strip() and not line.startswith("#")]
        except FileNotFoundError:
            logger.info(f"Proxies file '{filename}' not found. Running without proxies.")
            self.proxies = []
            return 0

        parsed_proxies = []
        for line in lines:
            proxy_url = parse_proxy_line(line)
            if proxy_url:
                parsed_proxies.append(proxy_url)

        self.proxies = parsed_proxies
        logger.info(f"Loaded {len(self.proxies)} proxies from {filename}")
        return len(self.proxies)

    def set_proxies(self, proxy_lines: List[str]) -> int:
        """Set proxies from a list of raw lines."""
        parsed_proxies = []
        for line in proxy_lines:
            proxy_url = parse_proxy_line(line)
            if proxy_url:
                parsed_proxies.append(proxy_url)

        self.proxies = parsed_proxies
        self.current_proxy_index = 0
        logger.info(f"Set {len(self.proxies)} proxies")
        return len(self.proxies)

    def get_next_proxy(self) -> Optional[str]:
        """Return the next proxy in rotation."""
        if not self.proxies:
            return None
        proxy = self.proxies[self.current_proxy_index % len(self.proxies)]
        self.current_proxy_index += 1
        return proxy

    # -------------------------------
    # Search control
    # -------------------------------
    async def start_search(
        self,
        max_results: int = config.MAX_RESULTS_PER_DORK,
        workers: int = config.WORKERS,
        request_timeout: int = config.REQUEST_TIMEOUT,
    ) -> bool:
        """Start a new search if none is running."""
        if self.running:
            return False
        if not self.dorks:
            self.load_dorks_from_file()
        if not self.dorks:
            return False

        self.running = True
        self.processed = 0
        self.failed = 0
        self.last_update_time = time.time()
        self.current_proxy_index = 0

        self.search_task = asyncio.create_task(
            self._run_search(max_results, workers, request_timeout)
        )
        return True

    async def _run_search(self, max_results: int, workers: int, request_timeout: int):
        """Process dorks using a fixed number of concurrent workers."""
        queue = asyncio.Queue()

        for dork in self.dorks:
            await queue.put(dork)

        async def worker():
            while True:
                try:
                    dork = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return

                try:
                    await self._process_dork(dork, max_results, request_timeout)
                finally:
                    queue.task_done()

        worker_count = max(1, min(workers, len(self.dorks)))

        worker_tasks = [
            asyncio.create_task(worker())
            for _ in range(worker_count)
        ]

        await queue.join()

        for task in worker_tasks:
            await task

        self.running = False
        # Apply domain deduplication before final save
        async with self.lock:
            before_count = len(self.unique_sites)
            self.unique_sites = deduplicate_by_domain(self.unique_sites)
            after_count = len(self.unique_sites)
            if before_count != after_count:
                logger.info(f"Final deduplication removed {before_count - after_count} duplicate domains")
        await self.write_sites_file()
        logger.info(f"Search completed with {worker_count} workers.")

    async def _process_dork(self, dork: str, max_results: int, request_timeout: int):
        """Perform one DDGS search, handle errors, update state."""
        async with self.lock:
            self.current_dork = dork

        try:
            results = await asyncio.wait_for(
                asyncio.to_thread(self._ddgs_search, dork, max_results, request_timeout),
                timeout=request_timeout + 10,
            )

            new_sites = 0
            for r in results:
                url = r.get("href", "")
                if url:
                    normalized = normalize_url(url)
                    async with self.lock:
                        if normalized not in self.unique_sites:
                            self.unique_sites.add(normalized)
                            new_sites += 1

            # Apply domain deduplication after adding new sites
            async with self.lock:
                before_count = len(self.unique_sites)
                self.unique_sites = deduplicate_by_domain(self.unique_sites)
                after_count = len(self.unique_sites)
                if before_count != after_count:
                    logger.info(f"Deduplication removed {before_count - after_count} duplicate domains")

            # Save after every dork
            await self.write_sites_file()

        except Exception as e:
            logger.error(f"Error searching '{dork}': {e}")
            async with self.lock:
                self.failed += 1
        finally:
            async with self.lock:
                self.processed += 1
                self.current_dork = None
                self.last_update_time = time.time()

    def _ddgs_search(self, query: str, max_results: int, request_timeout: int) -> List[Dict]:
        """Synchronous DDGS search, runs in a thread."""
        proxy = self.get_next_proxy()

        if proxy:
            os.environ["HTTP_PROXY"] = proxy
            os.environ["HTTPS_PROXY"] = proxy
        else:
            os.environ.pop("HTTP_PROXY", None)
            os.environ.pop("HTTPS_PROXY", None)

        with DDGS(timeout=request_timeout) as ddgs:
            return list(ddgs.text(query, max_results=max_results))

    # -------------------------------
    # Export & status
    # -------------------------------
    async def export_sites(self) -> List[str]:
        """Return a sorted list of all unique normalized URLs."""
        async with self.lock:
            return sorted(self.unique_sites)

    async def write_sites_file(self, filename: str = config.SITES_FILE):
        """Write current unique sites to sites.txt (thread-safe)."""
        async with self.lock:
            sites = sorted(self.unique_sites)
        async with self.file_lock:
            try:
                with open(filename, "w", encoding="utf-8") as f:
                    f.write("\n".join(sites))
                logger.info(f"Saved {len(sites)} sites to {filename}")
            except Exception as e:
                logger.error(f"Failed to write {filename}: {e}")

    async def get_status(self) -> Dict:
        """Return current status as a dictionary."""
        async with self.lock:
            return {
                "running": self.running,
                "total": self.total,
                "processed": self.processed,
                "failed": self.failed,
                "current_dork": self.current_dork,
                "unique_count": len(self.unique_sites),
                "last_update": self.last_update_time,
                "workers": config.WORKERS,
                "proxy_enabled": config.PROXY_ENABLED or len(self.proxies) > 0,
                "proxy_count": len(self.proxies),
            }

    def is_running(self) -> bool:
        return self.running
