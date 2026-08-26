"""Download the listing and quote page for each home id."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from .config import Settings
from .httpclient import PoliteSession

log = logging.getLogger("mvh.fetch")


def fetch_all(
    session: PoliteSession,
    settings: Settings,
    ids: Iterable[str],
    raw_dir: Path,
    with_quote: bool = True,
    refresh: bool = False,
) -> dict[str, dict]:
    raw_dir = Path(raw_dir)
    raw_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = raw_dir / "_manifest.json"
    manifest: dict[str, dict] = {}
    if manifest_path.exists() and not refresh:
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            manifest = {}

    ids = list(ids)
    for index, home_id in enumerate(ids, 1):
        entry = manifest.setdefault(home_id, {})
        try:
            status, body = session.get(
                settings.home_url(home_id), cache_key=home_id, refresh=refresh
            )
            entry["home_status"] = status or 200
            entry["home_bytes"] = len(body)
        except Exception as exc:
            entry["home_status"] = "error"
            entry["home_error"] = str(exc)
            log.warning("home %s failed: %s", home_id, exc)

        if with_quote:
            try:
                status, body = session.get(
                    settings.quote_url(home_id),
                    cache_key=f"{home_id}.quote",
                    refresh=refresh,
                )
                entry["quote_status"] = status or 200
                entry["quote_bytes"] = len(body)
            except Exception as exc:
                entry["quote_status"] = "error"
                entry["quote_error"] = str(exc)
                log.warning("quote %s failed: %s", home_id, exc)

        entry["fetched_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        if index % 10 == 0 or index == len(ids):
            manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
            log.info("fetched %d/%d", index, len(ids))

    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest


def raw_ids(raw_dir: Path) -> list[str]:
    """Home ids already on disk (files named <id>.html, quotes are <id>.quote.html)."""
    ids = {
        path.stem
        for path in Path(raw_dir).glob("*.html")
        if not path.stem.startswith(("_", "sitemap", "index")) and path.stem.isdigit()
    }
    return sorted(ids, key=lambda v: int(v))
