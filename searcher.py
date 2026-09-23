import asyncio
import logging
import os
import socket
import time
import urllib.error
import urllib.request
from datetime import datetime
from urllib.parse import urlparse, urlunparse, parse_qs
from typing import List, Dict, Optional, Set
from zoneinfo import ZoneInfo

from ddgs import DDGS

import config

logger = logging.getLogger(__name__)

TZ = ZoneInfo(os.getenv("TZ", "Asia/Manila"))


def now_str() -> str:
    return datetime.now(TZ).strftime("%I:%M:%S %p")


def format_duration(seconds: float) -> str:
    if seconds < 0:
        seconds = 0
    seconds = int(seconds)
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours > 0:
        return f"{hours}h {minutes}m {secs}s"
    elif minutes > 0:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def normalize_url(url: str) -> str:
    try:
        parsed = urlparse(url.strip())
        scheme = parsed.scheme.lower()
        netloc = parsed.netloc.lower()
        if (scheme == "http" and netloc.endswith(":80")) or (scheme == "https" and netloc.endswith(":443")):
            netloc = netloc.rsplit(":", 1)[0]
        if netloc.startswith("www."):
            netloc = netloc[4:]
        path = parsed.path or "/"
        if len(path) > 1 and path.endswith("/"):
            path = path[:-1]
        return urlunparse((scheme, netloc, path, "", parsed.query, ""))
    except Exception:
        return url.strip().lower()


def get_domain_key(url: str) -> str:
    try:
        parsed = urlparse(url)
        scheme = parsed.scheme.lower()
        netloc = parsed.netloc.lower()
        if netloc.startswith("www."):
            netloc = netloc[4:]
        return f"{scheme}://{netloc}"
    except Exception:
        return url


def get_param_count(url: str) -> int:
    try:
        parsed = urlparse(url)
        if not parsed.query:
            return 0
        return len(parse_qs(parsed.query))
    except Exception:
        return 0


def parse_proxy_line(line: str) -> Optional[str]:
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
    domain_map: Dict[str, str] = {}
    for url in urls:
        key = get_domain_key(url)
        cnt = get_param_count(url)
        if key not in domain_map:
            domain_map[key] = url
        else:
            existing = domain_map[key]
            ecnt = get_param_count(existing)
            if cnt > ecnt:
                domain_map[key] = url
            elif cnt == ecnt and len(url) > len(existing):
                domain_map[key] = url
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
        self.proxy_retries: int = 0

        self.running: bool = False
        self.search_task: Optional[asyncio.Task] = None
        self._stop_requested: bool = False

        self.lock = asyncio.Lock()
        self.file_lock = asyncio.Lock()
        self.last_update_time = time.time()

        self.start_time: Optional[float] = None
        self.end_time: Optional[float] = None
        self.elapsed_time: float = 0.0

        self.load_proxies_from_file()
        self.load_sites_from_file()

    # -------------------------------
    # Runtime helpers
    # -------------------------------
    def get_runtime(self) -> float:
        if self.running and self.start_time:
            return time.time() - self.start_time
        return self.elapsed_time

    def get_runtime_str(self) -> str:
        return format_duration(self.get_runtime())

    def get_eta(self) -> Optional[float]:
        runtime = self.get_runtime()
        if not self.running or self.processed == 0 or runtime <= 0:
            return None
        rate = self.processed / runtime
        if rate <= 0:
            return None
        return (self.total - self.processed) / rate

    def get_eta_str(self) -> str:
        eta = self.get_eta()
        return "-" if eta is None else format_duration(eta)

    def get_speed(self) -> float:
        runtime = self.get_runtime()
        return 0.0 if runtime <= 0 else self.processed / runtime

    # -------------------------------
    # Remote dork fetching
    # -------------------------------
    def fetch_dorks_from_url(self, url: str, timeout: int = 30, total_timeout: int = 120, retries: int = 3) -> List[str]:
        return self.fetch_dorks_from_url_with_hooks(
            url, timeout=timeout, total_timeout=total_timeout, retries=retries
        )

    def fetch_dorks_from_url_with_hooks(
        self,
        url: str,
        on_attempt=None,
        on_response=None,
        on_lines=None,
        on_partial=None,
        timeout: int = 30,
        total_timeout: int = 120,
        retries: int = 3,
    ) -> List[str]:
        """
        Fetch dorks with:
          - per-read socket timeout (timeout)
          - HARD total wall-clock cap (total_timeout) across the whole body read
        If we hit the total cap, we keep whatever bytes we already got.
        """
        if not url:
            return []

        last_err = None
        for attempt in range(1, retries + 1):
            if on_attempt:
                try:
                    on_attempt(attempt, retries)
                except Exception:
                    pass

            attempt_start = time.time()

            try:
                req = urllib.request.Request(
                    url,
                    headers={"User-Agent": "Mozilla/5.0 (DorkBot)"},
                    method="GET",
                )
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    if resp.status != 200:
                        last_err = f"HTTP {resp.status}"
                        if attempt < retries:
                            time.sleep(min(3 * (2 ** (attempt - 1)), 30))
                        continue

                    if on_response:
                        try:
                            on_response()
                        except Exception:
                            pass

                    # ---- streaming read with HARD total cap ----
                    chunks: List[bytes] = []
                    total_bytes = 0
                    read_timed_out = False
                    read_error = None
                    read_start = time.time()

                    while True:
                        if time.time() - read_start >= total_timeout:
                            read_timed_out = True
                            read_error = f"total read time exceeded ({total_timeout}s)"
                            logger.warning(
                                f"Hit total read cap of {total_timeout}s "
                                f"({total_bytes} bytes received). Keeping partial."
                            )
                            break

                        try:
                            chunk = resp.read(65536)
                        except socket.timeout as e:
                            read_timed_out = True
                            read_error = e
                            break
                        except Exception as e:
                            read_timed_out = True
                            read_error = e
                            break

                        if not chunk:
                            break

                        chunks.append(chunk)
                        total_bytes += len(chunk)

                if total_bytes == 0:
                    last_err = f"no data received ({read_error or 'empty response'})"
                    logger.warning(
                        f"Attempt {attempt}/{retries} got 0 bytes from {url} "
                        f"after {int(time.time() - attempt_start)}s"
                    )
                    if attempt < retries:
                        time.sleep(min(3 * (2 ** (attempt - 1)), 30))
                    continue

                if read_timed_out:
                    logger.warning(
                        f"Partial read from {url} after {total_bytes} bytes "
                        f"({read_error}). Using what we got."
                    )
                    if on_partial:
                        try:
                            on_partial()
                        except Exception:
                            pass

                raw = b"".join(chunks).decode("utf-8", errors="ignore")

                head = raw[:200].lower()
                if "<html" in head or "<!doctype" in head:
                    logger.error(f"URL returned HTML, not plain text: {url}")
                    return []

                lines = [
                    l.strip()
                    for l in raw.splitlines()
                    if l.strip() and not l.strip().startswith("#")
                ]

                if on_lines:
                    try:
                        on_lines(len(lines))
                    except Exception:
                        pass

                logger.info(
                    f"Fetched {len(lines)} dorks from {url} "
                    f"(attempt {attempt}, {total_bytes} bytes, "
                    f"{int(time.time() - attempt_start)}s"
                    + (", PARTIAL" if read_timed_out else "")
                    + ")"
                )
                return lines

            except urllib.error.HTTPError as e:
                last_err = f"HTTP {e.code} {e.reason}"
                logger.warning(f"Attempt {attempt}/{retries} HTTP error for {url}: {e}")
            except urllib.error.URLError as e:
                last_err = f"URL error: {e.reason}"
                logger.warning(f"Attempt {attempt}/{retries} URL error for {url}: {e}")
            except Exception as e:
                last_err = str(e)
                logger.warning(f"Attempt {attempt}/{retries} failed for {url}: {e}")

            if attempt < retries:
                backoff = min(3 * (2 ** (attempt - 1)), 30)
                time.sleep(backoff)

        logger.error(f"All {retries} attempts failed for {url}: {last_err}")
        return []

    def load_dorks_from_remote(self, url: str) -> int:
        fetched = self.fetch_dorks_from_url(url)
        if not fetched:
            return 0
        before = len(self.dorks)
        self.dorks = list(dict.fromkeys(self.dorks + fetched))
        self.total = len(self.dorks)
        added = self.total - before
        self.save_dorks_to_file()
        return added

    # -------------------------------
    # Sites
    # -------------------------------
    def load_sites_from_file(self, filename: str = None):
        filename = filename or config.SITES_FILE
        try:
            with open(filename, "r", encoding="utf-8") as f:
                lines = [l.strip() for l in f if l.strip()]
            self.unique_sites = deduplicate_by_domain(set(lines))
            logger.info(f"Loaded {len(self.unique_sites)} existing sites from {filename}")
        except FileNotFoundError:
            logger.info("No existing sites file found. Starting fresh.")
            self.unique_sites = set()

    # -------------------------------
    # Dorks
    # -------------------------------
    def load_dorks_from_file(self, filename: str = None) -> int:
        filename = filename or config.DORKS_FILE
        try:
            with open(filename, "r", encoding="utf-8") as f:
                lines = [l.strip() for l in f if l.strip() and not l.startswith("#")]
            return self.set_dorks(lines)
        except FileNotFoundError:
            logger.warning(f"Dorks file '{filename}' not found.")
            return 0

    def set_dorks(self, dorks_list: List[str]) -> int:
        self.dorks = list(dict.fromkeys(dorks_list))
        self.total = len(self.dorks)
        self.processed = 0
        self.failed = 0
        return self.total

    def add_dorks(self, new_dorks: List[str]) -> int:
        self.dorks = list(dict.fromkeys(self.dorks + new_dorks))
        self.total = len(self.dorks)
        self.save_dorks_to_file()
        return self.total

    def clear_dorks(self) -> bool:
        self.dorks = []
        self.total = 0
        self.processed = 0
        self.failed = 0
        self.save_dorks_to_file()
        return True

    def save_dorks_to_file(self, filename: str = None):
        filename = filename or config.DORKS_FILE
        try:
            with open(filename, "w", encoding="utf-8") as f:
                f.write("\n".join(self.dorks))
            logger.info(f"Saved {len(self.dorks)} dorks to {filename}")
        except Exception as e:
            logger.error(f"Failed to write {filename}: {e}")

    # -------------------------------
    # Proxies
    # -------------------------------
    def load_proxies_from_file(self, filename: str = None) -> int:
        filename = filename or config.PROXIES_FILE
        try:
            with open(filename, "r", encoding="utf-8") as f:
                lines = [l.strip() for l in f if l.strip() and not l.startswith("#")]
        except FileNotFoundError:
            logger.info(f"Proxies file '{filename}' not found. Running without proxies.")
            self.proxies = []
            return 0
        parsed = [parse_proxy_line(l) for l in lines]
        self.proxies = [p for p in parsed if p]
        logger.info(f"Loaded {len(self.proxies)} proxies from {filename}")
        return len(self.proxies)

    def set_proxies(self, proxy_lines: List[str]) -> int:
        parsed = [parse_proxy_line(l) for l in proxy_lines]
        self.proxies = [p for p in parsed if p]
        self.current_proxy_index = 0
        self.save_proxies_to_file()
        return len(self.proxies)

    def add_proxies(self, proxy_lines: List[str]) -> int:
        parsed = [parse_proxy_line(l) for l in proxy_lines]
        valid = [p for p in parsed if p]
        self.proxies = list(dict.fromkeys(self.proxies + valid))
        self.save_proxies_to_file()
        return len(self.proxies)

    def clear_proxies(self) -> bool:
        self.proxies = []
        self.current_proxy_index = 0
        self.save_proxies_to_file()
        return True

    def save_proxies_to_file(self, filename: str = None):
        filename = filename or config.PROXIES_FILE
        try:
            lines = []
            for proxy_url in self.proxies:
                parsed = urlparse(proxy_url)
                if parsed.username and parsed.password:
                    lines.append(f"{parsed.hostname}:{parsed.port}:{parsed.username}:{parsed.password}")
                else:
                    lines.append(f"{parsed.hostname}:{parsed.port}")
            with open(filename, "w", encoding="utf-8") as f:
                f.write("\n".join(lines))
            logger.info(f"Saved {len(lines)} proxies to {filename}")
        except Exception as e:
            logger.error(f"Failed to write {filename}: {e}")

    def get_next_proxy(self) -> Optional[str]:
        if not self.proxies:
            return None
        proxy = self.proxies[self.current_proxy_index % len(self.proxies)]
        self.current_proxy_index += 1
        return proxy

    # -------------------------------
    # Search control
    # -------------------------------
    async def start_search(self, max_results=config.MAX_RESULTS_PER_DORK, workers=config.WORKERS, request_timeout=config.REQUEST_TIMEOUT) -> bool:
        if self.running:
            return False

        self.load_dorks_from_file()

        remote_url = getattr(config, "DORKS_URL", "") or ""
        if remote_url:
            logger.info(f"Merging dorks from remote URL: {remote_url}")
            await asyncio.to_thread(self.load_dorks_from_remote, remote_url)

        if not self.dorks:
            return False

        self.running = True
        self._stop_requested = False
        self.processed = 0
        self.failed = 0
        self.proxy_retries = 0
        self.last_update_time = time.time()
        self.current_proxy_index = 0
        self.start_time = time.time()
        self.end_time = None
        self.elapsed_time = 0.0

        self.search_task = asyncio.create_task(self._run_search(max_results, workers, request_timeout))
        return True

    async def stop_search(self) -> bool:
        if not self.running:
            return False
        self._stop_requested = True
        if self.search_task and not self.search_task.done():
            try:
                await asyncio.wait_for(self.search_task, timeout=10.0)
            except asyncio.TimeoutError:
                self.search_task.cancel()
                try:
                    await self.search_task
                except asyncio.CancelledError:
                    pass
            except asyncio.CancelledError:
                pass
        self.running = False
        self._stop_requested = False
        return True

    async def _run_search(self, max_results: int, workers: int, request_timeout: int):
        queue = asyncio.Queue()
        for dork in self.dorks:
            await queue.put(dork)

        async def worker():
            while not self._stop_requested:
                try:
                    dork = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                try:
                    await self._process_dork(dork, max_results, request_timeout)
                finally:
                    queue.task_done()

        worker_count = max(1, min(workers, len(self.dorks)))
        worker_tasks = [asyncio.create_task(worker()) for _ in range(worker_count)]

        try:
            await queue.join()
        except asyncio.CancelledError:
            for t in worker_tasks:
                t.cancel()
            raise

        for t in worker_tasks:
            if not t.done():
                t.cancel()
                try:
                    await t
                except asyncio.CancelledError:
                    pass

        self.running = False
        self.end_time = time.time()
        if self.start_time:
            self.elapsed_time = self.end_time - self.start_time

        async with self.lock:
            self.unique_sites = deduplicate_by_domain(self.unique_sites)
        await self.write_sites_file()

        logger.info(f"Search complete. Proxy retries used: {self.proxy_retries}")

    async def _process_dork(self, dork: str, max_results: int, request_timeout: int):
        if self._stop_requested:
            return

        async with self.lock:
            self.current_dork = dork

        max_attempts = len(self.proxies) if self.proxies else 1
        results = None
        last_err = None

        for attempt in range(1, max_attempts + 1):
            if self._stop_requested:
                break

            proxy = self.get_next_proxy()

            try:
                results = await asyncio.wait_for(
                    asyncio.to_thread(self._ddgs_search, dork, max_results, request_timeout, proxy),
                    timeout=request_timeout + 10,
                )
                if attempt > 1:
                    async with self.lock:
                        self.proxy_retries += (attempt - 1)
                break

            except asyncio.CancelledError:
                raise
            except Exception as e:
                last_err = e
                logger.warning(
                    f"Dork '{dork}' attempt {attempt}/{max_attempts} "
                    f"failed via {proxy or 'no-proxy'}: {e}"
                )
                if attempt < max_attempts and not self._stop_requested:
                    await asyncio.sleep(0.5)

        if results is None:
            logger.error(f"Dork '{dork}' failed after {max_attempts} proxy attempt(s): {last_err}")
            async with self.lock:
                self.failed += 1
                self.processed += 1
                self.current_dork = None
                self.last_update_time = time.time()
            return

        try:
            for r in results:
                url = r.get("href", "")
                if url:
                    normalized = normalize_url(url)
                    async with self.lock:
                        self.unique_sites.add(normalized)

            async with self.lock:
                self.unique_sites = deduplicate_by_domain(self.unique_sites)
            await self.write_sites_file()
        finally:
            async with self.lock:
                self.processed += 1
                self.current_dork = None
                self.last_update_time = time.time()

    def _ddgs_search(self, query: str, max_results: int, request_timeout: int, proxy: Optional[str]) -> List[Dict]:
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
        async with self.lock:
            return sorted(self.unique_sites)

    async def write_sites_file(self, filename: str = None):
        filename = filename or config.SITES_FILE
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
                "proxy_retries": self.proxy_retries,
                "runtime": self.get_runtime(),
                "runtime_str": self.get_runtime_str(),
                "eta_str": self.get_eta_str(),
                "speed": self.get_speed(),
            }

    def is_running(self) -> bool:
        return self.running
