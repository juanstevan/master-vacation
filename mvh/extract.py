"""Layered extraction from a listing page.

No single selector survives a site redesign, so this pulls the same facts from
every layer a page might expose them in -- JSON-LD, embedded JS state, meta
tags, microdata, spec tables, and finally plain text -- and records which layer
each value came from. Later layers never overwrite earlier (more reliable) ones.
"""
from __future__ import annotations

import json
import re
from typing import Any, Iterable, Optional
from urllib.parse import urljoin

from bs4 import BeautifulSoup

WS = re.compile(r"\s+")
NON_ALNUM = re.compile(r"[^a-z0-9]+")


def clean(text: Optional[str]) -> str:
    if not text:
        return ""
    return WS.sub(" ", text.replace("\xa0", " ")).strip()


def normalize_label(text: str) -> str:
    return NON_ALNUM.sub(" ", (text or "").lower()).strip()


# Spec-table / label vocabulary -> canonical field name.
LABEL_MAP = {
    "bedrooms": "bedrooms", "bedroom": "bedrooms", "beds": "bedrooms",
    "br": "bedrooms", "no of bedrooms": "bedrooms", "number of bedrooms": "bedrooms",
    "bathrooms": "bathrooms", "bathroom": "bathrooms", "baths": "bathrooms",
    "bath": "bathrooms", "ba": "bathrooms", "full baths": "bathrooms",
    "number of bathrooms": "bathrooms",
    "half baths": "half_baths", "half bath": "half_baths", "powder room": "half_baths",
    "sleeps": "sleeps", "guests": "sleeps", "max guests": "sleeps",
    "maximum guests": "sleeps", "occupancy": "sleeps", "max occupancy": "sleeps",
    "maximum occupancy": "sleeps", "accommodates": "sleeps",
    "square feet": "sqft", "sq ft": "sqft", "sqft": "sqft", "size": "sqft",
    "living area": "sqft",
    "check in": "check_in_time", "check in time": "check_in_time",
    "arrival": "check_in_time", "arrival time": "check_in_time",
    "check out": "check_out_time", "check out time": "check_out_time",
    "departure": "check_out_time", "departure time": "check_out_time",
    "pets": "pets", "pet policy": "pets", "pet friendly": "pets",
    "pets allowed": "pets", "smoking": "smoking", "smoking policy": "smoking",
    "pool": "pool", "private pool": "pool", "pool type": "pool",
    "spa": "spa", "hot tub": "spa", "jacuzzi": "spa",
    "pool heat": "pool_heat", "heated pool": "pool_heat",
    "community": "community", "resort": "community", "subdivision": "community",
    "neighborhood": "community", "development": "community", "complex": "community",
    "property type": "property_type", "home type": "property_type",
    "type": "property_type", "unit type": "property_type",
    "minimum stay": "min_nights", "min stay": "min_nights",
    "minimum nights": "min_nights", "min nights": "min_nights",
    "home number": "house_number", "house number": "house_number",
    "unit number": "house_number", "unit": "house_number", "unit id": "house_number",
    "home": "house_number", "property number": "house_number",
    "property id": "listing_id", "listing id": "listing_id", "id": "listing_id",
    "address": "address", "location": "address", "street address": "address",
    "city": "city", "state": "state", "zip": "zip", "zip code": "zip",
    "postal code": "zip", "country": "country",
    "wifi": "wifi", "internet": "wifi", "wi fi": "wifi",
    "parking": "parking", "garage": "parking",
    "cancellation policy": "cancellation_policy", "cancellation": "cancellation_policy",
    "rate": "rate", "rates": "rate", "nightly rate": "rate", "price": "rate",
    "total": "quote_total", "grand total": "quote_total", "amount due": "quote_total",
    "balance": "quote_balance", "balance due": "quote_balance",
    "deposit": "quote_deposit", "security deposit": "quote_deposit",
    "taxes": "quote_taxes", "tax": "quote_taxes", "sales tax": "quote_taxes",
    "cleaning fee": "cleaning_fee", "cleaning": "cleaning_fee",
    "confirmation number": "confirmation_number",
    "reservation number": "confirmation_number",
    "confirmation": "confirmation_number",
    "phone": "phone", "telephone": "phone", "contact": "phone",
    "email": "email", "e mail": "email",
    "latitude": "latitude", "longitude": "longitude",
    "floors": "floors", "stories": "floors", "levels": "floors",
    "games room": "game_room", "game room": "game_room",
    "grill": "grill", "bbq": "grill",
}

# Keys to look for when spelunking arbitrary embedded JSON.
JSON_KEY_ALIASES: dict[str, tuple[str, ...]] = {
    "name": ("name", "title", "unitName", "propertyName", "homeName", "headline"),
    "house_number": (
        "unitNumber", "homeNumber", "houseNumber", "propertyNumber", "unitCode",
        "propertyCode", "listingNumber", "unitShortName", "code",
    ),
    "listing_id": ("id", "unitId", "propertyId", "listingId", "homeId", "unitID"),
    "bedrooms": ("bedrooms", "numberOfBedrooms", "bedroomCount", "numBedrooms", "beds"),
    "bathrooms": (
        "bathrooms", "numberOfBathrooms", "bathroomCount", "numBathrooms", "baths",
        "fullBathrooms",
    ),
    "half_baths": ("halfBathrooms", "halfBaths", "numberOfHalfBathrooms"),
    "sleeps": (
        "sleeps", "maxOccupancy", "occupancy", "maxGuests", "guests", "capacity",
        "numberOfGuests", "maximumAttendeeCapacity",
    ),
    "sqft": ("squareFeet", "sqft", "size", "livingArea", "floorSize"),
    "property_type": ("propertyType", "unitType", "homeType", "type", "category"),
    "community": ("community", "resort", "subdivision", "complex", "neighborhood", "area"),
    "address": ("address", "streetAddress", "address1", "addressLine1"),
    "city": ("city", "addressLocality", "town"),
    "state": ("state", "addressRegion", "province"),
    "zip": ("zip", "zipCode", "postalCode", "postcode"),
    "latitude": ("latitude", "lat"),
    "longitude": ("longitude", "lng", "lon", "long"),
    "description": ("description", "longDescription", "summary", "details", "overview"),
    "check_in_time": ("checkIn", "checkInTime", "arrivalTime"),
    "check_out_time": ("checkOut", "checkOutTime", "departureTime"),
    "min_nights": ("minNights", "minimumNights", "minStay", "minimumStay"),
    "rate": ("rate", "nightlyRate", "basePrice", "price", "averageNightlyRate"),
    "rating": ("ratingValue", "rating", "averageRating"),
    "review_count": ("reviewCount", "numReviews", "totalReviews"),
    "pets": ("petsAllowed", "pets", "petFriendly"),
    "pool": ("pool", "hasPool", "privatePool", "poolType"),
    "spa": ("spa", "hotTub", "hasSpa", "jacuzzi"),
}

AMENITY_KEYS = (
    "amenities", "amenityFeature", "features", "unitAmenities", "amenityList",
    "propertyAmenities", "facilities",
)

SKIP_TEXT_TAGS = ("script", "style", "noscript", "template", "svg", "iframe")


# --------------------------------------------------------------------- values
def to_number(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    m = re.search(r"-?\d+(?:\.\d+)?", str(value).replace(",", ""))
    return float(m.group()) if m else None


TRUEISH = {"yes", "y", "true", "1", "allowed", "available", "included", "included."}
FALSEISH = {"no", "n", "false", "0", "not allowed", "none", "n a", "na", "unavailable"}


def to_bool(value: Any) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    key = normalize_label(str(value))
    if key in TRUEISH:
        return True
    if key in FALSEISH:
        return False
    return None


# ------------------------------------------------------------------- JSON-LD
def extract_jsonld(soup: BeautifulSoup) -> list[Any]:
    blocks = []
    for tag in soup.find_all("script", attrs={"type": re.compile("ld\\+json", re.I)}):
        raw = tag.string or tag.get_text() or ""
        raw = raw.strip()
        if not raw:
            continue
        try:
            blocks.append(json.loads(raw))
        except json.JSONDecodeError:
            # Some CMSes emit several concatenated objects or trailing commas.
            for chunk in re.findall(r"\{.*?\}(?=\s*\{|\s*$)", raw, re.S):
                try:
                    blocks.append(json.loads(chunk))
                except json.JSONDecodeError:
                    pass
    return blocks


# ------------------------------------------------------- embedded JS payloads
STATE_HINTS = re.compile(
    r"(?:window|self|globalThis)?\.?"
    r"(__NEXT_DATA__|__NUXT__|__INITIAL_STATE__|__PRELOADED_STATE__|__APOLLO_STATE__"
    r"|APP_DATA|appData|pageData|propertyData|unitData|listingData|homeData"
    r"|initialData|__data|dataLayer)\s*=\s*",
    re.I,
)


def _balanced(text: str, start: int) -> Optional[str]:
    """Slice a balanced {...} or [...] beginning at ``start``, string-aware."""
    opener = text[start]
    closer = {"{": "}", "[": "]"}.get(opener)
    if not closer:
        return None
    depth = 0
    in_string = False
    quote = ""
    escaped = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == quote:
                in_string = False
            continue
        if ch in "\"'`":
            in_string, quote = True, ch
        elif ch == opener:
            depth += 1
        elif ch == closer:
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


def extract_embedded_state(html: str, soup: BeautifulSoup) -> list[Any]:
    """Grab JSON payloads the page hands to its own JavaScript."""
    found: list[Any] = []

    # <script type="application/json"> ... </script>  (Next.js and friends)
    for tag in soup.find_all("script", attrs={"type": re.compile("application/json", re.I)}):
        raw = (tag.string or tag.get_text() or "").strip()
        if raw:
            try:
                found.append(json.loads(raw))
            except json.JSONDecodeError:
                pass

    # window.SOMETHING = {...};
    for match in STATE_HINTS.finditer(html):
        idx = match.end()
        while idx < len(html) and html[idx] in " \t\r\n":
            idx += 1
        if idx < len(html) and html[idx] in "{[":
            blob = _balanced(html, idx)
            if blob:
                try:
                    found.append(json.loads(blob))
                except json.JSONDecodeError:
                    pass

    # Last resort: any sizeable JSON object literal assigned in a script tag.
    if not found:
        for tag in soup.find_all("script"):
            body = tag.string or tag.get_text() or ""
            if len(body) < 80 or "{" not in body:
                continue
            for m in re.finditer(r"=\s*(\{)", body):
                blob = _balanced(body, m.start(1))
                if not blob or len(blob) < 80:
                    continue
                try:
                    parsed = json.loads(blob)
                except json.JSONDecodeError:
                    continue
                if isinstance(parsed, dict) and len(parsed) >= 3:
                    found.append(parsed)
    return found


# ---------------------------------------------------------------- meta / head
def extract_meta(soup: BeautifulSoup) -> dict[str, str]:
    meta: dict[str, str] = {}
    for tag in soup.find_all("meta"):
        key = tag.get("property") or tag.get("name") or tag.get("itemprop")
        content = tag.get("content")
        if key and content:
            meta.setdefault(key.strip().lower(), clean(content))
    if soup.title and soup.title.string:
        meta.setdefault("<title>", clean(soup.title.string))
    canonical = soup.find("link", rel=lambda v: v and "canonical" in v)
    if canonical and canonical.get("href"):
        meta.setdefault("<canonical>", canonical["href"].strip())
    return meta


def extract_microdata(soup: BeautifulSoup) -> dict[str, str]:
    out: dict[str, str] = {}
    for el in soup.select("[itemprop]"):
        name = el.get("itemprop")
        value = (
            el.get("content")
            or el.get("datetime")
            or el.get("href")
            or clean(el.get_text(" ", strip=True))
        )
        if name and value:
            out.setdefault(name.strip(), clean(str(value))[:2000])
    return out


# ------------------------------------------------------- key/value structures
def extract_kv_pairs(soup: BeautifulSoup) -> dict[str, str]:
    """Spec tables, definition lists, label/value div pairs, 'Label: value' text."""
    pairs: dict[str, str] = {}

    def put(key: str, value: str) -> None:
        key, value = clean(key).rstrip(":").strip(), clean(value)
        if key and value and len(key) <= 60 and len(value) <= 500:
            pairs.setdefault(key, value)

    for table in soup.find_all("table"):
        for row in table.find_all("tr"):
            cells = row.find_all(["th", "td"])
            if len(cells) == 2:
                put(cells[0].get_text(" ", strip=True), cells[1].get_text(" ", strip=True))

    for dl in soup.find_all("dl"):
        terms = dl.find_all("dt")
        defs = dl.find_all("dd")
        for dt, dd in zip(terms, defs):
            put(dt.get_text(" ", strip=True), dd.get_text(" ", strip=True))

    # <div class="label">Bedrooms</div><div class="value">4</div>
    # <li> is excluded on purpose: in an amenity list, "Private Pool" is a
    # value, not a label for the item that follows it.
    for el in soup.find_all(["div", "span", "p", "strong", "b", "label", "h4", "h5"]):
        text = clean(el.get_text(" ", strip=True))
        if not text or len(text) > 60:
            continue
        candidate = text.rstrip(":").strip()
        if normalize_label(candidate) not in LABEL_MAP:
            continue
        # A real label/value pair sits in a small container. Three or more
        # same-tag siblings means this is a list being repeated, not a pair.
        parent = el.parent
        if parent is not None and len(parent.find_all(el.name, recursive=False)) >= 3:
            continue
        sibling = el.find_next_sibling()
        hops = 0
        while sibling is not None and hops < 3:
            value = clean(sibling.get_text(" ", strip=True))
            if value and len(value) <= 200:
                put(candidate, value)
                break
            sibling = sibling.find_next_sibling()
            hops += 1

    # "Bedrooms: 4" inside one element.
    for el in soup.find_all(["li", "p", "span", "div", "td"]):
        if el.find(["li", "p", "div", "table"]):
            continue
        text = clean(el.get_text(" ", strip=True))
        if ":" not in text or len(text) > 200:
            continue
        key, _, value = text.partition(":")
        if normalize_label(key) in LABEL_MAP:
            put(key, value)

    return pairs


# ------------------------------------------------------------------ amenities
AMENITY_HEADING = re.compile(r"amenit|feature|include|facilit|highlight", re.I)


def extract_amenities(soup: BeautifulSoup) -> list[str]:
    found: list[str] = []

    for el in soup.select('[class*="amenit" i], [id*="amenit" i], [class*="feature" i]'):
        for li in el.find_all("li"):
            found.append(clean(li.get_text(" ", strip=True)))

    for heading in soup.find_all(["h1", "h2", "h3", "h4", "h5", "strong"]):
        if not AMENITY_HEADING.search(heading.get_text() or ""):
            continue
        node = heading.find_next_sibling()
        hops = 0
        while node is not None and hops < 4:
            if node.name in ("ul", "ol"):
                found.extend(clean(li.get_text(" ", strip=True)) for li in node.find_all("li"))
                break
            inner = node.find(["ul", "ol"]) if hasattr(node, "find") else None
            if inner:
                found.extend(clean(li.get_text(" ", strip=True)) for li in inner.find_all("li"))
                break
            node = node.find_next_sibling()
            hops += 1

    seen: set[str] = set()
    result = []
    for item in found:
        if not item or len(item) > 120:
            continue
        key = item.lower()
        if key not in seen:
            seen.add(key)
            result.append(item)
    return result


# --------------------------------------------------------------------- images
def extract_images(soup: BeautifulSoup, base_url: Optional[str]) -> list[str]:
    urls: list[str] = []
    for img in soup.find_all("img"):
        src = img.get("src") or img.get("data-src") or img.get("data-lazy-src")
        if src:
            urls.append(src)
        srcset = img.get("srcset") or img.get("data-srcset")
        if srcset:
            urls.extend(part.strip().split(" ")[0] for part in srcset.split(",") if part.strip())
    for source in soup.find_all("source"):
        srcset = source.get("srcset")
        if srcset:
            urls.extend(part.strip().split(" ")[0] for part in srcset.split(",") if part.strip())

    seen: set[str] = set()
    result = []
    for url in urls:
        if not url or url.startswith("data:"):
            continue
        if re.search(r"(logo|icon|sprite|placeholder|pixel|favicon|blank)", url, re.I):
            continue
        absolute = urljoin(base_url, url) if base_url else url
        if absolute not in seen:
            seen.add(absolute)
            result.append(absolute)
    return result


# ----------------------------------------------------------------------- text
def extract_text(soup: BeautifulSoup) -> str:
    """Readable body text -- the raw material for RAG chunks."""
    clone = BeautifulSoup(str(soup), "lxml")
    for tag in clone(list(SKIP_TEXT_TAGS)):
        tag.decompose()
    for selector in ("nav", "footer", "header"):
        for tag in clone.find_all(selector):
            tag.decompose()
    main = clone.find("main") or clone.find(attrs={"role": "main"}) or clone.body or clone
    lines = [clean(line) for line in main.get_text("\n").splitlines()]
    kept: list[str] = []
    for line in lines:
        if not line:
            continue
        if kept and kept[-1] == line:
            continue
        kept.append(line)
    return "\n".join(kept)


def longest_paragraph(soup: BeautifulSoup, minimum: int = 60) -> str:
    """Fallback description for pages with no meta or structured data."""
    best = ""
    for para in soup.find_all("p"):
        text = clean(para.get_text(" ", strip=True))
        if len(text) >= minimum and len(text) > len(best):
            best = text
    return best


def extract_headings(soup: BeautifulSoup) -> list[str]:
    return [
        clean(h.get_text(" ", strip=True))
        for h in soup.find_all(["h1", "h2", "h3"])
        if clean(h.get_text(" ", strip=True))
    ]


# ----------------------------------------------------------- JSON deep search
def walk_json(node: Any) -> Iterable[dict]:
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from walk_json(value)
    elif isinstance(node, list):
        for value in node:
            yield from walk_json(value)


def deep_find(blocks: list[Any], aliases: Iterable[str]) -> Optional[Any]:
    """First plausible value stored under any of ``aliases``, at any depth."""
    lowered = {a.lower(): a for a in aliases}
    for block in blocks:
        for obj in walk_json(block):
            for key, value in obj.items():
                if not isinstance(key, str) or key.lower() not in lowered:
                    continue
                if value in (None, "", [], {}):
                    continue
                if isinstance(value, (str, int, float, bool)):
                    return value
                if isinstance(value, dict):
                    for inner in ("name", "value", "text", "@value"):
                        if isinstance(value.get(inner), (str, int, float)):
                            return value[inner]
    return None


def deep_amenities(blocks: list[Any]) -> list[str]:
    out: list[str] = []
    for block in blocks:
        for obj in walk_json(block):
            for key, value in obj.items():
                if not isinstance(key, str) or key not in AMENITY_KEYS:
                    continue
                if isinstance(value, list):
                    for item in value:
                        if isinstance(item, str):
                            out.append(item)
                        elif isinstance(item, dict):
                            label = item.get("name") or item.get("title") or item.get("label")
                            if isinstance(label, str):
                                out.append(label)
                elif isinstance(value, str):
                    out.extend(part.strip() for part in value.split(",") if part.strip())
    seen: set[str] = set()
    result = []
    for item in out:
        item = clean(item)
        key = item.lower()
        if item and key not in seen:
            seen.add(key)
            result.append(item)
    return result


# ------------------------------------------------------------------ front door
def extract_layers(html: str, url: Optional[str] = None) -> dict[str, Any]:
    soup = BeautifulSoup(html, "lxml")
    return {
        "jsonld": extract_jsonld(soup),
        "state": extract_embedded_state(html, soup),
        "meta": extract_meta(soup),
        "microdata": extract_microdata(soup),
        "kv": extract_kv_pairs(soup),
        "amenities": extract_amenities(soup),
        "images": extract_images(soup, url),
        "headings": extract_headings(soup),
        "paragraph": longest_paragraph(soup),
        "text": extract_text(soup),
    }
