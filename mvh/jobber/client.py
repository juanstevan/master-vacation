"""GraphQL transport for Jobber: cost-aware, self-refreshing, resumable.

Jobber does not rate limit on rows or requests the way a REST API does. It
prices every query and deducts the cost from a leaky bucket
(``maximumAvailable`` 10000, refilling at ``restoreRate`` a second), then
reports the remaining balance on the response. Two things follow:

* An unbounded connection is the expensive mistake, not a large account. Ask
  for ``jobs`` with no ``first:`` and Jobber prices it as if 100 nodes came
  back at every level of nesting -- which is what actually gets a "give me all
  the jobs" query rejected. Every connection this client sends is bounded.
* The balance is on every response, so there is no need to guess. This client
  reads it and waits for the bucket to refill *before* it runs dry, which is
  cheaper than being throttled and retrying.

Separately Jobber caps an app at 2500 requests per 5 minutes per account and
answers 429 past that; the inter-request delay keeps us well underneath.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Iterator, Optional

import requests

from .auth import JobberAuth, JobberAuthError
from .config import JobberSettings

log = logging.getLogger("mvh.jobber.client")

RETRY_STATUS = {408, 425, 500, 502, 503, 504}


class JobberError(RuntimeError):
    pass


class JobberQueryError(JobberError):
    """The server understood the request and rejected it -- usually a field
    that does not exist in this API version. Retrying will not help."""


class JobberClient:
    def __init__(self, settings: JobberSettings, auth: JobberAuth,
                 session: Optional[requests.Session] = None):
        self.s = settings
        self.auth = auth
        self.session = session or requests.Session()
        self.session.headers.update({
            "User-Agent": settings.user_agent,
            "Content-Type": "application/json",
            "Accept": "application/json",
        })
        self.last_throttle: dict = {}
        self.requests_made = 0
        self._last_request = 0.0
        self._schema_cache: dict[str, dict] = {}

    # ------------------------------------------------------------- throttling
    def _pace(self) -> None:
        wait = self.s.delay - (time.monotonic() - self._last_request)
        if wait > 0:
            time.sleep(wait)
        self._last_request = time.monotonic()

    def _respect_budget(self, throttle: dict) -> None:
        """Wait for the bucket to refill before it runs out, not after."""
        if not throttle:
            return
        self.last_throttle = throttle
        available = throttle.get("currentlyAvailable")
        restore = throttle.get("restoreRate") or 1
        if available is None or available >= self.s.cost_floor:
            return
        need = self.s.cost_floor - available
        pause = min(need / max(float(restore), 1.0), 60.0)
        log.info("query budget down to %s points; pausing %.1fs to refill",
                 available, pause)
        time.sleep(pause)

    # ---------------------------------------------------------------- execute
    def execute(self, query: str, variables: Optional[dict] = None) -> dict:
        """Run one query and return its ``data``. Raises on anything else."""
        payload = json.dumps({"query": query, "variables": variables or {}})
        refreshed = False

        for attempt in range(self.s.max_retries + 1):
            self._pace()
            headers = {
                "Authorization": f"Bearer {self.auth.access_token()}",
                "X-JOBBER-GRAPHQL-VERSION": self.s.api_version,
            }
            try:
                resp = self.session.post(self.s.api_url, data=payload,
                                         headers=headers, timeout=self.s.timeout)
            except requests.RequestException as exc:
                if attempt >= self.s.max_retries:
                    raise JobberError(f"could not reach {self.s.api_url}: {exc}")
                backoff = 2 ** attempt
                log.warning("%s; retrying in %ss", exc, backoff)
                time.sleep(backoff)
                continue
            self.requests_made += 1

            # An access token that expired mid-run is the classic backfill
            # killer: valid at minute 0, dead at minute 61. Refresh once.
            if resp.status_code in (401, 403) and not refreshed:
                log.info("HTTP %s -- refreshing the access token and retrying",
                         resp.status_code)
                refreshed = True
                try:
                    self.auth.force_refresh()
                except JobberAuthError:
                    raise
                continue

            if resp.status_code == 429:
                backoff = self._retry_after(resp, attempt)
                log.warning("HTTP 429 (request-rate cap); waiting %.0fs", backoff)
                time.sleep(backoff)
                continue

            if resp.status_code in RETRY_STATUS and attempt < self.s.max_retries:
                backoff = self._retry_after(resp, attempt)
                log.warning("HTTP %s; retrying in %.0fs", resp.status_code, backoff)
                time.sleep(backoff)
                continue

            try:
                body = resp.json()
            except ValueError:
                raise JobberError(
                    f"HTTP {resp.status_code} from {self.s.api_url} was not JSON. "
                    f"First 300 characters: {resp.text[:300]!r}")

            self._respect_budget(
                (body.get("extensions") or {}).get("cost", {}).get("throttleStatus", {}))

            errors = body.get("errors") or []
            if errors:
                if self._is_throttled(errors):
                    pause = min(2 ** attempt, 30)
                    log.warning("throttled by the API; waiting %ss", pause)
                    time.sleep(pause)
                    continue
                raise JobberQueryError(self._explain(errors, resp.status_code))

            if body.get("data") is None:
                raise JobberError(f"no data in response: {str(body)[:300]}")
            return body["data"]

        raise JobberError(
            f"gave up after {self.s.max_retries + 1} attempts against {self.s.api_url}")

    @staticmethod
    def _retry_after(resp, attempt: int) -> float:
        try:
            return min(float(resp.headers.get("Retry-After", "")), 120.0)
        except (TypeError, ValueError):
            return float(min(2 ** attempt, 60))

    @staticmethod
    def _is_throttled(errors: list) -> bool:
        for error in errors:
            code = str((error.get("extensions") or {}).get("code", "")).upper()
            if "THROTTLE" in code or "throttle" in str(error.get("message", "")).lower():
                return True
        return False

    def _explain(self, errors: list, status: int) -> str:
        """Turn a GraphQL error list into something actionable."""
        messages = [str(e.get("message", e)) for e in errors]
        joined = "; ".join(messages[:5])
        blob = joined.lower()
        hint = ""
        if "version" in blob and ("header" in blob or "invalid" in blob or
                                  "retired" in blob or "supported" in blob):
            hint = (f"\n\nThe X-JOBBER-GRAPHQL-VERSION header ("
                    f"{self.s.api_version}) is not a version this account "
                    f"accepts. The message above lists the valid ones -- set "
                    f"JOBBER_API_VERSION to one of them.")
        elif "scope" in blob or "permission" in blob or "unauthorized" in blob:
            hint = ("\n\nThe app is authenticated but not permitted to read "
                    "this. Add the missing read scope to the app in the "
                    "Developer Center, then re-run `python -m mvh jobber login` "
                    "-- a scope change requires fresh consent.")
        elif "doesn't exist" in blob or "does not exist" in blob or "undefined field" in blob:
            hint = ("\n\nA requested field is not in this schema version. Run "
                    "`python -m mvh jobber schema` to list the fields this "
                    "account actually exposes.")
        return f"GraphQL error (HTTP {status}): {joined}{hint}"

    # -------------------------------------------------------------- paginate
    def paginate(self, query: str, variables: dict, connection: str,
                 start_cursor: Optional[str] = None,
                 max_pages: Optional[int] = None) -> Iterator[tuple[list, dict]]:
        """Walk a Relay connection, yielding (nodes, pageInfo) one page at a time.

        Yielding per page rather than accumulating is deliberate: the caller
        writes each page to disk as it arrives, so a run interrupted at page
        400 resumes at 401 instead of starting over.
        """
        cursor, page = start_cursor, 0
        while True:
            data = self.execute(query, {**variables, "after": cursor})
            block = data
            for part in connection.split("."):
                block = (block or {}).get(part) or {}
            nodes = block.get("nodes") or []
            info = block.get("pageInfo") or {}
            page += 1
            yield nodes, info
            cursor = info.get("endCursor")
            if not info.get("hasNextPage") or not cursor:
                return
            if max_pages and page >= max_pages:
                log.info("stopping at the --max-pages limit of %d", max_pages)
                return

    # ----------------------------------------------------------- introspection
    def type_fields(self, type_name: str) -> dict[str, dict]:
        """Field name -> {kind, type_name, args} for one type, from the live
        schema. Used to build queries out of fields this account really has,
        instead of guessing and getting the whole query rejected."""
        if type_name in self._schema_cache:
            return self._schema_cache[type_name]
        query = """
        query TypeFields($name: String!) {
          __type(name: $name) {
            name
            kind
            fields {
              name
              args { name }
              type { ...Ref }
            }
            possibleTypes { name }
          }
        }
        fragment Ref on __Type {
          kind name
          ofType { kind name
            ofType { kind name
              ofType { kind name } } }
        }
        """
        data = self.execute(query, {"name": type_name})
        node = data.get("__type") or {}
        fields: dict[str, dict] = {}
        for entry in node.get("fields") or []:
            kind, name = _unwrap(entry.get("type") or {})
            fields[entry["name"]] = {
                "kind": kind,
                "type_name": name,
                "args": {a["name"] for a in entry.get("args") or []},
            }
        self._schema_cache[type_name] = fields
        return fields

    def possible_types(self, type_name: str) -> list[str]:
        key = f"__possible__{type_name}"
        if key in self._schema_cache:
            return list(self._schema_cache[key])           # type: ignore[arg-type]
        data = self.execute(
            "query T($name: String!){ __type(name:$name){ possibleTypes { name } } }",
            {"name": type_name})
        node = data.get("__type") or {}
        names = [t["name"] for t in node.get("possibleTypes") or []]
        self._schema_cache[key] = names                    # type: ignore[assignment]
        return names

    def query_args(self, field_name: str, root: str = "Query") -> set:
        """Which arguments the root query accepts -- answers 'can we filter
        server-side?' without reading the docs."""
        return self.type_fields(root).get(field_name, {}).get("args", set())


def _unwrap(type_ref: dict) -> tuple[str, str]:
    """Strip NON_NULL / LIST wrappers down to the named type."""
    kind, name = type_ref.get("kind"), type_ref.get("name")
    while name is None and type_ref.get("ofType"):
        type_ref = type_ref["ofType"]
        kind, name = type_ref.get("kind"), type_ref.get("name")
    return kind or "", name or ""
