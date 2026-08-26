"""Turn downloaded HTML into Record objects."""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional, Sequence

from .config import Settings
from .extract import extract_layers
from .fetch import raw_ids
from .records import Record, build_record

log = logging.getLogger("mvh.parse")


def _read(path: Path) -> Optional[str]:
    if not path.exists():
        return None
    text = path.read_text(encoding="utf-8", errors="replace")
    return text if text.strip() else None


def parse_raw_dir(
    raw_dir: Path, settings: Settings, ids: Optional[Sequence[str]] = None
) -> list[Record]:
    raw_dir = Path(raw_dir)
    ids = list(ids) if ids else raw_ids(raw_dir)
    records: list[Record] = []

    for home_id in ids:
        home_html = _read(raw_dir / f"{home_id}.html")
        quote_html = _read(raw_dir / f"{home_id}.quote.html")
        if home_html is None and quote_html is None:
            log.warning("no saved html for %s; skipping", home_id)
            continue
        home_layers = (
            extract_layers(home_html, settings.home_url(home_id)) if home_html else None
        )
        quote_layers = (
            extract_layers(quote_html, settings.quote_url(home_id)) if quote_html else None
        )
        records.append(
            build_record(
                home_id,
                home_layers,
                quote_layers,
                settings.home_url(home_id),
                settings.quote_url(home_id) if quote_layers else "",
            )
        )
    log.info("parsed %d homes", len(records))
    return records


def load_records(path: Path) -> list[Record]:
    """Rebuild Record objects from a parsed.jsonl file."""
    records: list[Record] = []
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            payload = json.loads(line)
            record = Record()
            record.amenities = payload.pop("_amenities", [])
            record.images = payload.pop("_images", [])
            record.fees = [tuple(f) for f in payload.pop("_fees", [])]
            record.extras = payload.pop("_extras", {})
            record.sources = payload.pop("_sources", {})
            record.notes = payload.pop("_notes", [])
            record.update(payload)
            records.append(record)
    return records
