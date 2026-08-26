"""Run with: python3 tests/test_extract.py   (also works under pytest)"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mvh.extract import (  # noqa: E402
    _balanced, extract_amenities, extract_kv_pairs, extract_layers, deep_find,
)
from mvh.records import build_record  # noqa: E402
from bs4 import BeautifulSoup  # noqa: E402


def soup(html):
    return BeautifulSoup(html, "lxml")


def test_balanced_handles_braces_inside_strings():
    text = 'var x = {"a": "}{ not a brace", "b": {"c": 1}};'
    blob = _balanced(text, text.index("{"))
    assert blob == '{"a": "}{ not a brace", "b": {"c": 1}}', blob


def test_balanced_handles_escaped_quotes():
    text = '= {"a": "say \\"hi\\"", "b": 2}'
    blob = _balanced(text, text.index("{"))
    assert blob.endswith('"b": 2}'), blob


def test_jsonld_and_deep_find():
    html = """<html><head><script type="application/ld+json">
    {"@type":"Accommodation","numberOfBedrooms":5,
     "address":{"streetAddress":"1 Test St","addressLocality":"Testville"}}
    </script></head><body></body></html>"""
    layers = extract_layers(html)
    assert len(layers["jsonld"]) == 1
    assert deep_find(layers["jsonld"], ("numberOfBedrooms",)) == 5
    assert deep_find(layers["jsonld"], ("addressLocality",)) == "Testville"


def test_embedded_state_is_found():
    html = """<html><body><script>
    window.__INITIAL_STATE__ = {"unit":{"unitNumber":"1906","bedrooms":6}};
    </script></body></html>"""
    layers = extract_layers(html)
    assert layers["state"], "no embedded state found"
    assert deep_find(layers["state"], ("unitNumber",)) == "1906"


def test_table_pairs():
    pairs = extract_kv_pairs(soup(
        "<table><tr><th>Bedrooms</th><td>4</td></tr>"
        "<tr><th>Sleeps</th><td>10</td></tr></table>"))
    assert pairs["Bedrooms"] == "4"
    assert pairs["Sleeps"] == "10"


def test_label_value_divs():
    pairs = extract_kv_pairs(soup(
        '<div class="d"><div class="label">Bedrooms</div>'
        '<div class="value">7</div></div>'))
    assert pairs["Bedrooms"] == "7", pairs


def test_amenity_list_is_not_read_as_label_value_pairs():
    """Regression: 'Private Pool' is an amenity, not a label for the next item."""
    html = ('<ul class="amenities"><li>Private Pool</li><li>Spa / Hot Tub</li>'
            '<li>Games Room</li><li>Free WiFi</li></ul>')
    pairs = extract_kv_pairs(soup(html))
    assert "Private Pool" not in pairs, pairs
    amenities = extract_amenities(soup(html))
    assert amenities[:2] == ["Private Pool", "Spa / Hot Tub"], amenities


def test_inline_label_colon_value():
    pairs = extract_kv_pairs(soup("<p>Minimum Stay: 5 nights</p>"))
    assert pairs["Minimum Stay"] == "5 nights", pairs


def test_json_booleans_render_as_yes_no():
    """Regression: petsAllowed:false must not land in the sheet as 'False'."""
    html = ('<html><body><script>window.__INITIAL_STATE__ = '
            '{"unit":{"petsAllowed":false,"bedrooms":3}};</script></body></html>')
    record = build_record("1", extract_layers(html))
    assert record["pets"] == "No", record["pets"]
    assert record["bedrooms"] == 3


def test_text_heuristics_fill_gaps():
    html = ("<html><body><main><h1>Home 4410</h1>"
            "<p>Sleeps 14 guests. Check-in is at 4:00 PM and check-out at 10:00 AM. "
            "Minimum stay 3 nights. Private pool and hot tub.</p>"
            "</main></body></html>")
    record = build_record("999", extract_layers(html))
    assert record["sleeps"] == 14, record["sleeps"]
    assert "4:00 PM" in str(record["check_in_time"])
    assert "10:00 AM" in str(record["check_out_time"])
    assert record["min_nights"] == 3
    assert record["pool"] == "Private pool"
    assert record["spa"] == "Yes"


def test_structured_data_beats_text_guess():
    """JSON-LD is trusted over a number scraped out of prose."""
    html = """<html><head><script type="application/ld+json">
    {"@type":"Accommodation","numberOfBedrooms":6}</script></head>
    <body><main><p>Near our 2 bedroom condos.</p></main></body></html>"""
    record = build_record("1", extract_layers(html))
    assert record["bedrooms"] == 6, record["bedrooms"]
    assert record.sources["bedrooms"] == "jsonld"


def test_quote_page_fees_and_confirmation():
    home = "<html><body><main><h1>Home 1906</h1></main></body></html>"
    quote = ("<html><body><main><table>"
             "<tr><td>Confirmation Number</td><td>ABC-123</td></tr>"
             "<tr><td>Total</td><td>$1,713.40</td></tr>"
             "<tr><td>Cleaning Fee</td><td>$185.00</td></tr>"
             "</table></main></body></html>")
    record = build_record("1906", extract_layers(home), extract_layers(quote))
    assert record["confirmation_number"] == "ABC-123"
    assert record["quote_total"] == "$1,713.40"
    assert ("Total", "$1,713.40") in record.fees


def test_unmapped_fields_are_kept_as_extras():
    record = build_record("1", extract_layers(
        "<table><tr><th>Lakeview Dock Access</th><td>Yes</td></tr></table>"))
    assert record.extras.get("Lakeview Dock Access") == "Yes", record.extras


def test_images_skip_logos_and_resolve_relative_urls():
    layers = extract_layers(
        '<img src="/logo.png"><img src="/img/a.jpg"><img src="data:image/gif;base64,x">',
        "https://example.com/home/1")
    assert layers["images"] == ["https://example.com/img/a.jpg"], layers["images"]


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
