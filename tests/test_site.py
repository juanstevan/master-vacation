"""Regression tests for the mastervacationhomes.com adapter.

Every case here is a bug that actually bit during the first live run against
the real site. Run with: python3 tests/test_site.py
"""
from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mvh.config import Settings  # noqa: E402
from mvh.site import (  # noqa: E402
    blocked_ranges, build_record, parse_home, parse_letter, parse_summary,
    pick_dates,
)

SETTINGS = Settings(base_url="https://mastervacationhomes.com")

# Trimmed from the real /home/390765, keeping the structure that matters.
HOME_HTML = """
<html><head>
  <meta property="og:title" content="Master Vacation Homes - 5 BD/ 5.5 BA/ Windsor Island (1906 SD)" />
  <meta property="place:location:latitude" content="28.33563" />
  <meta property="place:location:longitude" content="-81.52822" />
</head><body>
<div class="row mt-4"><div class="col-lg-10 offset-lg-1">
  <h4>14 guests &#9679; 5 beds &#9679; 5.5 baths &#9679; Windsor Island Resort (1906 SD)</h4>
</div></div>
<h5 class="mb-0">House rules</h5>
<p class="text-sm mb-3">No pets, parties, or smoking allowed.</p>
<h5 class="mb-0">Free cancellation for 48 hours</h5>
<p class="text-sm mb-3">After that, check our cancellation policy for details.</p>
<h3>Main Features</h3>
<div class="container"><div class="row">
  <div class="col-6 mb-1"><span class="material-symbols-outlined align-middle">pool</span> Private Pool</div>
  <div class="col-6 mb-1"><span class="material-symbols-outlined align-middle">pool</span> Communal Pool</div>
  <div class="col-6 mb-1"><span class="material-symbols-outlined align-middle">wifi</span> WiFi</div>
  <div class="col-6 mb-1"><span class="material-symbols-outlined align-middle">square_foot</span> 2,622 sf / 244 m2</div>
</div></div>
<p>&bull; Community: Windsor Island Resort<br>&bull; Disney (7.7 miles) SeaWorld (17.0 miles)<br>
   &bull; Location: Summer Drive, Davenport, FL 33897</p>
<p id="more" class="collapse">DETAILS<br>The garage has been converted into a game room.<br>Minimum 3 nights.</p>
<img src="https://cdn.ciirus.com/properties/76642/390765/images/hd/a.jpg">
<img src="https://mastervacationhomes.com/images/contact.png">
<script>
new Litepicker({ element: x, minDate: "2026-08-26",
  lockDays: [ ["09/03/2026", "09/05/2026"], ["09/13/2026", "09/14/2026"], ] });
</script>
</body></html>
"""

QUOTE_HTML = """
<html><body>
<h4>14 guests &#9679; 5 beds &#9679; 5.5 baths &#9679; House &#9679; Windsor Island Resort (1906 SD)</h4>
<div id="confirmation"><div id="confirmation_body">
<p>CHECK-IN: after 4:00pm<br>CHECK-OUT: before 10:00am</p>
<p>RESORT/COMMUNITY: Windsor Island Resort<br>1104 Aloha Blvd, Davenport, FL 33897</p>
<p>GATE ACCESS: Everyone over 18 must be registered.</p>
<p>HOME ADDRESS<br>1906 Summer Drive<br>Davenport, FL<br>33897</p>
<p>DOOR CODE: [DOOR_CODE] (Last 4 digits of your phone number)</p>
<p>WIFI NETWORK: MasterVacationHomes.com<br>WIFI PASSWORD: welcometoorlando</p>
<p>OPTIONAL SERVICES<br>* BBQ Rental $75.00 all stay.<br>* Pool Heat $35.00 per day.</p>
<p>give us a call at (407) 873-0226.</p>
</div></div></body></html>
"""

QUOTE_JSON = {
    "nights": 4, "QuoteBaseRate": 691, "cleaning": "382.00", "service": "139.49",
    "taxes": "128.76", "QuoteExcludingTax": "1212.49", "QuoteIncludingTax": "1341.25",
    "PropertyType": "House", "Community": "Windsor Island Resort", "Bedrooms": 5,
    "Bathrooms": "5.5", "Sleeps": 14, "HasPool": 1, "HasSpa": 0, "GamesRoom": 0,
    "WebsitePropertyName": "5 BD/ 5.5 BA/ Windsor Island (1906 SD)",
    "_window": "2026-09-21..2026-09-25",
}


def record():
    return build_record("390765", SETTINGS, parse_home(HOME_HTML),
                        parse_letter(QUOTE_HTML), dict(QUOTE_JSON))


# ------------------------------------------------------------------ summary
def test_summary_without_property_type():
    got = parse_summary("<h4>14 guests ● 5 beds ● 5.5 baths ● Windsor Island Resort (1906 SD)</h4>")
    assert got["sleeps"] == "14" and got["bedrooms"] == "5", got
    assert got["bathrooms"] == "5.5", got
    assert got["community"] == "Windsor Island Resort", got
    assert got["house_number"] == "1906 SD", got
    assert "property_type" not in got, got


def test_summary_with_property_type():
    got = parse_summary("<h4>4 guests ● 2 beds ● 2 baths ● Apartment ● Storey Lake (3120-403 PC)</h4>")
    assert got["property_type"] == "Apartment", got
    assert got["house_number"] == "3120-403 PC", got   # hyphenated unit numbers
    assert got["community"] == "Storey Lake", got


# ----------------------------------------------------------------- features
def test_square_footage_is_not_an_amenity():
    """`square_foot` is a measurement tile in the same grid as the amenities."""
    home = parse_home(HOME_HTML)
    assert home["sqft"] == "2622", home.get("sqft")
    assert not any("sf" in f or "m2" in f for f in home["_features"]), home["_features"]


def test_specific_pool_beats_the_coarse_quote_flag():
    """HasPool=1 only says "a pool"; Main Features says which kind."""
    assert record().get("pool") == "Private pool", record().get("pool")


def test_spa_not_invented_from_resort_amenities():
    assert not record().get("spa"), record().get("spa")


RESORT_BLOCK_HTML = HOME_HTML.replace(
    "The garage has been converted into a game room.",
    "WINDSOR ISLAND RESORT AMENITIES<br>- Games Room<br>- Hot Tub<br>- Fitness Center")


def test_resort_amenities_are_not_read_as_home_amenities():
    """Nearly every description ends with a RESORT AMENITIES block. Reading it
    put a games room in 324 homes that do not have one."""
    rec = build_record("390765", SETTINGS, parse_home(RESORT_BLOCK_HTML),
                       parse_letter(QUOTE_HTML), dict(QUOTE_JSON, GamesRoom=0))
    assert not rec.get("game_room"), rec.get("game_room")
    assert not rec.get("spa"), rec.get("spa")


def test_game_room_comes_from_the_features_list():
    html = HOME_HTML.replace(
        '<span class="material-symbols-outlined align-middle">wifi</span> WiFi',
        '<span class="material-symbols-outlined align-middle">videogame_asset</span> Game Room')
    rec = build_record("390765", SETTINGS, parse_home(html),
                       parse_letter(QUOTE_HTML), dict(QUOTE_JSON))
    assert rec.get("game_room") == "Yes", dict(rec)


def test_pool_heat_needs_a_private_pool():
    """"POOL HEAT - OPTIONAL" is boilerplate in every description, including
    homes that only have access to the communal pool."""
    html = HOME_HTML.replace("Private Pool", "Communal Pool").replace(
        "The garage has been converted into a game room.",
        "POOL HEAT - OPTIONAL 1-Cost: $35 per day.")
    rec = build_record("390765", SETTINGS, parse_home(html),
                       parse_letter(QUOTE_HTML), dict(QUOTE_JSON, HasPool=0))
    assert rec.get("pool") == "Communal pool", rec.get("pool")
    assert not rec.get("pool_heat"), rec.get("pool_heat")


# ------------------------------------------------------- rules / letter
def test_house_rules_yield_pets_and_smoking():
    """"No pets, parties, or smoking allowed" never says "no smoking"."""
    rec = record()
    assert rec.get("pets") == "No pets", rec.get("pets")
    assert rec.get("smoking") == "Non-smoking", rec.get("smoking")


def test_letter_gives_the_home_address_not_the_resort_address():
    letter = parse_letter(QUOTE_HTML)
    assert letter["address"] == "1906 Summer Drive", letter
    assert letter["city"] == "Davenport" and letter["state"] == "FL", letter
    assert letter["zip"] == "33897", letter
    assert letter["resort_address"].startswith("1104 Aloha Blvd"), letter


def test_quote_page_header_supplies_the_property_type():
    """The listing header omits it; the quote header has it. Free fallback for
    homes whose getquote returns nothing because they are fully booked."""
    letter = parse_letter(QUOTE_HTML)
    assert letter["property_type"] == "House", letter
    rec = build_record("390765", SETTINGS, parse_home(HOME_HTML),
                       parse_letter(QUOTE_HTML), None)
    assert rec.get("property_type") == "House", dict(rec)


def test_city_casing_normalised():
    """The letters are hand-typed per home, so casing varies."""
    got = parse_letter('<div id="confirmation_body"><p>HOME ADDRESS<br>'
                       '1 A St<br>kissimmee, FL<br>34747</p></div>')
    assert got["city"] == "Kissimmee", got


def test_letter_extras():
    letter = parse_letter(QUOTE_HTML)
    assert letter["check_in_time"] == "4:00pm", letter
    assert letter["check_out_time"] == "10:00am", letter
    assert letter["wifi_password"] == "welcometoorlando", letter
    assert letter["phone"] == "(407) 873-0226", letter
    assert "BBQ Rental $75.00 all stay" in letter["optional_services"], letter


# ------------------------------------------------------------ coordinates
def test_office_coordinates_are_not_used_as_home_coordinates():
    """place:location:* is the company office, identical on every listing."""
    rec = record()
    assert not rec.get("latitude") and not rec.get("longitude"), dict(rec)


# ----------------------------------------------------------- availability
def test_blocked_ranges_parsed_from_the_datepicker():
    ranges = blocked_ranges(HOME_HTML)
    assert ranges == [(date(2026, 9, 3), date(2026, 9, 5)),
                      (date(2026, 9, 13), date(2026, 9, 14))], ranges


def test_pick_dates_skips_blocked_ranges():
    today = date.today()
    locks = [(today + timedelta(days=21), today + timedelta(days=40))]
    window = pick_dates(locks, nights=4, offset=21, horizon=120)
    assert window, "no free window found"
    checkin, checkout = window
    assert not (locks[0][0] <= checkout and checkin <= locks[0][1]), window


def test_pick_dates_returns_none_when_everything_is_booked():
    today = date.today()
    assert pick_dates([(today, today + timedelta(days=400))],
                      nights=4, offset=21, horizon=120) is None


# ------------------------------------------------------------------ money
def test_quote_money_is_formatted_and_rate_is_per_night():
    rec = record()
    assert rec.get("cleaning_fee") == "$382.00", rec.get("cleaning_fee")
    assert rec.get("quote_total") == "$1,341.25", rec.get("quote_total")
    assert rec.get("rate") == "$172.75", rec.get("rate")   # 691 / 4 nights
    assert rec.get("quote_nights") == 4, rec.get("quote_nights")


def test_record_survives_a_home_with_no_quote():
    rec = build_record("409572", SETTINGS, parse_home(HOME_HTML),
                       parse_letter(QUOTE_HTML), None)
    assert rec.get("community") == "Windsor Island Resort"
    assert not rec.get("cleaning_fee")
    assert rec.get("pool") == "Private pool"


def test_images_limited_to_the_property_cdn():
    home = parse_home(HOME_HTML)
    assert home["_images"] == [
        "https://cdn.ciirus.com/properties/76642/390765/images/hd/a.jpg"], home["_images"]


def run():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for test in tests:
        try:
            test()
            print(f"  PASS  {test.__name__}")
        except AssertionError as exc:
            failed += 1
            print(f"  FAIL  {test.__name__}: {exc}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"  ERROR {test.__name__}: {type(exc).__name__}: {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(run())
