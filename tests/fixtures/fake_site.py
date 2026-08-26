"""A throwaway local site used to test the pipeline end to end.

Everything here is INVENTED TEST DATA -- fake homes, fake addresses, fake
prices. It exists only so the extractor can be exercised without network
access. It deliberately renders three homes in three different markup styles,
because the real site will use exactly one of them and we do not know which.
"""
from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse

HOMES = {
    # style A: schema.org JSON-LD + Open Graph + spec table
    "390765": dict(style="jsonld", number="1906", name="Sample Home 1906",
                   beds=6, baths=4.5, sleeps=12, community="Test Ridge Resort"),
    # style B: single-page-app state blob
    "390766": dict(style="state", number="2014", name="Sample Home 2014",
                   beds=4, baths=3, sleeps=8, community="Placeholder Palms"),
    # style C: plain server-rendered HTML, no structured data at all
    "390767": dict(style="plain", number="3302", name="Sample Home 3302",
                   beds=8, baths=5, sleeps=16, community="Example Lakes"),
}

AMENITIES = ["Private Pool", "Spa / Hot Tub", "Games Room", "Free WiFi",
             "Washer & Dryer", "BBQ Grill", "Screened Lanai"]


def page_jsonld(hid, h):
    ld = {
        "@context": "https://schema.org", "@type": "Accommodation",
        "name": h["name"], "identifier": h["number"],
        "numberOfBedrooms": h["beds"], "numberOfBathroomsTotal": h["baths"],
        "occupancy": {"@type": "QuantitativeValue", "value": h["sleeps"]},
        "description": f"A sample {h['beds']} bedroom test home in {h['community']}.",
        "address": {"@type": "PostalAddress", "streetAddress": "100 Example Way",
                    "addressLocality": "Testville", "addressRegion": "FL",
                    "postalCode": "34747"},
        "latitude": 28.3, "longitude": -81.6,
        "amenityFeature": [{"@type": "LocationFeatureSpecification", "name": a}
                           for a in AMENITIES],
    }
    rows = "".join(
        f"<tr><th>{k}</th><td>{v}</td></tr>" for k, v in [
            ("Home Number", h["number"]), ("Bedrooms", h["beds"]),
            ("Bathrooms", h["baths"]), ("Sleeps", h["sleeps"]),
            ("Community", h["community"]), ("Property Type", "Vacation Home"),
            ("Check In", "4:00 PM"), ("Check Out", "10:00 AM"),
            ("Minimum Stay", "3 nights"), ("Pets", "No pets"),
            ("Square Feet", "3,100"),
        ])
    return f"""<html><head><title>{h['name']}</title>
<meta property="og:title" content="{h['name']} - Sample Listing">
<meta property="og:description" content="Sample {h['beds']} bedroom home.">
<meta property="og:image" content="/img/{hid}-1.jpg">
<script type="application/ld+json">{json.dumps(ld)}</script></head>
<body><main><h1>{h['name']}</h1>
<table class="specs">{rows}</table>
<h3>Amenities</h3><ul>{''.join(f'<li>{a}</li>' for a in AMENITIES)}</ul>
<p>This sample home sleeps {h['sleeps']} guests and has a private pool.</p>
<img src="/img/{hid}-1.jpg"><img src="/img/{hid}-2.jpg">
</main></body></html>"""


def page_state(hid, h):
    state = {"unit": {
        "unitId": int(hid), "unitNumber": h["number"], "unitName": h["name"],
        "bedrooms": h["beds"], "bathrooms": h["baths"], "maxOccupancy": h["sleeps"],
        "community": h["community"], "propertyType": "Townhome",
        "checkInTime": "4:00 PM", "checkOutTime": "10:00 AM", "minNights": 4,
        "petsAllowed": False, "squareFeet": 1850,
        "city": "Testville", "state": "FL", "zipCode": "34747",
        "description": f"Sample townhome in {h['community']}. Test data only.",
        "amenities": AMENITIES,
    }}
    return f"""<html><head><title>{h['name']}</title></head><body><main>
<h1>{h['name']}</h1><div id="app"></div>
<script>window.__INITIAL_STATE__ = {json.dumps(state)};</script>
<p>Sleeps {h['sleeps']}. Community pool and spa on site.</p>
<img src="/img/{hid}-1.jpg"></main></body></html>"""


def page_plain(hid, h):
    details = "".join(
        f'<div class="detail"><div class="label">{k}</div>'
        f'<div class="value">{v}</div></div>' for k, v in [
            ("Home Number", h["number"]), ("Bedrooms", h["beds"]),
            ("Bathrooms", h["baths"]), ("Sleeps", h["sleeps"]),
            ("Community", h["community"]), ("Check In", "4:00 PM"),
            ("Check Out", "10:00 AM"), ("Pets", "Pet friendly"),
        ])
    return f"""<html><head><title>{h['name']}</title></head><body><main>
<h1>{h['name']}</h1><div class="details">{details}</div>
<div class="amenities"><h4>Features</h4>
<ul>{''.join(f'<li>{a}</li>' for a in AMENITIES)}</ul></div>
<p>Minimum stay 5 nights. Non-smoking. Private pool with pool heat available.</p>
<img src="/img/{hid}-1.jpg"></main></body></html>"""


def page_quote(hid, h):
    rows = "".join(f"<tr><td>{k}</td><td>{v}</td></tr>" for k, v in [
        ("Confirmation Number", f"TEST-{hid}"), ("Rate", "$245.00"),
        ("Cleaning Fee", "$185.00"), ("Taxes", "$98.40"),
        ("Total", "$1,713.40"), ("Balance Due", "$856.70"),
        ("Cancellation Policy", "Sample policy: free cancellation 30 days out."),
    ])
    return f"""<html><head><title>Quote - {h['name']}</title></head><body><main>
<h1>Confirmation Letter</h1><h2>{h['name']}</h2>
<table>{rows}</table>
<p>Check-in is at 4:00 PM and check-out is at 10:00 AM.
This is sample confirmation text for testing only.</p>
</main></body></html>"""


BUILDERS = {"jsonld": page_jsonld, "state": page_state, "plain": page_plain}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # keep the test output quiet
        pass

    def _send(self, body: str, ctype="text/html"):
        data = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", f"{ctype}; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = urlparse(self.path).path

        if path == "/robots.txt":
            return self._send("User-agent: *\nAllow: /\n", "text/plain")

        if path == "/sitemap.xml":
            locs = "".join(
                f"<url><loc>http://{self.headers['Host']}/home/{i}</loc></url>"
                for i in HOMES)
            return self._send(
                f'<?xml version="1.0"?><urlset>{locs}</urlset>', "application/xml")

        if path == "/":
            links = "".join(f'<a href="/home/{i}">Home {h["number"]}</a>'
                            for i, h in HOMES.items())
            return self._send(f"<html><body><h1>Our Homes</h1>{links}</body></html>")

        parts = [p for p in path.split("/") if p]
        if len(parts) >= 2 and parts[0] == "home" and parts[1] in HOMES:
            hid = parts[1]
            home = HOMES[hid]
            if len(parts) == 3 and parts[2] == "quote":
                return self._send(page_quote(hid, home))
            return self._send(BUILDERS[home["style"]](hid, home))

        self.send_error(404)


def serve(port=8899):
    HTTPServer(("127.0.0.1", port), Handler).serve_forever()


if __name__ == "__main__":
    import sys
    serve(int(sys.argv[1]) if len(sys.argv) > 1 else 8899)
