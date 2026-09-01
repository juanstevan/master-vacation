"""A throwaway local stand-in for Jobber's OAuth + GraphQL API.

Everything here is INVENTED TEST DATA -- fake jobs, fake addresses, fake
people. It exists so the client can be exercised without credentials, and it
deliberately reproduces the four things that actually break a real backfill:

  * access tokens that expire mid-run and answer 401,
  * refresh tokens that rotate, so a client that fails to save the new one
    cannot authenticate on its next call,
  * cost throttling, both as a GraphQL THROTTLED error and as HTTP 429,
  * a schema that does not contain every field a client might ask for.

The schema served here is a small subset shaped like Jobber's, enough for
introspection to drive the query builder.
"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

NOW = datetime(2026, 9, 1, tzinfo=timezone.utc)

# The schema. `customFields` is a union, exactly as Jobber models it, so the
# query builder has to emit inline fragments to read a value.
SCHEMA = {
    "Query": {
        "jobs": {"kind": "OBJECT", "type_name": "JobConnection",
                 "args": ["first", "after", "filter", "sort"]},
        "__typename": {"kind": "SCALAR", "type_name": "String", "args": []},
    },
    "JobConnection": {
        "nodes": {"kind": "OBJECT", "type_name": "Job", "args": []},
        "totalCount": {"kind": "SCALAR", "type_name": "Int", "args": []},
        "pageInfo": {"kind": "OBJECT", "type_name": "PageInfo", "args": []},
    },
    "PageInfo": {
        "hasNextPage": {"kind": "SCALAR", "type_name": "Boolean", "args": []},
        "endCursor": {"kind": "SCALAR", "type_name": "String", "args": []},
    },
    "Job": {
        "id": {"kind": "SCALAR", "type_name": "ID", "args": []},
        "jobNumber": {"kind": "SCALAR", "type_name": "Int", "args": []},
        "title": {"kind": "SCALAR", "type_name": "String", "args": []},
        "instructions": {"kind": "SCALAR", "type_name": "String", "args": []},
        "jobStatus": {"kind": "ENUM", "type_name": "JobStatusTypeEnum", "args": []},
        "createdAt": {"kind": "SCALAR", "type_name": "ISO8601DateTime", "args": []},
        "updatedAt": {"kind": "SCALAR", "type_name": "ISO8601DateTime", "args": []},
        "startAt": {"kind": "SCALAR", "type_name": "ISO8601DateTime", "args": []},
        "completedAt": {"kind": "SCALAR", "type_name": "ISO8601DateTime", "args": []},
        "client": {"kind": "OBJECT", "type_name": "Client", "args": []},
        "property": {"kind": "OBJECT", "type_name": "Property", "args": []},
        "customFields": {"kind": "UNION", "type_name": "CustomFieldUnion", "args": []},
        "visits": {"kind": "OBJECT", "type_name": "VisitConnection",
                   "args": ["first", "after"]},
        # Deliberately absent, though the wish list asks for them:
        # jobType, endAt, total. They must be dropped, not fatal.
    },
    "Client": {
        "id": {"kind": "SCALAR", "type_name": "ID", "args": []},
        "name": {"kind": "SCALAR", "type_name": "String", "args": []},
        "isCompany": {"kind": "SCALAR", "type_name": "Boolean", "args": []},
    },
    "Property": {
        "id": {"kind": "SCALAR", "type_name": "ID", "args": []},
        "address": {"kind": "OBJECT", "type_name": "PropertyAddress", "args": []},
    },
    "PropertyAddress": {
        "street": {"kind": "SCALAR", "type_name": "String", "args": []},
        "city": {"kind": "SCALAR", "type_name": "String", "args": []},
        "province": {"kind": "SCALAR", "type_name": "String", "args": []},
        "postalCode": {"kind": "SCALAR", "type_name": "String", "args": []},
    },
    "VisitConnection": {
        "nodes": {"kind": "OBJECT", "type_name": "Visit", "args": []},
        "totalCount": {"kind": "SCALAR", "type_name": "Int", "args": []},
    },
    "Visit": {
        "id": {"kind": "SCALAR", "type_name": "ID", "args": []},
        "title": {"kind": "SCALAR", "type_name": "String", "args": []},
        "startAt": {"kind": "SCALAR", "type_name": "ISO8601DateTime", "args": []},
        "completedAt": {"kind": "SCALAR", "type_name": "ISO8601DateTime", "args": []},
        "assignedUsers": {"kind": "OBJECT", "type_name": "UserConnection",
                          "args": ["first"]},
    },
    "UserConnection": {
        "nodes": {"kind": "OBJECT", "type_name": "User", "args": []},
    },
    "User": {
        "id": {"kind": "SCALAR", "type_name": "ID", "args": []},
        "name": {"kind": "OBJECT", "type_name": "UserName", "args": []},
    },
    "UserName": {
        "full": {"kind": "SCALAR", "type_name": "String", "args": []},
    },
    "CustomFieldText": {
        "label": {"kind": "SCALAR", "type_name": "String", "args": []},
        "valueText": {"kind": "SCALAR", "type_name": "String", "args": []},
    },
    "CustomFieldNumeric": {
        "label": {"kind": "SCALAR", "type_name": "String", "args": []},
        "valueNumeric": {"kind": "SCALAR", "type_name": "Float", "args": []},
    },
}

UNIONS = {"CustomFieldUnion": ["CustomFieldText", "CustomFieldNumeric"]}

TITLES = [
    ("PH ON - Pool Heat", "GUEST REQUESTED POOL HEAT"),
    ("PH OFF", "turn pool heat off"),
    ("BBQ CLEAN after checkout", ""),
    ("Guest Report - AC not working", "Guest says AC is blowing warm"),
    ("Guest Report - WiFi not working", "No internet in the house"),
    ("Guest Report - Missing items", "Two towels missing"),
    ("Quarterly filter change", "routine"),
]


def make_jobs(count: int = 250) -> list[dict]:
    """`count` invented jobs spread backwards in time, one per 4 days, so a
    24-month window keeps roughly the first 180 and drops the rest."""
    jobs = []
    for index in range(count):
        title, instructions = TITLES[index % len(TITLES)]
        created = NOW - timedelta(days=4 * index)
        jobs.append({
            "id": f"Z2lkOi8vSm9iYmVyL0pvYi8{index:05d}",
            "jobNumber": 1000 + index,
            "title": title,
            "instructions": instructions,
            "jobStatus": "completed" if index % 3 else "active",
            "createdAt": created.isoformat().replace("+00:00", "Z"),
            "updatedAt": (created + timedelta(days=1)).isoformat().replace("+00:00", "Z"),
            "startAt": (created + timedelta(days=1)).isoformat().replace("+00:00", "Z"),
            "completedAt": (created + timedelta(days=2)).isoformat().replace("+00:00", "Z"),
            "client": {"id": f"client-{index % 7}", "name": f"Test Client {index % 7}",
                       "isCompany": False},
            "property": {"id": f"prop-{index % 11}", "address": {
                "street": f"{100 + index} Example Way", "city": "Testville",
                "province": "FL", "postalCode": "34747"}},
            "customFields": [
                {"__typename": "CustomFieldText", "label": "Problem type",
                 "valueText": title.split(" - ")[-1]},
                {"__typename": "CustomFieldNumeric", "label": "Priority",
                 "valueNumeric": float(index % 5)},
            ],
            "visits": {"totalCount": 1, "nodes": [{
                "id": f"visit-{index}", "title": title,
                "startAt": (created + timedelta(days=1)).isoformat().replace("+00:00", "Z"),
                "completedAt": (created + timedelta(days=2)).isoformat().replace("+00:00", "Z"),
                "assignedUsers": {"nodes": [
                    {"id": f"user-{index % 4}",
                     "name": {"full": f"Tech {index % 4}"}}]},
            }]},
        })
    return jobs


class FakeJobber:
    """Server state and the failure switches the tests flip."""

    def __init__(self, jobs=None, page_size_cap: int = 100):
        self.jobs = jobs if jobs is not None else make_jobs()
        self.page_size_cap = page_size_cap
        self.access_token = "access-1"
        self.refresh_token = "refresh-1"
        self.rotate_refresh = True
        self.token_calls = 0
        self.graphql_calls = 0
        self.expire_after_calls = None      # the access token dies after N calls
        self.expired_yet = False
        self.jobs_queries = 0
        self.throttle_on_call = set()       # GraphQL THROTTLED on these calls
        self.http_429_on_call = set()
        self.available_points = 10000
        self.lock = threading.Lock()


def _selected_fields(selection: str, type_name: str) -> list[str]:
    """Which of a type's fields the query text asked for. Crude but enough:
    the point is to prove the client only ever asks for fields that exist."""
    return [name for name in SCHEMA.get(type_name, {}) if name in selection]


def _prune(value, selection: str, type_name: str):
    """Return only what the query selected, so a client asking for a field the
    schema lacks would visibly get nothing back for it."""
    if isinstance(value, list):
        return [_prune(v, selection, type_name) for v in value]
    if not isinstance(value, dict):
        return value
    fields = SCHEMA.get(type_name, {})
    out = {}
    for name, info in fields.items():
        if name not in value or name not in selection:
            continue
        if info["kind"] in ("OBJECT",):
            out[name] = _prune(value[name], selection, info["type_name"])
        else:
            out[name] = value[name]
    if "__typename" in value:
        out["__typename"] = value["__typename"]
    # Union members carry their own fields.
    if type_name == "CustomFieldUnion":
        return value
    return out


class Handler(BaseHTTPRequestHandler):
    state: FakeJobber = None                       # type: ignore[assignment]

    # ------------------------------------------------------------------ util
    def _json(self, payload, status=200):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read(self) -> bytes:
        return self.rfile.read(int(self.headers.get("Content-Length") or 0))

    def log_message(self, *args):
        pass

    # ------------------------------------------------------------------ post
    def do_POST(self):                                          # noqa: N802
        if self.path.endswith("/oauth/token"):
            return self._token()
        if self.path.endswith("/graphql"):
            return self._graphql()
        self.send_error(404)

    # ----------------------------------------------------------------- oauth
    def _token(self):
        raw = self._read().decode("utf-8")
        form = {k: v[0] for k, v in
                __import__("urllib.parse", fromlist=["parse_qs"]).parse_qs(raw).items()}
        state = self.state
        with state.lock:
            state.token_calls += 1
            grant = form.get("grant_type")
            if grant == "refresh_token":
                if form.get("refresh_token") != state.refresh_token:
                    return self._json({"error": "invalid_grant"}, 400)
            elif grant == "authorization_code":
                if form.get("code") != "test-code":
                    return self._json({"error": "invalid_grant"}, 400)
            else:
                return self._json({"error": "unsupported_grant_type"}, 400)

            state.access_token = f"access-{state.token_calls + 1}"
            body = {"access_token": state.access_token, "expires_in": 3600,
                    "token_type": "Bearer", "scope": "read_jobs read_clients"}
            if state.rotate_refresh:
                state.refresh_token = f"refresh-{state.token_calls + 1}"
                body["refresh_token"] = state.refresh_token
        return self._json(body)

    # --------------------------------------------------------------- graphql
    def _graphql(self):
        state = self.state
        body = json.loads(self._read().decode("utf-8") or "{}")
        query = body.get("query") or ""
        variables = body.get("variables") or {}

        with state.lock:
            state.graphql_calls += 1
            call = state.graphql_calls
            # Simulate the 60-minute expiry by retiring the current token. The
            # client's copy stops matching, so it gets a 401 until it refreshes
            # -- and the token a refresh hands back then works.
            if (state.expire_after_calls is not None and not state.expired_yet
                    and call > state.expire_after_calls):
                state.expired_yet = True
                state.access_token = "retired-by-expiry"
            token_ok = (self.headers.get("Authorization") ==
                        f"Bearer {state.access_token}")

        if not self.headers.get("X-JOBBER-GRAPHQL-VERSION"):
            return self._json({"errors": [{"message":
                "X-JOBBER-GRAPHQL-VERSION header is required"}]}, 400)
        if not token_ok:
            return self._json({"errors": [{"message": "Unauthorized"}]}, 401)
        if call in state.http_429_on_call:
            self.send_response(429)
            self.send_header("Retry-After", "0")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if call in state.throttle_on_call:
            return self._json({"errors": [{
                "message": "Throttled",
                "extensions": {"code": "THROTTLED"}}]})

        if "__typename" in query and "jobs" not in query:
            return self._json({"data": {"__typename": "Query"}})
        if "__type(" in query or "__type (" in query:
            return self._introspect(variables.get("name", ""))
        if "jobs(" in query:
            return self._jobs(query, variables)
        return self._json({"errors": [{"message": f"unhandled query: {query[:80]}"}]})

    def _introspect(self, name: str):
        if name in UNIONS:
            return self._json({"data": {"__type": {
                "name": name, "kind": "UNION", "fields": None,
                "possibleTypes": [{"name": n} for n in UNIONS[name]]}}})
        fields = SCHEMA.get(name)
        if fields is None:
            return self._json({"data": {"__type": None}})
        return self._json({"data": {"__type": {
            "name": name, "kind": "OBJECT",
            "fields": [{"name": key,
                        "args": [{"name": a} for a in info["args"]],
                        "type": {"kind": info["kind"], "name": info["type_name"],
                                 "ofType": None}}
                       for key, info in fields.items()],
            "possibleTypes": None}}})

    def _jobs(self, query: str, variables: dict):
        state = self.state
        first = min(int(variables.get("first") or 25), state.page_size_cap)
        after = variables.get("after")
        start = 0
        if after:
            try:
                start = int(after.split(":")[-1])
            except ValueError:
                start = 0
        with state.lock:
            state.jobs_queries += 1
        window = state.jobs[start:start + first]
        end = start + len(window)

        nodes = [_prune(job, query, "Job") for job in window]
        data = {"jobs": {
            "nodes": nodes,
            "pageInfo": {"hasNextPage": end < len(state.jobs),
                         "endCursor": f"cursor:{end}"},
        }}
        if "totalCount" in query:
            data["jobs"]["totalCount"] = len(state.jobs)

        with state.lock:
            state.available_points = max(0, state.available_points - 60)
            points = state.available_points
        return self._json({"data": data, "extensions": {"cost": {
            "requestedQueryCost": 60,
            "throttleStatus": {"maximumAvailable": 10000,
                               "currentlyAvailable": points,
                               "restoreRate": 500}}}})


def serve(state: FakeJobber, port: int = 0):
    """Start the fake API on a background thread; returns (server, base_url)."""
    handler = type("BoundHandler", (Handler,), {"state": state})
    server = HTTPServer(("127.0.0.1", port), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"
