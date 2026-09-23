import asyncio
import gc
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
FETCH_TOTAL_TIMEOUT = 60
FETCH_READ_TIMEOUT = 1


def now_str() -> str:
    return datetime.now(TZ).strftime("%I:%M:%S %p")


def format_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes}m {secs}s"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def normalize_url(url: str) -> str:
    try:
        parsed = urlparse(url.strip())
        scheme, netloc = parsed.scheme.lower(), parsed.netloc.lower()
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
        netloc = parsed.netloc.lower()
        if netloc.startswith("www."):
            netloc = netloc[4:]
        return f"{parsed.scheme.lower()}://{netloc}"
    except Exception:
        return url


def get_param_count(url: str) -> int:
    try:
        return len(parse_qs(urlparse(url).query))
    except Exception:
        return 0


def parse_proxy_line(line: str) -> Optional[str]:
    parts = line.strip().split(":")
    if not line.strip() or line.strip().startswith("#"):
        return None
    if len(parts) == 4:
        host, port, username, password = parts
        return f"http://{username}:{password}@{host}:{port}"
    if len(parts) == 2:
        return f"http://{parts[0]}:{parts[1]}"
    logger.warning(f"Invalid proxy format: {line}")
    return None


def deduplicate_by_domain(urls: Set[str]) -> Set[str]:
    selected: Dict[str, str] = {}
    for url in urls:
        key = get_domain_key(url)
        old = selected.get(key)
        if old is None or get_param_count(url) > get_param_count(old) or (
            get_param_count(url) == get_param_count(old) and len(url) > len(old)
        ):
            selected[key] = url
    return set(selected.values())


class SearchManager:
    def __init__(self):
        self.dorks: List[str] = []
        self.total = self.processed = self.failed = 0
        self.current_dork: Optional[str] = None
        self.unique_sites: Set[str] = set()
        self.proxies: List[str] = []
        self.current_proxy_index = 0
        self.proxy_retries = 0
        self.running = False
        self.search_task: Optional[asyncio.Task] = None
        self._stop_requested = False
        self.lock = asyncio.Lock()
        self.file_lock = asyncio.Lock()
        self.last_update_time = time.time()
        self.start_time: Optional[float] = None
        self.end_time: Optional[float] = None
        self.elapsed_time = 0.0
        self.load_proxies_from_file()
        self.load_sites_from_file()

    # -------------------------------
    # Runtime helpers
    # -------------------------------
    def get_runtime(self) -> float:
        return time.time() - self.start_time if self.running and self.start_time else self.elapsed_time

    def get_runtime_str(self) -> str:
        return format_duration(self.get_runtime())

    def get_eta(self) -> Optional[float]:
        runtime = self.get_runtime()
        if not self.running or not self.processed or runtime <= 0:
            return None
        return max(0, self.total - self.processed) / (self.processed / runtime)

    def get_eta_str(self) -> str:
        eta = self.get_eta()
        return "-" if eta is None else format_duration(eta)

    def get_speed(self) -> float:
        runtime = self.get_runtime()
        return self.processed / runtime if runtime > 0 else 0.0

    # -------------------------------
    # Remote fetch (streaming, no big buffer)
    # -------------------------------
    def fetch_dorks_from_url(self, url: str, timeout: int = FETCH_READ_TIMEOUT,
                             total_timeout: int = FETCH_TOTAL_TIMEOUT, retries: int = 1) -> List[str]:
        return self.fetch_dorks_from_url_with_hooks(
            url, timeout=timeout, total_timeout=total_timeout, retries=retries
        )

    def fetch_dorks_from_url_with_hooks(self, url: str, on_attempt=None, on_response=None,
                                        on_lines=None, on_partial=None,
                                        timeout: int = FETCH_READ_TIMEOUT,
                                        total_timeout: int = FETCH_TOTAL_TIMEOUT,
                                        retries: int = 1) -> List[str]:
        """
        Stream-read a raw text URL with a hard 60s wall-clock cap.
        Decodes chunks incrementally so we never buffer the full body in RAM.
        """
        if not url:
            return []

        deadline = time.monotonic() + total_timeout
        last_error = None

        for attempt in range(1, retries + 1):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            if on_attempt:
                on_attempt(attempt, retries)

            lines: List[str] = []
            partial = False

            try:
                request_timeout = max(0.1, min(float(timeout), remaining))
                req = urllib.request.Request(
                    url,
                    headers={"User-Agent": "Mozilla/5.0 (DorkBot)"},
                    method="GET",
                )
                with urllib.request.urlopen(req, timeout=request_timeout) as resp:
                    if resp.status != 200:
                        last_error = f"HTTP {resp.status}"
                        continue
                    if on_response:
                        on_response()

                    try:
                        sock = resp.fp.raw._sock
                        sock.settimeout(max(0.1, min(float(timeout), deadline - time.monotonic())))
                    except Exception:
                        pass

                    leftover = b""
                    first_chunk_check = True

                    while True:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            partial = True
                            break

                        try:
                            chunk = resp.read(min(65536, max(1, int(remaining * 65536))))
                        except (socket.timeout, TimeoutError) as exc:
                            last_error = exc
                            partial = True
                            break
                        except Exception as exc:
                            last_error = exc
                            partial = True
                            break

                        if not chunk:
                            break

                        # HTML guard on first chunk
                        if first_chunk_check:
                            head = chunk[:200].lower()
                            if b"<html" in head or b"<!doctype" in head:
                                logger.error(f"URL returned HTML, not plain text: {url}")
                                return []
                            first_chunk_check = False

                        # Decode incrementally — never keep raw bytes
                        data = leftover + chunk
                        *complete, leftover = data.split(b"\n")
                        for raw_line in complete:
                            s = raw_line.decode("utf-8", errors="ignore").strip()
                            if s and not s.startswith("#"):
                                lines.append(s)

                    # Trailing line
                    if leftover:
                        s = leftover.decode("utf-8", errors="ignore").strip()
                        if s and not s.startswith("#"):
                            lines.append(s)

                if partial and on_partial:
                    on_partial()

                if not lines:
                    last_error = last_error or "empty response"
                    continue

                if on_lines:
                    on_lines(len(lines))

                logger.info(
                    "Fetched %s lines from %s%s",
                    len(lines), url, " (PARTIAL)" if partial else ""
                )
                return lines

            except (urllib.error.HTTPError, urllib.error.URLError, socket.timeout, TimeoutError) as exc:
                last_error = exc
                logger.warning("Fetch attempt %s/%s failed: %s", attempt, retries, exc)
            except Exception as exc:
                last_error = exc
                logger.warning("Fetch attempt %s/%s failed: %s", attempt, retries, exc)

            if time.monotonic() >= deadline:
                break
            time.sleep(min(3, max(0, deadline - time.monotonic())))

        logger.error("Fetch stopped at %ss cap for %s: %s", total_timeout, url, last_error)
        return []

    def load_dorks_from_remote(self, url: str) -> int:
        fetched = self.fetch_dorks_from_url(url)
        if not fetched:
            return 0
        before = len(self.dorks)
        self.dorks = list(dict.fromkeys(self.dorks + fetched))
        self.total = len(self.dorks)
        self.save_dorks_to_file()
        return self.total - before

    # -------------------------------
    # Sites — capped to prevent OOM
    # -------------------------------
    def load_sites_from_file(self, filename=None):
        try:
            with open(filename or config.SITES_FILE, encoding="utf-8") as file:
                lines = [line.strip() for line in file if line.strip()]
            cap = getattr(config, "MAX_SITES_IN_MEMORY", 200_000)
            if len(lines) > cap:
                logger.warning(
                    "sites.txt has %s lines — trimming to last %s",
                    len(lines), cap
                )
                lines = lines[-cap:]
            self.unique_sites = deduplicate_by_domain(set(lines))
            logger.info("Loaded %s unique sites", len(self.unique_sites))
        except FileNotFoundError:
            self.unique_sites = set()

    # -------------------------------
    # Dorks
    # -------------------------------
    def load_dorks_from_file(self, filename=None) -> int:
        try:
            with open(filename or config.DORKS_FILE, encoding="utf-8") as file:
                return self.set_dorks([line.strip() for line in file if line.strip() and not line.startswith("#")])
        except FileNotFoundError:
            return 0

    def set_dorks(self, values: List[str]) -> int:
        self.dorks = list(dict.fromkeys(values))
        self.total = len(self.dorks)
        self.processed = self.failed = 0
        return self.total

    def add_dorks(self, values: List[str]) -> int:
        self.dorks = list(dict.fromkeys(self.dorks + values))
        self.total = len(self.dorks)
        self.save_dorks_to_file()
        return self.total

    def clear_dorks(self) -> bool:
        self.dorks = []
        self.total = self.processed = self.failed = 0
        self.save_dorks_to_file()
        return True

    def save_dorks_to_file(self, filename=None):
        try:
            with open(filename or config.DORKS_FILE, "w", encoding="utf-8") as file:
                file.write("\n".join(self.dorks))
        except Exception as exc:
            logger.error("Failed to save dorks: %s", exc)

    # -------------------------------
    # Proxies
    # -------------------------------
    def load_proxies_from_file(self, filename=None) -> int:
        try:
            with open(filename or config.PROXIES_FILE, encoding="utf-8") as file:
                self.proxies = [p for p in (parse_proxy_line(x) for x in file) if p]
        except FileNotFoundError:
            self.proxies = []
        return len(self.proxies)

    def set_proxies(self, values: List[str]) -> int:
        self.proxies = [p for p in (parse_proxy_line(x) for x in values) if p]
        self.current_proxy_index = 0
        self.save_proxies_to_file()
        return len(self.proxies)

    def add_proxies(self, values: List[str]) -> int:
        valid = [p for p in (parse_proxy_line(x) for x in values) if p]
        self.proxies = list(dict.fromkeys(self.proxies + valid))
        self.save_proxies_to_file()
        return len(self.proxies)

    def clear_proxies(self) -> bool:
        self.proxies = []
        self.current_proxy_index = 0
        self.save_proxies_to_file()
        return True

    def save_proxies_to_file(self, filename=None):
        try:
            with open(filename or config.PROXIES_FILE, "w", encoding="utf-8") as file:
                for proxy in self.proxies:
                    parsed = urlparse(proxy)
                    if parsed.username and parsed.password:
                        file.write(f"{parsed.hostname}:{parsed.port}:{parsed.username}:{parsed.password}\n")
                    else:
                        file.write(f"{parsed.hostname}:{parsed.port}\n")
        except Exception as exc:
            logger.error("Failed to save proxies: %s", exc)

    def get_next_proxy(self) -> Optional[str]:
        if not self.proxies:
            return None
        proxy = self.proxies[self.current_proxy_index % len(self.proxies)]
        self.current_proxy_index += 1
        return proxy

    # -------------------------------
    # Search control
    # -------------------------------
    async def start_search(self, max_results=config.MAX_RESULTS_PER_DORK,
                           workers=config.WORKERS, request_timeout=config.REQUEST_TIMEOUT) -> bool:
        if self.running:
            return False
        self.load_dorks_from_file()
        remote_url = getattr(config, "DORKS_URL", "") or ""
        if remote_url:
            await asyncio.to_thread(self.load_dorks_from_remote, remote_url)
        if not self.dorks:
            return False
        self.running = True
        self._stop_requested = False
        self.processed = self.failed = self.proxy_retries = 0
        self.current_proxy_index = 0
        self.start_time = time.time()
        self.elapsed_time = 0
        gc.collect()  # clean slate before heavy work
        self.search_task = asyncio.create_task(self._run_search(max_results, workers, request_timeout))
        return True

    async def stop_search(self) -> bool:
        if not self.running:
            return False
        self._stop_requested = True
        if self.search_task and not self.search_task.done():
            try:
                await asyncio.wait_for(self.search_task, timeout=10)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self.search_task.cancel()
        self.running = False
        self._stop_requested = False
        return True

    async def _run_search(self, max_results, workers, request_timeout):
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

        # Hard cap workers to prevent memory blowups
        actual_workers = max(1, min(workers, len(self.dorks), 3))
        tasks = [asyncio.create_task(worker()) for _ in range(actual_workers)]
        logger.info(f"Search starting with {actual_workers} worker(s)")

        try:
            await queue.join()
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()

        self.running = False
        self.end_time = time.time()
        self.elapsed_time = self.end_time - self.start_time if self.start_time else 0
        self.unique_sites = deduplicate_by_domain(self.unique_sites)
        await self.write_sites_file()
        gc.collect()

    async def _process_dork(self, dork, max_results, request_timeout):
        """Try capped # of proxies per dork. Retry with next proxy on failure."""
        if self._stop_requested:
            return
        self.current_dork = dork

        # Cap retries to prevent spawning 1000+ threads
        cap = getattr(config, "MAX_PROXY_ATTEMPTS_PER_DORK", 10)
        max_attempts = min(len(self.proxies), cap) if self.proxies else 1

        results = None
        last_error = None

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
                    self.proxy_retries += (attempt - 1)
                break
            except asyncio.TimeoutError:
                last_error = "search timed out"
                logger.warning(
                    "Dork %r attempt %s/%s TIMED OUT after %ss",
                    dork, attempt, max_attempts, request_timeout + 10
                )
                if attempt < max_attempts and not self._stop_requested:
                    await asyncio.sleep(0.5)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                last_error = exc
                logger.warning(
                    "Dork %r attempt %s/%s failed via %s: %s",
                    dork, attempt, max_attempts, proxy or "no-proxy", exc
                )
                if attempt < max_attempts and not self._stop_requested:
                    await asyncio.sleep(0.5)

        if results is None:
            logger.error("Dork %r failed after %s attempt(s): %s", dork, max_attempts, last_error)
            self.failed += 1
            self.processed += 1
            self.current_dork = None
            self.last_update_time = time.time()
            results = None
            gc.collect()
            return

        try:
            for result in results:
                if result.get("href"):
                    self.unique_sites.add(normalize_url(result["href"]))
            self.unique_sites = deduplicate_by_domain(self.unique_sites)
            await self.write_sites_file()
        except Exception as exc:
            logger.error("Error merging results for %r: %s", dork, exc)
        finally:
            self.processed += 1
            self.current_dork = None
            self.last_update_time = time.time()
            # Free the results list ASAP
            results = None
            gc.collect()

    def _ddgs_search(self, query, max_results, request_timeout, proxy=None) -> List[Dict]:
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

    async def write_sites_file(self, filename=None):
        async with self.file_lock:
            with open(filename or config.SITES_FILE, "w", encoding="utf-8") as file:
                file.write("\n".join(sorted(self.unique_sites)))

    async def get_status(self) -> Dict:
        async with self.lock:
            return {
                "running": self.running, "total": self.total, "processed": self.processed,
                "failed": self.failed, "current_dork": self.current_dork,
                "unique_count": len(self.unique_sites), "last_update": self.last_update_time,
                "workers": config.WORKERS,
                "proxy_enabled": config.PROXY_ENABLED or bool(self.proxies),
                "proxy_count": len(self.proxies), "proxy_retries": self.proxy_retries,
                "runtime": self.get_runtime(), "runtime_str": self.get_runtime_str(),
                "eta_str": self.get_eta_str(), "speed": self.get_speed(),
            }

    def is_running(self) -> bool:
        return self.running
