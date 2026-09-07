"""Access tokens from the Supabase function instead of from a local grant.

When JOBBER_TOKEN_ENDPOINT is set, this machine holds no Jobber credential at
all -- no client secret, no refresh token. It presents a shared key to the Edge
Function and gets back an access token good for the next hour. The consent
flow, the client secret and the refresh token all stay in Supabase.

That is the better arrangement whenever the person who can consent is not the
person running the pull: the Jobber admin visits one URL once, and nothing
secret is ever copied onto a laptop or into this repository.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Optional

import requests

from .auth import EXPIRY_MARGIN, JobberAuth, JobberAuthError, Token
from .config import JobberSettings

log = logging.getLogger("mvh.jobber.remote")


class RemoteAuth:
    """Same surface as JobberAuth, so JobberClient cannot tell the difference."""

    def __init__(self, settings: JobberSettings,
                 session: Optional[requests.Session] = None):
        self.settings = settings
        self.session = session or requests.Session()
        self.session.headers["User-Agent"] = settings.user_agent
        self.token = Token()

    # ------------------------------------------------------------- interface
    def access_token(self) -> str:
        if not self.token.access_token or self.token.expired:
            self._fetch(force=False)
        return self.token.access_token

    def force_refresh(self) -> str:
        """Jobber rejected the token we had, so ask for a genuinely new one
        rather than the endpoint's cached copy."""
        self._fetch(force=True)
        return self.token.access_token

    # ---------------------------------------------------------------- detail
    def _fetch(self, force: bool) -> None:
        endpoint = self.settings.token_endpoint
        if not self.settings.token_endpoint_key:
            raise JobberAuthError(
                "JOBBER_TOKEN_ENDPOINT is set but JOBBER_TOKEN_KEY is not. The "
                "endpoint is public, so it will refuse a request without the key.")

        url = endpoint + ("?force=true" if force else "")
        try:
            resp = self.session.post(
                url, timeout=self.settings.timeout,
                headers={"x-mvh-key": self.settings.token_endpoint_key},
            )
        except requests.RequestException as exc:
            raise JobberAuthError(f"could not reach {endpoint}: {exc}")

        if resp.status_code == 401:
            raise JobberAuthError(
                f"{endpoint} rejected the key. Check JOBBER_TOKEN_KEY matches "
                "the MVH_TOKEN_KEY secret set on the function.")
        if resp.status_code == 404:
            raise JobberAuthError(
                f"{endpoint} returned 404. The URL should end in "
                "/functions/v1/jobber-auth/token.")
        if resp.status_code >= 400:
            raise JobberAuthError(
                f"{endpoint} returned HTTP {resp.status_code}: {resp.text[:300]}")

        try:
            body = resp.json()
        except ValueError:
            raise JobberAuthError(
                f"{endpoint} did not return JSON: {resp.text[:200]}")

        access = body.get("access_token")
        if not access:
            raise JobberAuthError(
                f"{endpoint} returned no access token: {str(body)[:300]}")

        self.token = Token(
            access_token=access,
            refresh_token="",            # deliberately never sent to this machine
            expires_at=_expiry_seconds(body.get("expires_at")),
            scope=body.get("scope") or "",
        )
        log.debug("got an access token from %s (refreshed=%s)",
                  endpoint, body.get("refreshed"))


def _expiry_seconds(value) -> float:
    """The endpoint reports an ISO timestamp; fall back to a conservative
    lifetime if it sends something unexpected."""
    if isinstance(value, str) and value:
        text = value.strip().replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return time.time() + EXPIRY_MARGIN + 60
        if not parsed.tzinfo:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    return time.time() + EXPIRY_MARGIN + 60


def build_auth(settings: JobberSettings):
    """Remote when an endpoint is configured, local OAuth otherwise."""
    if settings.token_endpoint:
        return RemoteAuth(settings)
    return JobberAuth(settings)
