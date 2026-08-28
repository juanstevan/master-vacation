"""Merge extraction layers into one canonical record per home."""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Optional

from .extract import (
    JSON_KEY_ALIASES,
    LABEL_MAP,
    clean,
    deep_amenities,
    deep_find,
    normalize_label,
    to_number,
)

# Column order for the spreadsheet.
FIELDS = [
    "house_number", "listing_id", "name", "property_type", "community",
    "bedrooms", "bathrooms", "sleeps", "sqft",     "address", "city", "state", "zip", "country",
    "pool", "pool_heat", "spa", "game_room", "grill", "wifi",
    "pets", "smoking", "house_rules", "check_in_time", "check_out_time",
    "rate",     "quote_total",     "quote_taxes", "cleaning_fee", "cancellation_policy",
    "phone",
    "amenity_count", "amenities", "image_count", "primary_image",
    "description", "url", "quote_url", "scraped_at",
    # site-specific columns (mastervacationhomes.com)
    "quote_subtotal", "service_fee", "quote_nights", "quote_dates",
    "wifi_network", "wifi_password", "door_code_rule", "gate_access",
    "resort_address", "optional_services", "distances", "blocked_dates",
]

# Numeric coercion is independent of the spreadsheet columns: the generic
# extraction layer still reads these for other sites even where
# mastervacationhomes.com never publishes them.
NUMERIC_FIELDS = {
    "bedrooms", "bathrooms", "half_baths", "sleeps", "sqft", "floors",
    "min_nights", "latitude", "longitude", "rating", "review_count",
}

MONEY = re.compile(r"(?:\$|USD\s*)\s*-?[\d,]+(?:\.\d{2})?")


class Record(dict):
    """A home, plus a note of where every value came from."""

    def __init__(self) -> None:
        super().__init__()
        self.sources: dict[str, str] = {}
        self.extras: dict[str, str] = {}
        self.amenities: list[str] = []
        self.images: list[str] = []
        self.fees: list[tuple[str, str]] = []
        self.notes: list[str] = []

    def put(self, field: str, value: Any, source: str) -> None:
        if value in (None, "", [], {}):
            return
        if self.get(field) not in (None, ""):
            return
        if isinstance(value, bool) and field not in NUMERIC_FIELDS:
            # petsAllowed: false reads as "No" in a spreadsheet, not "False".
            value = "Yes" if value else "No"
        if isinstance(value, str):
            value = clean(value)
            if not value:
                return
            if len(value) > 20000:
                value = value[:20000]
        if field in NUMERIC_FIELDS:
            number = to_number(value)
            if number is None:
                return
            value = int(number) if float(number).is_integer() else number
        self[field] = value
        self.sources[field] = source


# ------------------------------------------------------------ text heuristics
TEXT_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("bedrooms", re.compile(r"(\d{1,2})\s*(?:bed\s?rooms?|bedroom|\bbr\b|\bbd\b)", re.I)),
    ("bathrooms", re.compile(r"(\d{1,2}(?:\.\d)?)\s*(?:bath\s?rooms?|bathroom|\bba\b)", re.I)),
    ("sleeps", re.compile(r"sleeps\s*(?:up\s*to\s*)?(\d{1,2})", re.I)),
    ("house_number", re.compile(
        r"(?:home|house|unit|property)\s*(?:#|no\.?|number)\s*[:\-]?\s*(\d{3,6})", re.I)),
    ("check_in_time", re.compile(
        r"check[\s\-]?in[^0-9]{0,15}(\d{1,2}(?::\d{2})?\s*(?:a\.?m\.?|p\.?m\.?))", re.I)),
    ("check_out_time", re.compile(
        r"check[\s\-]?out[^0-9]{0,15}(\d{1,2}(?::\d{2})?\s*(?:a\.?m\.?|p\.?m\.?))", re.I)),
    ("min_nights", re.compile(r"(?:minimum|min\.?)[^.\n]{0,25}?(\d{1,2})\s*night", re.I)),
    ("sqft", re.compile(r"([\d,]{3,7})\s*(?:sq\.?\s*(?:ft|feet)|square\s*feet)", re.I)),
    ("latitude", re.compile(r"\"?lat(?:itude)?\"?\s*[:=]\s*\"?(-?\d{1,3}\.\d+)", re.I)),
    ("longitude", re.compile(r"\"?(?:lng|lon|longitude)\"?\s*[:=]\s*\"?(-?\d{1,3}\.\d+)", re.I)),
    ("confirmation_number", re.compile(
        r"(?:confirmation|reservation)\s*(?:#|no\.?|number)\s*[:\-]?\s*([A-Z0-9\-]{4,20})", re.I)),
]

FLAG_PATTERNS: list[tuple[str, re.Pattern, str]] = [
    ("pool", re.compile(r"private\s+pool", re.I), "Private pool"),
    ("pool", re.compile(r"community\s+pool", re.I), "Community pool"),
    ("pool", re.compile(r"\bpool\b", re.I), "Yes"),
    ("spa", re.compile(r"\b(?:hot\s*tub|spa|jacuzzi)\b", re.I), "Yes"),
    ("pool_heat", re.compile(r"pool\s+heat|heated\s+pool", re.I), "Available"),
    ("game_room", re.compile(r"game[s]?\s*room", re.I), "Yes"),
    ("grill", re.compile(r"\b(?:bbq|barbecue|grill)\b", re.I), "Yes"),
    ("wifi", re.compile(r"\bwi[\s\-]?fi\b|\bwireless internet\b", re.I), "Yes"),
    ("pets", re.compile(r"\bno pets\b|pets\s+are\s+not", re.I), "No pets"),
    ("pets", re.compile(r"pet[\s\-]friendly|pets\s+(?:are\s+)?(?:allowed|welcome)", re.I),
     "Pet friendly"),
    ("smoking", re.compile(r"\bnon[\s\-]?smoking\b|\bno smoking\b", re.I), "Non-smoking"),
]


def apply_text_heuristics(record: Record, text: str, source: str) -> None:
    head = text[:6000]  # facts live near the top; avoid matching review prose
    for field, pattern in TEXT_PATTERNS:
        if record.get(field) not in (None, ""):
            continue
        match = pattern.search(head) or pattern.search(text)
        if match:
            record.put(field, match.group(1), source)
    for field, pattern, label in FLAG_PATTERNS:
        if record.get(field) in (None, "") and pattern.search(text):
            record.put(field, label, source)


# --------------------------------------------------------------------- merge
def _from_kv(record: Record, pairs: dict[str, str], source: str) -> None:
    for raw_key, value in pairs.items():
        field = LABEL_MAP.get(normalize_label(raw_key))
        if field:
            record.put(field, value, source)
        else:
            record.extras.setdefault(clean(raw_key), clean(value))


def _from_jsonld(record: Record, blocks: list[Any], source: str) -> None:
    for field, aliases in JSON_KEY_ALIASES.items():
        value = deep_find(blocks, aliases)
        if value is not None:
            record.put(field, value, source)
    # schema.org nests the address one level down.
    address = deep_find(blocks, ("streetAddress",))
    if address:
        record.put("address", address, source)


def _from_meta(record: Record, meta: dict[str, str], source: str) -> None:
    record.put("name", meta.get("og:title") or meta.get("<title>"), source)
    record.put(
        "description",
        meta.get("og:description") or meta.get("description") or meta.get("twitter:description"),
        source,
    )
    record.put("primary_image", meta.get("og:image"), source)
    record.put("latitude", meta.get("place:location:latitude"), source)
    record.put("longitude", meta.get("place:location:longitude"), source)


def extract_fees(kv: dict[str, str]) -> list[tuple[str, str]]:
    return [
        (clean(key), clean(value))
        for key, value in kv.items()
        if MONEY.search(str(value))
    ]


def build_record(
    home_id: str,
    home_layers: Optional[dict[str, Any]],
    quote_layers: Optional[dict[str, Any]] = None,
    home_url: str = "",
    quote_url: str = "",
) -> Record:
    record = Record()
    record.put("listing_id", home_id, "url")
    record.put("url", home_url, "url")

    if home_layers:
        _from_jsonld(record, home_layers.get("jsonld") or [], "jsonld")
        _from_jsonld(record, home_layers.get("state") or [], "embedded-json")
        _from_kv(record, home_layers.get("microdata") or {}, "microdata")
        _from_kv(record, home_layers.get("kv") or {}, "page-table")
        _from_meta(record, home_layers.get("meta") or {}, "meta-tag")

        headings = home_layers.get("headings") or []
        if headings:
            record.put("name", headings[0], "h1")

        record.amenities = list(
            dict.fromkeys(
                (home_layers.get("amenities") or [])
                + deep_amenities(home_layers.get("state") or [])
                + deep_amenities(home_layers.get("jsonld") or [])
            )
        )
        record.images = list(home_layers.get("images") or [])
        apply_text_heuristics(record, home_layers.get("text") or "", "page-text")
        record.put("description", home_layers.get("paragraph"), "longest-paragraph")
        record["page_text"] = home_layers.get("text") or ""
    else:
        record.notes.append("listing page missing")

    if quote_layers:
        record.put("quote_url", quote_url, "url")
        _from_kv(record, quote_layers.get("kv") or {}, "quote-table")
        apply_text_heuristics(record, quote_layers.get("text") or "", "quote-text")
        record.fees = extract_fees(quote_layers.get("kv") or {})
        record["quote_text"] = quote_layers.get("text") or ""
        if not record.amenities:
            record.amenities = list(quote_layers.get("amenities") or [])
    else:
        record["quote_text"] = ""

    if record.images:
        record.put("primary_image", record.images[0], "first-image")
    record["amenity_count"] = len(record.amenities)
    record["image_count"] = len(record.images)
    record["amenities"] = "; ".join(record.amenities)
    record["scraped_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")

    for field in FIELDS:
        record.setdefault(field, "")
    return record


def record_to_row(record: Record) -> dict[str, Any]:
    return {field: record.get(field, "") for field in FIELDS}
