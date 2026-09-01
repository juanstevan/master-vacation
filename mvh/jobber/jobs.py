"""Pull maintenance jobs out of Jobber, one bounded page at a time.

The query is built against the *live* schema rather than hard-coded. Jobber
pins behaviour to a dated API version and different accounts expose different
optional fields, so a fixed query string is one schema change away from a 400
that rejects the entire request -- including the fields that would have worked.
Here the wanted fields are a wish list: anything the account does not have is
dropped, named in the run summary, and the rest still comes back.

Pages are written to disk as they arrive. A backfill of a large account is a
long run, and it has to be able to die and resume without re-fetching.
"""
from __future__ import annotations

import calendar
import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from .client import JobberClient

log = logging.getLogger("mvh.jobber.jobs")

LEAF_KINDS = {"SCALAR", "ENUM"}


# --------------------------------------------------------------- selections
@dataclass
class Sel:
    """One wanted field. ``sub`` is the nested wish list; ``args`` are the
    arguments to send -- every connection gets a ``first:`` so the query is
    never priced as if 100 nodes came back."""
    name: str
    sub: Optional[list] = None
    args: Optional[dict] = None


def _render_args(args: Optional[dict]) -> str:
    if not args:
        return ""
    parts = []
    for key, value in args.items():
        if isinstance(value, bool):
            parts.append(f"{key}: {'true' if value else 'false'}")
        elif isinstance(value, (int, float)):
            parts.append(f"{key}: {value}")
        elif value is None:
            continue
        else:
            parts.append(f"{key}: {json.dumps(str(value))}")
    return "(" + ", ".join(parts) + ")" if parts else ""


def render(client: JobberClient, type_name: str, wanted: list[Sel],
           dropped: list[str], indent: int = 2, path: str = "") -> str:
    """Render a selection set containing only fields this schema really has."""
    available = client.type_fields(type_name)
    pad = " " * indent
    lines: list[str] = []

    for sel in wanted:
        info = available.get(sel.name)
        here = f"{path}.{sel.name}" if path else sel.name
        if not info:
            dropped.append(here)
            continue
        kind, sub_type = info["kind"], info["type_name"]
        args = _render_args(sel.args)

        if kind in LEAF_KINDS:
            # Asked for a nested shape but the schema says it is a plain
            # value: take the value.
            lines.append(f"{pad}{sel.name}{args}")
            continue

        if not sel.sub:
            dropped.append(here)          # an object needs a selection set
            continue

        if kind in ("UNION", "INTERFACE"):
            body = _render_union(client, sub_type, sel.sub, dropped,
                                 indent + 2, here)
            if body:
                lines.append(f"{pad}{sel.name}{args} {{\n{body}\n{pad}}}")
            else:
                dropped.append(here)
            continue

        body = render(client, sub_type, sel.sub, dropped, indent + 2, here)
        if body.strip():
            lines.append(f"{pad}{sel.name}{args} {{\n{body}\n{pad}}}")
        else:
            dropped.append(here)

    return "\n".join(lines)


def _render_union(client: JobberClient, union_type: str, wanted: list[Sel],
                  dropped: list[str], indent: int, path: str) -> str:
    """Custom fields come back as a union -- one member type per field type --
    so each member needs its own inline fragment over whichever of the wanted
    keys it actually has."""
    pad = " " * indent
    lines = [f"{pad}__typename"]
    for member in client.possible_types(union_type):
        body = render(client, member, wanted, [], indent + 2, path)
        if body.strip():
            lines.append(f"{pad}... on {member} {{\n{body}\n{pad}}}")
    return "\n".join(lines) if len(lines) > 1 else ""


def job_wishlist(visits: int = 5, assignees: int = 5) -> list[Sel]:
    """Everything worth having on a job. Missing fields are dropped, so this
    can stay generous."""
    wanted = [
        Sel("id"), Sel("jobNumber"), Sel("title"), Sel("instructions"),
        Sel("jobStatus"), Sel("jobType"), Sel("createdAt"), Sel("updatedAt"),
        Sel("startAt"), Sel("endAt"), Sel("completedAt"), Sel("total"),
        Sel("client", [Sel("id"), Sel("name"), Sel("isCompany"),
                       Sel("companyName")]),
        Sel("property", [
            Sel("id"), Sel("name"),
            Sel("address", [Sel("street"), Sel("street1"), Sel("street2"),
                            Sel("city"), Sel("province"), Sel("postalCode"),
                            Sel("country")]),
        ]),
        # Where a "problem type" is most likely to live, if the team records
        # one as structured data rather than in the title.
        Sel("customFields", [Sel("label"), Sel("valueText"), Sel("valueNumeric"),
                             Sel("valueBool"), Sel("valueArea"), Sel("value")]),
    ]
    if visits > 0:
        wanted.append(Sel("visits", args={"first": visits}, sub=[
            Sel("totalCount"),
            Sel("nodes", [
                Sel("id"), Sel("title"), Sel("startAt"), Sel("endAt"),
                Sel("completedAt"), Sel("visitStatus"),
                Sel("assignedUsers", args={"first": assignees}, sub=[
                    Sel("nodes", [Sel("id"),
                                  Sel("name", [Sel("full"), Sel("first"),
                                               Sel("last")])]),
                ]),
            ]),
        ]))
    return wanted


def build_jobs_query(client: JobberClient, visits: int = 5) -> tuple[str, list[str]]:
    """Return (query, dropped field paths) for the paginated jobs connection."""
    query_fields = client.type_fields("Query")
    if "jobs" not in query_fields:
        raise RuntimeError(
            "This schema has no `jobs` query. Either the token is for an app "
            "without the jobs read scope, or JOBBER_API_VERSION points at a "
            "version that predates it. Run `python -m mvh jobber schema`.")

    connection_type = query_fields["jobs"]["type_name"]
    node_type = _node_type(client, connection_type)
    dropped: list[str] = []
    selection = render(client, node_type, job_wishlist(visits), dropped, indent=8)

    connection_fields = client.type_fields(connection_type)
    total = "\n    totalCount" if "totalCount" in connection_fields else ""

    query = f"""query Jobs($first: Int!, $after: String) {{
  jobs(first: $first, after: $after) {{{total}
    nodes {{
{selection}
    }}
    pageInfo {{
      hasNextPage
      endCursor
    }}
  }}
}}"""
    return query, dropped


def _node_type(client: JobberClient, connection_type: str) -> str:
    fields = client.type_fields(connection_type)
    if "nodes" in fields:
        return fields["nodes"]["type_name"]
    raise RuntimeError(f"{connection_type} has no `nodes` field to page over")


# -------------------------------------------------------------- date window
def months_ago(months: int, now: Optional[datetime] = None) -> datetime:
    now = now or datetime.now(timezone.utc)
    year, month = now.year, now.month - months
    while month <= 0:
        month += 12
        year -= 1
    day = min(now.day, calendar.monthrange(year, month)[1])
    return now.replace(year=year, month=month, day=day)


def parse_time(value: Any) -> Optional[datetime]:
    if not value or not isinstance(value, str):
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


DATE_FIELDS = ("createdAt", "startAt", "completedAt", "updatedAt")


def in_window(job: dict, cutoff: datetime, date_field: str = "createdAt") -> bool:
    """Keep a job if its primary date is inside the window. A job whose primary
    date is missing falls back to any other date it has, so a record is never
    silently dropped for lacking one field."""
    primary = parse_time(job.get(date_field))
    if primary is not None:
        return primary >= cutoff
    for name in DATE_FIELDS:
        other = parse_time(job.get(name))
        if other is not None:
            return other >= cutoff
    return True                      # undated: keep it and let the export show it


# --------------------------------------------------------------------- pull
@dataclass
class PullResult:
    pages: int = 0
    fetched: int = 0
    kept: int = 0
    total_count: Optional[int] = None
    dropped_fields: list = field(default_factory=list)
    resumed_from_page: int = 0
    complete: bool = False


def _state_path(out_dir: Path) -> Path:
    return out_dir / "state.json"


def _page_path(out_dir: Path, page: int) -> Path:
    return out_dir / "raw" / f"jobs-{page:05d}.json"


def pull_jobs(client: JobberClient, out_dir: Path, months: int = 24,
              page_size: int = 50, visits: int = 5, date_field: str = "createdAt",
              refresh: bool = False, max_pages: Optional[int] = None,
              now: Optional[datetime] = None) -> PullResult:
    """Page through the jobs connection, saving each page as it arrives."""
    out_dir = Path(out_dir)
    (out_dir / "raw").mkdir(parents=True, exist_ok=True)

    query, dropped = build_jobs_query(client, visits=visits)
    if dropped:
        log.info("not in this schema, skipped: %s", ", ".join(dropped))

    # A changed query means the saved pages have a different shape, so resuming
    # onto them would silently mix two record layouts.
    signature = hashlib.sha256(query.encode("utf-8")).hexdigest()[:16]
    state = _load_state(out_dir)
    cursor, page = None, 0
    if not refresh and state.get("signature") == signature and state.get("cursor"):
        cursor, page = state["cursor"], int(state.get("pages") or 0)
        log.info("resuming after page %d (cached pages are reused)", page)
    elif not refresh and state and state.get("signature") != signature:
        log.info("the query changed since the last run; starting a fresh pull")

    cutoff = months_ago(months, now)
    result = PullResult(dropped_fields=dropped, resumed_from_page=page)
    result.kept = int(state.get("kept") or 0) if cursor else 0
    result.fetched = int(state.get("fetched") or 0) if cursor else 0

    log.info("pulling jobs updated on or after %s (%d months), %d per page",
             cutoff.date(), months, page_size)

    try:
        for nodes, info in client.paginate(
                query, {"first": page_size}, "jobs",
                start_cursor=cursor, max_pages=max_pages):
            page += 1
            kept = [job for job in nodes if in_window(job, cutoff, date_field)]
            _page_path(out_dir, page).write_text(
                json.dumps(kept, ensure_ascii=False), encoding="utf-8")

            result.pages += 1
            result.fetched += len(nodes)
            result.kept += len(kept)
            _save_state(out_dir, {
                "signature": signature, "cursor": info.get("endCursor"),
                "pages": page, "fetched": result.fetched, "kept": result.kept,
                "months": months, "date_field": date_field,
                "cutoff": cutoff.isoformat(),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            })
            if result.pages % 10 == 0:
                log.info("  ... page %d, %d jobs seen, %d inside the window",
                         page, result.fetched, result.kept)
            if not info.get("hasNextPage"):
                result.complete = True
    except KeyboardInterrupt:
        log.warning("interrupted -- %d pages are saved; re-run to resume", page)
        raise

    if result.complete:
        _save_state(out_dir, {**_load_state(out_dir), "cursor": None,
                              "complete": True})
    return result


def _load_state(out_dir: Path) -> dict:
    path = _state_path(out_dir)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return {}


def _save_state(out_dir: Path, state: dict) -> None:
    _state_path(out_dir).write_text(json.dumps(state, indent=2), encoding="utf-8")


def load_pages(out_dir: Path) -> list[dict]:
    """Every job saved by previous pulls, in page order."""
    raw = Path(out_dir) / "raw"
    jobs: list[dict] = []
    seen: set[str] = set()
    for path in sorted(raw.glob("jobs-*.json")):
        try:
            page = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            log.warning("skipping unreadable page %s", path)
            continue
        for job in page:
            key = str(job.get("id") or "")
            if key and key in seen:      # a re-run overlapping a cursor boundary
                continue
            if key:
                seen.add(key)
            jobs.append(job)
    return jobs
