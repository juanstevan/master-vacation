"""Site-specific settings. Everything here can be overridden from the CLI."""
from __future__ import annotations

import os
from dataclasses import dataclass, field

DEFAULT_BASE_URL = os.environ.get("MVH_BASE_URL", "https://mastervacationhomes.com")

# The public listing page and the quote/confirmation-letter page for one home.
HOME_PATH = "/home/{id}"
QUOTE_PATH = "/home/{id}/quote"

# Paths tried when discovering the full set of homes. The first ones that
# actually return listing links win; the rest are reported as misses so you can
# see what the site really uses.
SITEMAP_PATHS = [
    "/sitemap.xml",
    "/sitemap_index.xml",
    "/sitemap-index.xml",
    "/wp-sitemap.xml",
    "/sitemap/sitemap-index.xml",
]

INDEX_PATHS = [
    "/",
    "/homes",
    "/home",
    "/search",
    "/vacation-rentals",
    "/vacation-homes",
    "/rentals",
    "/properties",
    "/all-homes",
    "/our-homes",
]

# Query-string pagination patterns tried against whichever index path works.
PAGE_PARAMS = ["page", "p", "pg", "offset"]


@dataclass
class Settings:
    base_url: str = DEFAULT_BASE_URL
    user_agent: str = (
        "MasterVacationHomes-ListingExport/1.0 "
        "(internal content export for customer-service knowledge base)"
    )
    delay: float = 1.0            # seconds between requests
    jitter: float = 0.35          # random extra delay, 0..jitter
    timeout: float = 30.0
    max_retries: int = 4
    respect_robots: bool = True
    extra_headers: dict = field(default_factory=dict)

    def home_url(self, home_id: str) -> str:
        return self.base_url.rstrip("/") + HOME_PATH.format(id=home_id)

    def quote_url(self, home_id: str) -> str:
        return self.base_url.rstrip("/") + QUOTE_PATH.format(id=home_id)
