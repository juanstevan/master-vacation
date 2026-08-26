"""Find every home id the public site exposes."""
from __future__ import annotations

import logging
import re
from typing import Optional
from urllib.parse import urljoin

from .config import INDEX_PATHS, PAGE_PARAMS, SITEMAP_PATHS, Settings
from .httpclient import PoliteSession

log = logging.getLogger("mvh.discover")

HOME_ID = re.compile(r"/home/(\d+)", re.I)
LOC = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>", re.I)


def _ids_in(text: str) -> set[str]:
    return set(HOME_ID.findall(text or ""))


def from_sitemaps(session: PoliteSession, settings: Settings) -> tuple[set[str], list[str]]:
    """Walk sitemap.xml, following sitemap indexes one level deep."""
    ids: set[str] = set()
    hits: list[str] = []
    queue = [settings.base_url.rstrip("/") + path for path in SITEMAP_PATHS]
    seen: set[str] = set()

    while queue:
        url = queue.pop(0)
        if url in seen:
            continue
        seen.add(url)
        try:
            status, body = session.get(url, cache_key=f"sitemap_{len(seen)}")
        except Exception as exc:
            log.debug("sitemap %s failed: %s", url, exc)
            continue
        if status not in (0, 200) or "<" not in body:
            continue

        found = _ids_in(body)
        if found:
            ids |= found
            hits.append(f"{url} -> {len(found)} home ids")
            log.info("sitemap %s -> %d home ids", url, len(found))

        # Nested sitemap index: queue child sitemaps (bounded).
        if "<sitemapindex" in body.lower():
            children = [loc for loc in LOC.findall(body) if loc.endswith((".xml", ".xml.gz"))]
            for child in children[:50]:
                if child not in seen:
                    queue.append(child)
    return ids, hits


def from_index_pages(
    session: PoliteSession, settings: Settings, max_pages: int = 40
) -> tuple[set[str], list[str]]:
    """Scrape listing/search pages, following simple query-string pagination."""
    ids: set[str] = set()
    hits: list[str] = []

    for path in INDEX_PATHS:
        url = urljoin(settings.base_url.rstrip("/") + "/", path.lstrip("/"))
        try:
            status, body = session.get(url, cache_key=f"index_{path.strip('/') or 'root'}")
        except Exception as exc:
            log.debug("index %s failed: %s", url, exc)
            continue
        if status not in (0, 200):
            continue
        found = _ids_in(body)
        if not found:
            continue
        ids |= found
        hits.append(f"{url} -> {len(found)} home ids")
        log.info("index %s -> %d home ids", url, len(found))

        # This path lists homes; try paginating it.
        for param in PAGE_PARAMS:
            page, stalls = 2, 0
            while page <= max_pages and stalls < 2:
                paged = f"{url}{'&' if '?' in url else '?'}{param}={page}"
                try:
                    status, body = session.get(
                        paged, cache_key=f"index_{path.strip('/') or 'root'}_{param}{page}"
                    )
                except Exception:
                    break
                if status not in (0, 200):
                    break
                new = _ids_in(body) - ids
                if new:
                    ids |= new
                    stalls = 0
                    log.info("  %s -> %d new", paged, len(new))
                else:
                    stalls += 1
                page += 1
            if page > 2 and stalls < 2:
                hits.append(f"pagination via ?{param}= worked on {url}")
                break
    return ids, hits


def from_id_range(start: int, end: int) -> set[str]:
    return {str(i) for i in range(start, end + 1)}


def read_id_file(path: str) -> set[str]:
    """One id per line; '#' comments and blank lines ignored."""
    ids: set[str] = set()
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.split("#", 1)[0].strip()
            if not line:
                continue
            for token in re.split(r"[,\s]+", line):
                match = re.search(r"\d+", token)
                if match:
                    ids.add(match.group())
    return ids


def discover(
    session: PoliteSession,
    settings: Settings,
    use_sitemap: bool = True,
    use_index: bool = True,
    id_range: Optional[tuple[int, int]] = None,
) -> tuple[list[str], list[str]]:
    ids: set[str] = set()
    report: list[str] = []

    if use_sitemap:
        found, hits = from_sitemaps(session, settings)
        ids |= found
        report += hits or ["sitemap: no home ids found"]
    if use_index:
        found, hits = from_index_pages(session, settings)
        ids |= found
        report += hits or ["index pages: no home ids found"]
    if id_range:
        ids |= from_id_range(*id_range)
        report.append(f"id range {id_range[0]}-{id_range[1]} added")

    return sorted(ids, key=lambda v: int(v)), report
