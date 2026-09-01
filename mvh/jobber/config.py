"""Jobber API settings.

Nothing secret is stored here. The client id and secret arrive from the
environment; the refresh token lives in a token file kept outside the working
tree (see ``token_file``). If a credential ever appears in this repository,
that is a bug.

Every URL is overridable because Jobber's OAuth paths and the set of valid
``X-JOBBER-GRAPHQL-VERSION`` values change independently of this code. Run
``python -m mvh jobber probe`` after any change: it reports exactly which of
these the server rejected.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

# The API is a single GraphQL endpoint. secure.getjobber.com is the human web
# app and is not usable from here -- it has no API surface and no API keys.
DEFAULT_API_URL = "https://api.getjobber.com/api/graphql"
DEFAULT_AUTHORIZE_URL = "https://api.getjobber.com/api/oauth/authorize"
DEFAULT_TOKEN_URL = "https://api.getjobber.com/api/oauth/token"

# Jobber pins behaviour to a dated schema version sent on every request. If
# this one has been retired the server says so and lists the live ones; put a
# valid date in JOBBER_API_VERSION rather than editing this file.
DEFAULT_API_VERSION = "2025-01-20"

# Must match the redirect URI registered on the app in the Developer Center,
# character for character, or Jobber refuses the authorization request.
DEFAULT_REDIRECT_URI = "http://localhost:8123/oauth/callback"

# Jobber prices each query against a leaky bucket (maximumAvailable 10000,
# restoreRate ~500/sec) and reports the state on every response. Staying a
# comfortable distance above empty costs a little throughput and avoids the
# throttled-retry cycle entirely.
DEFAULT_PAGE_SIZE = 50
DEFAULT_COST_FLOOR = 2000

# Separate from query cost, Jobber caps an app at 2500 requests per 5 minutes
# per account. 0.3s between requests is 1000 per 5 minutes -- well under it.
DEFAULT_DELAY = 0.3


def _default_token_file() -> Path:
    """Outside the repo on purpose: a token under data/ is one `rm -rf` from
    gone, and one stray `git add -f` from being published."""
    return Path(
        os.environ.get("JOBBER_TOKEN_FILE")
        or Path.home() / ".config" / "mvh" / "jobber_token.json"
    )


@dataclass
class JobberSettings:
    client_id: str = ""
    client_secret: str = ""
    api_url: str = DEFAULT_API_URL
    authorize_url: str = DEFAULT_AUTHORIZE_URL
    token_url: str = DEFAULT_TOKEN_URL
    api_version: str = DEFAULT_API_VERSION
    redirect_uri: str = DEFAULT_REDIRECT_URI
    token_file: Path = None            # type: ignore[assignment]
    page_size: int = DEFAULT_PAGE_SIZE
    cost_floor: int = DEFAULT_COST_FLOOR
    delay: float = DEFAULT_DELAY
    timeout: float = 60.0
    max_retries: int = 5
    user_agent: str = "MasterVacationHomes-JobberExport/1.0"

    @classmethod
    def from_env(cls, **overrides) -> "JobberSettings":
        settings = cls(
            client_id=os.environ.get("JOBBER_CLIENT_ID", ""),
            client_secret=os.environ.get("JOBBER_CLIENT_SECRET", ""),
            api_url=os.environ.get("JOBBER_API_URL", DEFAULT_API_URL),
            authorize_url=os.environ.get("JOBBER_AUTHORIZE_URL", DEFAULT_AUTHORIZE_URL),
            token_url=os.environ.get("JOBBER_TOKEN_URL", DEFAULT_TOKEN_URL),
            api_version=os.environ.get("JOBBER_API_VERSION", DEFAULT_API_VERSION),
            redirect_uri=os.environ.get("JOBBER_REDIRECT_URI", DEFAULT_REDIRECT_URI),
            token_file=_default_token_file(),
        )
        for key, value in overrides.items():
            if value is not None:
                setattr(settings, key, value)
        return settings

    def missing_credentials(self) -> list[str]:
        return [name for name, value in
                (("JOBBER_CLIENT_ID", self.client_id),
                 ("JOBBER_CLIENT_SECRET", self.client_secret)) if not value]
