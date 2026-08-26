"""Polite HTTP session: rate limiting, retries, robots.txt, on-disk cache."""
from __future__ import annotations

import logging
import random
import time
import urllib.robotparser
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter

from .config import Settings

log = logging.getLogger("mvh.http")

RETRY_STATUS = {408, 425, 429, 500, 502, 503, 504}


class RobotsBlocked(RuntimeError):
    pass


class PoliteSession:
    """One request at a time, spaced out, with resumable disk caching.

    Caching matters more than speed here: a crawl that dies at home 400 of 600
    should not re-download the first 399.
    """

    def __init__(
        self,
        settings: Settings,
        cache_dir: Optional[Path] = None,
        allow_disallowed: bool = False,
    ):
        self.s = settings
        self.cache_dir = Path(cache_dir) if cache_dir else None
        if self.cache_dir:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.allow_disallowed = allow_disallowed
        self._last_request = 0.0
        self._robots: dict[str, Optional[urllib.robotparser.RobotFileParser]] = {}

        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": settings.user_agent,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
                **settings.extra_headers,
            }
        )
        adapter = HTTPAdapter(pool_connections=4, pool_maxsize=4)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)

    # ---------------------------------------------------------------- robots
    def _robots_for(self, url: str) -> Optional[urllib.robotparser.RobotFileParser]:
        root = "{u.scheme}://{u.netloc}".format(u=urlparse(url))
        if root in self._robots:
            return self._robots[root]
        rp = urllib.robotparser.RobotFileParser()
        try:
            resp = self.session.get(root + "/robots.txt", timeout=self.s.timeout)
            if resp.status_code == 200:
                rp.parse(resp.text.splitlines())
            else:
                # No robots.txt served == nothing disallowed.
                rp.parse([])
        except requests.RequestException as exc:
            log.warning("could not read robots.txt (%s); assuming allowed", exc)
            rp.parse([])
        self._robots[root] = rp
        return rp

    def allowed(self, url: str) -> bool:
        if not self.s.respect_robots or self.allow_disallowed:
            return True
        rp = self._robots_for(url)
        return bool(rp and rp.can_fetch(self.s.user_agent, url))

    def crawl_delay(self, url: str) -> float:
        rp = self._robots_for(url)
        try:
            cd = rp.crawl_delay(self.s.user_agent) if rp else None
        except Exception:
            cd = None
        return float(cd) if cd else 0.0

    # ----------------------------------------------------------------- cache
    def _cache_path(self, key: str) -> Optional[Path]:
        if not self.cache_dir:
            return None
        safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in key)[:150]
        return self.cache_dir / f"{safe}.html"

    # ------------------------------------------------------------------ wait
    def _throttle(self, url: str) -> None:
        delay = max(self.s.delay, self.crawl_delay(url))
        elapsed = time.monotonic() - self._last_request
        wait = delay - elapsed
        if wait > 0:
            time.sleep(wait)
        if self.s.jitter:
            time.sleep(random.uniform(0, self.s.jitter))
        self._last_request = time.monotonic()

    # ------------------------------------------------------------------- get
    def get(
        self,
        url: str,
        cache_key: Optional[str] = None,
        refresh: bool = False,
    ) -> tuple[int, str]:
        """Return (status_code, body). Status 0 means "served from cache"."""
        path = self._cache_path(cache_key) if cache_key else None
        if path and path.exists() and not refresh:
            return 0, path.read_text(encoding="utf-8", errors="replace")

        if not self.allowed(url):
            raise RobotsBlocked(
                f"robots.txt disallows {url} for this user-agent. "
                "This is your own site, so if that is a stale rule you can pass "
                "--allow-disallowed to proceed."
            )

        last_exc: Optional[Exception] = None
        for attempt in range(self.s.max_retries + 1):
            self._throttle(url)
            try:
                resp = self.session.get(
                    url, timeout=self.s.timeout, allow_redirects=True
                )
            except requests.RequestException as exc:
                last_exc = exc
                backoff = 2 ** attempt
                log.warning("%s -> %s; retrying in %ss", url, exc, backoff)
                time.sleep(backoff)
                continue

            if resp.status_code in RETRY_STATUS and attempt < self.s.max_retries:
                retry_after = resp.headers.get("Retry-After")
                try:
                    backoff = float(retry_after) if retry_after else 2 ** attempt
                except ValueError:
                    backoff = 2 ** attempt
                log.warning(
                    "%s -> HTTP %s; backing off %ss", url, resp.status_code, backoff
                )
                time.sleep(min(backoff, 120))
                continue

            body = resp.text
            if path and resp.status_code == 200:
                path.write_text(body, encoding="utf-8")
            return resp.status_code, body

        raise requests.RequestException(f"giving up on {url}: {last_exc}")
