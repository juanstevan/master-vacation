"""OAuth 2.0 for Jobber: one browser consent, then a refresh token forever.

Jobber has no API keys, and no password grant -- a Jobber username and
password cannot authenticate an API call at all. The only way in is the
authorization code grant: an admin of the Jobber account consents once in a
browser, and the app keeps the refresh token that consent produces.

Two facts drive the design here:

* Access tokens last 60 minutes. A full history backfill takes longer than
  that, so the token has to be refreshed mid-run rather than once at startup.
  This is the usual reason a backfill dies two thirds of the way through.
* Jobber may rotate the refresh token on every refresh. A rotated token that
  is not written back to disk leaves the next run unable to authenticate, so
  the store is saved whenever the server hands us a new one.
"""
from __future__ import annotations

import json
import logging
import os
import secrets
import time
import urllib.parse
import webbrowser
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Optional

import requests

from .config import JobberSettings

log = logging.getLogger("mvh.jobber.auth")

# Refresh this long before the token actually dies, so a request never leaves
# with a token that expires in flight.
EXPIRY_MARGIN = 300.0


class JobberAuthError(RuntimeError):
    pass


@dataclass
class Token:
    access_token: str = ""
    refresh_token: str = ""
    expires_at: float = 0.0
    scope: str = ""
    account_name: str = ""

    @property
    def expired(self) -> bool:
        return time.time() >= self.expires_at - EXPIRY_MARGIN

    def to_dict(self) -> dict:
        return {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "expires_at": self.expires_at,
            "scope": self.scope,
            "account_name": self.account_name,
        }


class TokenStore:
    """The refresh token on disk, readable only by its owner."""

    def __init__(self, path: Path):
        self.path = Path(path)

    @staticmethod
    def env_refresh_token() -> str:
        return os.environ.get("JOBBER_REFRESH_TOKEN", "").strip()

    def load(self) -> Token:
        """The stored token wins over JOBBER_REFRESH_TOKEN.

        The env var seeds a machine that has no browser to consent with, but it
        is a *starting* value: once rotation is on, the refresh token changes
        every time it is used, so a static env var is stale the moment the
        first refresh happens. Whatever was written back is the live one.
        """
        stored = self._read()
        if stored.refresh_token:
            return stored
        env_refresh = self.env_refresh_token()
        return Token(refresh_token=env_refresh) if env_refresh else Token()

    def _read(self) -> Token:
        if not self.path.exists():
            return Token()
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (ValueError, OSError) as exc:
            log.warning("could not read %s (%s); re-authorization needed",
                        self.path, exc)
            return Token()
        return Token(
            access_token=raw.get("access_token", ""),
            refresh_token=raw.get("refresh_token", ""),
            expires_at=float(raw.get("expires_at") or 0),
            scope=raw.get("scope", ""),
            account_name=raw.get("account_name", ""),
        )

    def save(self, token: Token) -> None:
        """Always persist. A rotated refresh token that is not written down
        leaves the next run unable to authenticate at all."""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.chmod(self.path.parent, 0o700)
            except OSError:
                pass
            # Create with 0600 from the start rather than writing it
            # world-readable and tightening afterwards.
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(token.to_dict(), handle, indent=2)
        except OSError as exc:
            log.warning(
                "could not write %s (%s). If this app has refresh token "
                "rotation enabled, the next run will need re-authorization -- "
                "point JOBBER_TOKEN_FILE somewhere writable.", self.path, exc)
            return
        log.debug("saved token to %s", self.path)


class _CallbackHandler(BaseHTTPRequestHandler):
    """Catches the one redirect Jobber makes back to localhost."""

    result: dict = {}

    def do_GET(self) -> None:                              # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        _CallbackHandler.result = {k: v[0] for k, v in params.items()}
        ok = "code" in _CallbackHandler.result
        heading = "Authorized." if ok else "Authorization failed."
        detail = (
            "You can close this tab and return to the terminal." if ok
            else _CallbackHandler.result.get("error_description", "No code returned.")
        )
        body = (
            "<html><body style=\"font-family:system-ui;padding:3rem\">"
            f"<h2>{heading}</h2><p>{detail}</p></body></html>"
        ).encode("utf-8")
        self.send_response(200 if ok else 400)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:                  # keep the console clean
        pass


@dataclass
class JobberAuth:
    settings: JobberSettings
    store: TokenStore = field(default=None)                # type: ignore[assignment]
    session: requests.Session = field(default=None)        # type: ignore[assignment]
    token: Token = field(default=None)                     # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.store is None:
            self.store = TokenStore(self.settings.token_file)
        if self.session is None:
            self.session = requests.Session()
            self.session.headers["User-Agent"] = self.settings.user_agent
        if self.token is None:
            self.token = self.store.load()

    # ------------------------------------------------------------- authorize
    def authorization_url(self, state: str) -> str:
        query = urllib.parse.urlencode({
            "client_id": self.settings.client_id,
            "redirect_uri": self.settings.redirect_uri,
            "response_type": "code",
            "state": state,
        })
        return f"{self.settings.authorize_url}?{query}"

    def login(self, open_browser: bool = True, timeout: float = 300.0) -> Token:
        """Run the consent flow, catching the redirect on localhost."""
        self._require_client_credentials()
        state = secrets.token_urlsafe(24)
        url = self.authorization_url(state)

        parsed = urllib.parse.urlparse(self.settings.redirect_uri)
        server = HTTPServer((parsed.hostname or "localhost", parsed.port or 80),
                            _CallbackHandler)
        server.timeout = timeout
        _CallbackHandler.result = {}

        print("\nOpen this URL as a Jobber ADMIN of the account you want to read:\n")
        print(f"  {url}\n")
        if open_browser:
            try:
                webbrowser.open(url)
            except Exception:                    # headless box: the URL above is enough
                pass
        print(f"Waiting for the redirect to {self.settings.redirect_uri} ...")
        server.handle_request()
        server.server_close()

        result = _CallbackHandler.result
        if not result:
            raise JobberAuthError(
                "No redirect arrived within the timeout. If this machine has no "
                "browser, authorize on your laptop instead and pass the code:\n"
                "  python -m mvh jobber login --code <code from the redirect URL>"
            )
        if result.get("state") != state:
            raise JobberAuthError(
                "state parameter did not match -- discarding this response "
                "rather than trusting a redirect we did not initiate."
            )
        if "code" not in result:
            raise JobberAuthError(
                f"Jobber returned {result.get('error', 'no code')}: "
                f"{result.get('error_description', '')}"
            )
        return self.exchange_code(result["code"])

    def exchange_code(self, code: str) -> Token:
        self._require_client_credentials()
        payload = {
            "client_id": self.settings.client_id,
            "client_secret": self.settings.client_secret,
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self.settings.redirect_uri,
        }
        return self._token_request(payload, "authorization_code")

    # --------------------------------------------------------------- refresh
    def refresh(self) -> Token:
        self._require_client_credentials()
        if not self.token.refresh_token:
            raise JobberAuthError(
                "No refresh token. Run `python -m mvh jobber login` once as a "
                "Jobber admin, or set JOBBER_REFRESH_TOKEN."
            )
        def attempt(refresh_token: str) -> Token:
            return self._token_request({
                "client_id": self.settings.client_id,
                "client_secret": self.settings.client_secret,
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            }, "refresh_token")

        try:
            return attempt(self.token.refresh_token)
        except JobberAuthError:
            env_token = self.store.env_refresh_token()
            if not env_token or env_token == self.token.refresh_token:
                raise
            # The stored token is dead but the environment holds a different
            # one -- someone re-consented and exported the new value.
            log.info("stored refresh token rejected; trying JOBBER_REFRESH_TOKEN")
            self.token.refresh_token = env_token
            return attempt(env_token)

    def access_token(self) -> str:
        """A token good for the next few minutes, refreshing if needed."""
        if not self.token.access_token or self.token.expired:
            self.refresh()
        return self.token.access_token

    def force_refresh(self) -> str:
        """Refresh even if the cached token still looks valid -- used when the
        API answers 401 to a token we believed was live."""
        self.refresh()
        return self.token.access_token

    # ---------------------------------------------------------------- shared
    def _require_client_credentials(self) -> None:
        missing = self.settings.missing_credentials()
        if missing:
            raise JobberAuthError(
                "Missing " + " and ".join(missing) + ". Create an app at "
                "developer.getjobber.com, then export the values:\n"
                "  export JOBBER_CLIENT_ID=...\n  export JOBBER_CLIENT_SECRET=...\n"
                "Never commit them."
            )

    def _token_request(self, payload: dict, kind: str) -> Token:
        try:
            resp = self.session.post(self.settings.token_url, data=payload,
                                     timeout=self.settings.timeout)
        except requests.RequestException as exc:
            raise JobberAuthError(f"could not reach {self.settings.token_url}: {exc}")

        if resp.status_code >= 400:
            detail = resp.text[:400]
            hint = ""
            if kind == "refresh_token" and resp.status_code in (400, 401):
                hint = ("\nThe refresh token is no longer valid. That happens when "
                        "the app is disconnected in Jobber, the client secret is "
                        "rolled, or scopes change. Run `python -m mvh jobber login` "
                        "again.")
            raise JobberAuthError(
                f"{kind} request failed: HTTP {resp.status_code} {detail}{hint}")

        try:
            body = resp.json()
        except ValueError:
            raise JobberAuthError(
                f"{kind} response was not JSON: {resp.text[:200]}")

        access = body.get("access_token")
        if not access:
            raise JobberAuthError(f"{kind} response carried no access_token: {body}")

        self.token = Token(
            access_token=access,
            # Rotation is on for some apps: keep the new one, fall back to the
            # old only when the server did not send a replacement.
            refresh_token=body.get("refresh_token") or self.token.refresh_token,
            expires_at=time.time() + float(body.get("expires_in") or 3600),
            scope=body.get("scope", self.token.scope),
            account_name=self.token.account_name,
        )
        self.store.save(self.token)
        log.info("%s ok; access token valid for %.0f minutes", kind,
                 (self.token.expires_at - time.time()) / 60)
        return self.token
