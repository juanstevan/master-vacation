"""mastervacationhomes.com adapter.

Written against the real markup, not guessed at — every selector below was
checked against live pages. Three sources per home:

    /home/{id}            listing: features, description, photos, blocked dates
    /home/{id}/quote      confirmation letter: address, wifi, check-in times
    /home/{id}/getquote   JSON: property type, pool/spa flags, live pricing

The JSON endpoint is the booking widget's own "Get Quote" call (a price
lookup). Nothing here books anything.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from bs4 import BeautifulSoup

from .config import Settings
from .httpclient import PoliteSession
from .records import Record

log = logging.getLogger("mvh.site")

SEARCH_PATH = "/search"
HOME_LINK = re.compile(r"/home/(\d+)")
BULLET = "[●•]"

# "14 guests ● 5 beds ● 5.5 baths ● [House ●] Windsor Island Resort (1906 SD)"
SUMMARY = re.compile(
    rf"(\d+)\s*guests?\s*{BULLET}\s*(\d+)\s*beds?\s*{BULLET}\s*"
    rf"([\d.]+)\s*baths?\s*{BULLET}\s*(.+)$", re.I)

LOCK_PAIR = re.compile(r'\["(\d{2}/\d{2}/\d{4})"\s*,\s*"(\d{2}/\d{2}/\d{4})"\]')
CSRF = re.compile(r'name="csrf-token"\s+content="([^"]+)"')
SQFT = re.compile(r"([\d,]+)\s*sf\b", re.I)

# Main Features labels are a controlled vocabulary; map the ones that have
# their own spreadsheet column. Everything else still lands in `amenities`.
FEATURE_FLAGS: list[tuple[re.Pattern, str, str]] = [
    (re.compile(r"private\s+pool", re.I), "pool", "Private pool"),
    (re.compile(r"communal\s+pool|community\s+pool", re.I), "pool", "Communal pool"),
    (re.compile(r"pool\s+heat|heated\s+pool", re.I), "pool_heat", "Available"),
    (re.compile(r"spa|hot\s*tub|jacuzzi", re.I), "spa", "Yes"),
    (re.compile(r"game[s]?\s*room|arcade", re.I), "game_room", "Yes"),
    (re.compile(r"bbq|barbecue|grill", re.I), "grill", "Yes"),
    (re.compile(r"wifi|wi-fi", re.I), "wifi", "Yes"),
]


def _text(node) -> str:
    return re.sub(r"[ \t\xa0]+", " ", node.get_text(" ", strip=True)) if node else ""


def _lines(node) -> list[str]:
    """Text with <br> and block boundaries preserved as newlines."""
    if node is None:
        return []
    raw = node.get_text("\n", strip=True)
    return [re.sub(r"[ \t\xa0]+", " ", ln).strip() for ln in raw.split("\n") if ln.strip()]


# ----------------------------------------------------------------- discovery
def discover_ids(session: PoliteSession, settings: Settings,
                 max_pages: int = 400) -> list[str]:
    """Walk /search?page=N until a page yields no listings."""
    base = settings.base_url.rstrip("/")
    seen: set[str] = set()
    for page in range(1, max_pages + 1):
        _, body = session.get(f"{base}{SEARCH_PATH}?page={page}")
        found = set(HOME_LINK.findall(body))
        if not found:
            log.info("search page %d empty - stopping", page)
            break
        before = len(seen)
        seen |= found
        log.info("search page %d: %d listings (%d new, %d total)",
                 page, len(found), len(seen) - before, len(seen))
    return sorted(seen, key=int)


# ------------------------------------------------------------- availability
def blocked_ranges(html: str) -> list[tuple[date, date]]:
    """The datepicker's lockDays — the home's booked/unavailable dates."""
    start = html.find("lockDays")
    if start < 0:
        return []
    end = html.find("});", start)
    chunk = html[start:end if end > 0 else start + 20000]
    out = []
    for a, b in LOCK_PAIR.findall(chunk):
        try:
            out.append((datetime.strptime(a, "%m/%d/%Y").date(),
                        datetime.strptime(b, "%m/%d/%Y").date()))
        except ValueError:
            continue
    return out


def pick_dates(locks, nights: int = 4, offset: int = 21,
               horizon: int = 300) -> Optional[tuple[date, date]]:
    """First free `nights`-night window at least `offset` days out."""
    today = date.today()
    for day in range(offset, horizon):
        checkin = today + timedelta(days=day)
        checkout = checkin + timedelta(days=nights)
        if not any(start <= checkout and checkin <= end for start, end in locks):
            return checkin, checkout
    return None


# ------------------------------------------------------------ page parsing
def parse_summary(html: str) -> dict:
    """The `N guests ● N beds ● N baths ● [Type ●] Community (House no.)` line."""
    for match in re.finditer(r"<h4[^>]*>(.*?)</h4>", html, re.S):
        line = _text(BeautifulSoup(match.group(1), "lxml"))
        found = SUMMARY.search(line)
        if not found:
            continue
        sleeps, beds, baths, tail = found.groups()
        parts = [p.strip() for p in re.split(BULLET, tail) if p.strip()]
        out = {"sleeps": sleeps, "bedrooms": beds, "bathrooms": baths}
        if len(parts) > 1:
            out["property_type"] = parts[0]
        last = parts[-1]
        house = re.search(r"\(([^()]*)\)\s*$", last)
        if house:
            number = house.group(1).strip()
            # six listings print "(N/A)"; that is a null marker, not a number
            if number.upper().strip(" .") not in ("N/A", "NA", "-", ""):
                out["house_number"] = number
            last = last[: house.start()].strip()
        out["community"] = last
        return out
    return {}


def parse_home(html: str) -> dict:
    """Features, description, photos from /home/{id}."""
    soup = BeautifulSoup(html, "lxml")
    out: dict = dict(parse_summary(html))

    features, sqft = [], None
    for div in soup.select("div.col-6.mb-1"):
        icon = div.find("span", class_="material-symbols-outlined")
        label = _text(div)
        if icon:
            label = label.replace(_text(icon), "", 1).strip()
        if not label:
            continue
        if icon and _text(icon) == "square_foot":
            got = SQFT.search(label)
            if got:
                sqft = got.group(1).replace(",", "")
            continue                      # size is a measurement, not an amenity
        features.append(label)
    out["_features"] = features
    if sqft:
        out["sqft"] = sqft

    # The overview paragraph: "• Community: ... • Location: ..."
    overview = ""
    for para in soup.find_all("p"):
        text = _text(para)
        if "• Community:" in text or "Community:" in text and "•" in text:
            overview = "\n".join(_lines(para))
            break
    details = "\n".join(_lines(soup.find(id="more")))

    rules = []
    for heading in soup.select("h5.mb-0"):
        blurb = heading.find_next_sibling("p")
        rules.append(f"{_text(heading)}: {_text(blurb)}" if blurb else _text(heading))
    out["_rules"] = "\n".join(r for r in rules if r)
    out["_overview"] = overview
    out["_details"] = details

    for line in overview.split("\n"):
        got = re.match(r"•?\s*Location:\s*(.+)", line, re.I)
        if got:
            out["_location_line"] = got.group(1).strip()
    distances = [ln.strip("• ").strip() for ln in overview.split("\n")
                 if re.search(r"\(\d+(?:\.\d+)?\s*miles?\)", ln)]
    if distances:
        out["distances"] = " | ".join(distances)

    images, seen = [], set()
    for img in soup.find_all("img"):
        src = (img.get("src") or "").strip()
        if "cdn.ciirus.com" in src and src not in seen:
            seen.add(src)
            images.append(src)
    out["_images"] = images

    locks = blocked_ranges(html)
    if locks:
        out["blocked_dates"] = " | ".join(
            f"{a:%Y-%m-%d}..{b:%Y-%m-%d}" for a, b in locks)
    out["_locks"] = locks

    # NB: the page's place:location:* meta tags are the *company office*,
    # identical on every listing, so they are deliberately not used as
    # per-home coordinates.
    title = soup.find("meta", property="og:title") or soup.find(
        "meta", attrs={"name": "og:title"})
    if title and title.get("content"):
        out["name"] = title["content"]
    return out


# --------------------------------------------------- confirmation letter
LETTER_FIELDS = [
    ("check_in_time", re.compile(r"CHECK[- ]?IN:\s*(?:after\s*)?(.+)", re.I)),
    ("check_out_time", re.compile(r"CHECK[- ]?OUT:\s*(?:before\s*)?(.+)", re.I)),
    ("wifi_network", re.compile(r"WIFI NETWORK:\s*(.+)", re.I)),
    ("wifi_password", re.compile(r"WIFI PASSWORD:\s*(.+)", re.I)),
    ("door_code_rule", re.compile(r"DOOR CODE:\s*(.+)", re.I)),
    ("gate_access", re.compile(r"GATE ACCESS:\s*(.+)", re.I)),
]
PHONE = re.compile(r"\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}")
CITY_STATE_ZIP = re.compile(r"^(.+?),\s*([A-Z]{2})\s*,?\s*(\d{5})?$")


def parse_letter(html: str) -> dict:
    """The confirmation-letter modal on /home/{id}/quote."""
    soup = BeautifulSoup(html, "lxml")
    # The quote page's own header carries the property type, which the listing
    # page's header omits -- free fallback for homes whose getquote comes back
    # empty because they are fully booked.
    out: dict = parse_summary(html)
    body = soup.find(id="confirmation_body") or soup.find(id="confirmation")
    lines = _lines(body)
    if not lines:
        return out
    out["_letter"] = "\n".join(lines)

    for field, pattern in LETTER_FIELDS:
        for line in lines:
            got = pattern.search(line)
            if got and got.group(1).strip():
                out[field] = got.group(1).strip().rstrip(".")
                break

    for index, line in enumerate(lines):
        got = re.match(r"RESORT/COMMUNITY:\s*(.+)", line, re.I)
        if got:
            out.setdefault("community", got.group(1).strip())
            if index + 1 < len(lines) and re.search(r"\d{5}", lines[index + 1]):
                out["resort_address"] = lines[index + 1].strip()
        if re.match(r"HOME ADDRESS\s*$", line, re.I):
            block = [ln for ln in lines[index + 1:index + 5]
                     if not re.match(r"(DOOR CODE|TO OPEN|WIFI)", ln, re.I)]
            if block:
                out["address"] = block[0]
            for part in block[1:]:
                got = CITY_STATE_ZIP.match(part)
                if got:
                    city = got.group(1)
                    if city.islower() or city.isupper():
                        city = city.title()   # letters are hand-typed per home
                    out["city"], out["state"] = city, got.group(2)
                    if got.group(3):
                        out["zip"] = got.group(3)
                elif re.fullmatch(r"\d{5}(-\d{4})?", part):
                    out["zip"] = part
        if re.match(r"OPTIONAL SERVICES\s*$", line, re.I):
            extras = []
            for part in lines[index + 1:index + 8]:
                if not part.startswith("*"):
                    break
                extras.append(part.lstrip("* ").strip())
            if extras:
                out["optional_services"] = " | ".join(extras)

    phone = PHONE.search("\n".join(lines))
    if phone:
        out["phone"] = phone.group(0)
    return out


# ---------------------------------------------------------------- pricing
def fetch_quote_json(session: PoliteSession, settings: Settings, home_id: str,
                     token: str, window: tuple[date, date]) -> Optional[dict]:
    checkin, checkout = window
    url = f"{settings.base_url.rstrip('/')}/home/{home_id}/getquote"
    payload = session.post_json(
        url,
        data={"datetimes": f"{checkin:%m/%d/%Y} to {checkout:%m/%d/%Y}",
              "poolheat": 0, "_token": token},
        headers={"X-CSRF-TOKEN": token,
                 "Referer": f"{settings.base_url.rstrip('/')}/home/{home_id}/quote"},
    )
    if not isinstance(payload, dict) or not payload or "error" in payload:
        return None
    payload["_window"] = f"{checkin:%Y-%m-%d}..{checkout:%Y-%m-%d}"
    return payload


QUOTE_MAP = {
    "PropertyType": "property_type",
    "Community": "community",
    "Bedrooms": "bedrooms",
    "Bathrooms": "bathrooms",
    "Sleeps": "sleeps",
    "cleaning": "cleaning_fee",
    "service": "service_fee",
    "taxes": "quote_taxes",
    "QuoteIncludingTax": "quote_total",
    "QuoteExcludingTax": "quote_subtotal",
}


def _money(value) -> Optional[str]:
    try:
        return f"${float(str(value).replace(',', '').replace('$', '')):,.2f}"
    except (TypeError, ValueError):
        return None


# ------------------------------------------------------------------ record
def build_record(home_id: str, settings: Settings, home: dict, letter: dict,
                 quote: Optional[dict]) -> Record:
    rec = Record()
    base = settings.base_url.rstrip("/")
    rec.put("listing_id", home_id, "url")
    rec.put("url", f"{base}/home/{home_id}", "url")
    rec.put("quote_url", f"{base}/home/{home_id}/quote", "url")

    if quote:
        for key, field in QUOTE_MAP.items():
            value = quote.get(key)
            if value in (None, "", 0) and field != "cleaning_fee":
                continue
            if field in ("cleaning_fee", "service_fee", "quote_taxes",
                         "quote_total", "quote_subtotal"):
                value = _money(value)
            rec.put(field, value, "quote-json")
        nights = quote.get("nights") or 0
        base_rate = quote.get("QuoteBaseRate")
        if nights and base_rate:
            rec.put("rate", _money(float(base_rate) / float(nights)), "quote-json")
            rec.put("quote_nights", nights, "quote-json")
        rec.put("quote_dates", quote.get("_window"), "quote-json")
        rec.put("name", quote.get("WebsitePropertyName"), "quote-json")

    for field in ("house_number", "property_type", "community", "bedrooms",
                  "bathrooms", "sleeps", "sqft",
                  "name", "distances", "blocked_dates"):
        if home.get(field):
            rec.put(field, home[field], "listing-page")

    for field in ("address", "city", "state", "zip", "community", "phone",
                  "property_type", "house_number", "bedrooms", "bathrooms", "sleeps",
                  "check_in_time", "check_out_time", "wifi_network",
                  "wifi_password", "door_code_rule", "gate_access",
                  "resort_address", "optional_services"):
        if letter.get(field):
            rec.put(field, letter[field], "confirmation-letter")
    if rec.get("state"):
        rec.put("country", "USA", "derived")

    # Fall back to the overview's "Location:" line when the letter has no address.
    if not rec.get("address") and home.get("_location_line"):
        got = re.match(r"(.+?),\s*([A-Za-z .]+),\s*([A-Z]{2})\s*(\d{5})?",
                       home["_location_line"])
        if got:
            rec.put("address", got.group(1).strip(), "listing-page")
            rec.put("city", got.group(2).strip(), "listing-page")
            rec.put("state", got.group(3).strip(), "listing-page")
            if got.group(4):
                rec.put("zip", got.group(4), "listing-page")

    rec.amenities = list(home.get("_features") or [])
    joined = " ; ".join(rec.amenities)
    for pattern, field, label in FEATURE_FLAGS:
        if pattern.search(joined):
            rec.put(field, label, "features")
    if quote:
        # coarse fallbacks; the Main Features list above is more specific
        for key, field in (("HasPool", "pool"), ("HasSpa", "spa"),
                           ("GamesRoom", "game_room")):
            if quote.get(key):
                rec.put(field, "Yes", "quote-json")
    rec.put("amenity_count", len(rec.amenities), "features")
    rec.put("amenities", "; ".join(rec.amenities), "features")

    rec.images = list(home.get("_images") or [])
    rec.put("image_count", len(rec.images), "listing-page")
    if rec.images:
        rec.put("primary_image", rec.images[0], "listing-page")

    rec.put("house_rules", home.get("_rules"), "listing-page")
    text = "\n".join(filter(None, [home.get("_rules", ""), home.get("_details", ""),
                                   home.get("_overview", "")]))
    if re.search(r"\bno pets\b|pets\s+are\s+not|no pets, parties", text, re.I):
        rec.put("pets", "No pets", "listing-page")
    elif re.search(r"pet[\s-]friendly|pets\s+(?:are\s+)?(?:allowed|welcome)", text, re.I):
        rec.put("pets", "Pet friendly", "listing-page")
    if re.search(r"non[\s-]?smoking|\bno\b[^.\n]{0,40}\bsmoking\b", text, re.I):
        rec.put("smoking", "Non-smoking", "listing-page")
    # In-home amenities come from the structured Main Features list and the
    # Ciirus flags only. The description text is NOT a safe source for them:
    # nearly every one ends with a "RESORT AMENITIES" block listing the
    # community's games room, hot tub and gym, which are not in the house.
    if rec.get("pool") == "Private pool" and re.search(r"pool heat", text, re.I):
        rec.put("pool_heat", "Available", "listing-page")

    description = "\n\n".join(
        p for p in (home.get("_overview"), home.get("_details")) if p)
    if description:
        # assigned rather than put(): the line structure is what makes this
        # readable, and put() -> clean() collapses newlines into spaces.
        rec["description"] = description.strip()
        rec.sources["description"] = "listing-page"

    if letter.get("_letter"):
        rec.extras["confirmation_letter"] = letter["_letter"]
        cancel = re.search(r"([^\n]*cancellation[^\n]*)", letter["_letter"], re.I)
        if cancel:
            rec.put("cancellation_policy", cancel.group(1).strip(), "confirmation-letter")
    if not rec.get("cancellation_policy"):
        cancel = re.search(r"([^\n]{0,120}cancellation[^\n]{0,200})", text, re.I)
        if cancel:
            rec.put("cancellation_policy", cancel.group(1).strip(), "listing-page")

    if quote:
        nights = quote.get("nights")
        stay = f" ({nights} nights)" if nights else ""
        rec.fees = [(label, _money(quote[key]) or str(quote[key]))
                    for key, label in (
                        ("QuoteBaseRate", f"Rental rate{stay}"),
                        ("cleaning", "Cleaning fee"),
                        ("service", "Service fee"),
                        ("taxes", "Taxes"),
                        ("QuoteExcludingTax", "Subtotal before tax"),
                        ("QuoteIncludingTax", "Total"))
                    if quote.get(key) not in (None, "")]
        if quote.get("_window"):
            rec.fees.append(("Quoted for dates", quote["_window"]))
    rec.put("scraped_at", datetime.now(timezone.utc).isoformat(timespec="seconds"), "run")
    return rec
