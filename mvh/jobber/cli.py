"""`python -m mvh jobber ...` -- connect, check, pull, export."""
from __future__ import annotations

import json
import logging
from pathlib import Path

from . import export as jexport
from .auth import JobberAuth, JobberAuthError
from .client import JobberClient, JobberError
from .config import JobberSettings
from .jobs import load_pages, months_ago, pull_jobs
from .remote_auth import build_auth

log = logging.getLogger("mvh.jobber.cli")

SETUP_HELP = """
Jobber has no API keys and no password login for its API -- the only way in is
OAuth. One-time setup, done by a Jobber ADMIN of the account.

If your Jobber app cannot use a localhost redirect URI, deploy the Supabase
Edge Function in supabase/functions/jobber-auth instead (see the README) and
set JOBBER_TOKEN_ENDPOINT -- then no credential is needed on this machine at
all. Otherwise:

  1. Sign in at developer.getjobber.com and create an app.
  2. Set its redirect URI to exactly:
       {redirect}
  3. Give it read scopes for jobs, clients and properties.
  4. Copy the Client ID and Client Secret into your shell:
       export JOBBER_CLIENT_ID=...
       export JOBBER_CLIENT_SECRET=...
  5. Run: python -m mvh jobber login

That stores a refresh token at {token_file} (mode 0600, outside this repo).
Nothing secret is ever written into the source tree.
"""


def _settings(args) -> JobberSettings:
    return JobberSettings.from_env(
        api_version=getattr(args, "api_version", None),
        redirect_uri=getattr(args, "redirect_uri", None),
        page_size=getattr(args, "page_size", None),
        delay=getattr(args, "request_delay", None),
    )


def _out_dir(args) -> Path:
    return Path(args.out) / "jobber"


def _client(settings: JobberSettings) -> JobberClient:
    return JobberClient(settings, build_auth(settings))


REMOTE_SETUP_HELP = """
This machine is in remote mode: JOBBER_TOKEN_ENDPOINT is set, so access tokens
come from the Supabase Edge Function and nothing secret lives here.

Consent therefore happens at the function, not on this machine. A Jobber ADMIN
opens this once in a browser:

  {start_url}?key=<the MVH_ADMIN_KEY secret>

Then come back and run: python -m mvh jobber probe
"""


def _start_url(settings: JobberSettings) -> str:
    endpoint = settings.token_endpoint
    return (endpoint[: -len("/token")] if endpoint.endswith("/token")
            else endpoint) + "/start"


def cmd_login(args) -> int:
    settings = _settings(args)
    if settings.remote:
        print(REMOTE_SETUP_HELP.format(start_url=_start_url(settings)))
        return 0
    missing = settings.missing_credentials()
    if missing:
        print(SETUP_HELP.format(redirect=settings.redirect_uri,
                                token_file=settings.token_file))
        print(f"Missing right now: {', '.join(missing)}")
        return 2
    auth = JobberAuth(settings)
    try:
        if args.code:
            token = auth.exchange_code(args.code)
        else:
            token = auth.login(open_browser=not args.no_browser)
    except JobberAuthError as exc:
        print(f"\nAuthorization failed: {exc}")
        return 1
    print(f"\nConnected. Refresh token saved to {settings.token_file}")
    if token.scope:
        print(f"Scopes granted: {token.scope}")
    print("\nNext: python -m mvh jobber probe")
    return 0


def cmd_probe(args) -> int:
    """Answer 'is this working, and if not which part is broken' in one run."""
    settings = _settings(args)
    print("Jobber connection check")
    print("=" * 60)
    print(f"  endpoint      {settings.api_url}")
    print(f"  api version   {settings.api_version}")

    missing = settings.missing_credentials()
    if settings.remote:
        print(f"  auth mode     remote (Supabase); no Jobber secret on this machine")
        print(f"  token source  {settings.token_endpoint}")
        print(f"  shared key    {'set' if settings.token_endpoint_key else 'MISSING'}")
        if missing:
            print(REMOTE_SETUP_HELP.format(start_url=_start_url(settings)))
            print(f"Missing right now: {', '.join(missing)}")
            return 2
    else:
        print(f"  auth mode     local OAuth grant")
        print(f"  token file    {settings.token_file}")
        print(f"  client id     {'set' if settings.client_id else 'MISSING'}")
        print(f"  client secret {'set' if settings.client_secret else 'MISSING'}")
        if missing:
            print(SETUP_HELP.format(redirect=settings.redirect_uri,
                                    token_file=settings.token_file))
            return 2

    auth = build_auth(settings)
    if not settings.remote and not auth.token.refresh_token:
        print("\n  refresh token MISSING -- run: python -m mvh jobber login")
        return 2
    if not settings.remote:
        print("  refresh token set")

    client = JobberClient(settings, auth)
    try:
        client.execute("query Ping { __typename }")
    except JobberAuthError as exc:
        where = ("getting a token from the Supabase function"
                 if settings.remote else "authenticating with Jobber")
        print(f"\nFailed while {where}:\n\n{exc}\n")
        return 1
    except JobberError as exc:
        print(f"\nThe API rejected the request:\n\n{exc}\n")
        return 1
    print("\n  authentication OK, API version accepted")

    query_fields = client.type_fields("Query")
    if "jobs" not in query_fields:
        print("\n  the `jobs` query is not visible to this token -- the app is "
              "missing the jobs read scope.")
        return 1

    try:
        data = client.execute(
            "query Probe { jobs(first: 1) { nodes { id title } "
            "pageInfo { hasNextPage } } }")
    except JobberError as exc:
        print(f"\nReading jobs failed:\n\n{exc}\n")
        return 1

    nodes = (data.get("jobs") or {}).get("nodes") or []
    print(f"  jobs readable OK ({len(nodes)} returned by a first:1 probe)")
    if nodes:
        print(f"    sample: {nodes[0].get('id')}  {nodes[0].get('title', '')[:60]}")

    throttle = client.last_throttle
    if throttle:
        print(f"\n  query budget  {throttle.get('currentlyAvailable')} of "
              f"{throttle.get('maximumAvailable')} points, "
              f"restoring {throttle.get('restoreRate')}/sec")
    print("\nEverything needed for a pull is in place.")
    print("Next: python -m mvh jobber pull --months 24")
    return 0


def cmd_schema(args) -> int:
    """What this account actually exposes -- the ground truth for the query."""
    settings = _settings(args)
    client = _client(settings)
    try:
        query_fields = client.type_fields("Query")
        if "jobs" not in query_fields:
            print("No `jobs` query in this schema (missing scope or wrong version).")
            return 1
        connection = query_fields["jobs"]["type_name"]
        node_type = client.type_fields(connection)["nodes"]["type_name"]
        fields = client.type_fields(node_type)
    except (JobberError, JobberAuthError) as exc:
        print(f"Could not read the schema: {exc}")
        return 1

    args_available = sorted(query_fields["jobs"]["args"])
    print(f"jobs -> {connection} of {node_type}")
    print(f"\nArguments accepted by `jobs`: {', '.join(args_available) or 'none'}")
    if "filter" in args_available:
        print("  `filter` exists, so the 24-month window can move server-side "
              "and cut the backfill down. Worth doing as a follow-up.")
    else:
        print("  no `filter` argument, so the window is applied after fetching.")

    print(f"\n{node_type} has {len(fields)} fields:\n")
    for name in sorted(fields):
        info = fields[name]
        suffix = f"({', '.join(sorted(info['args']))})" if info["args"] else ""
        print(f"  {name}{suffix}: {info['type_name']} [{info['kind']}]")
    return 0


def cmd_pull(args) -> int:
    settings = _settings(args)
    out_dir = _out_dir(args)
    client = _client(settings)

    cutoff = months_ago(args.months)
    print(f"Pulling Jobber jobs from {cutoff.date()} onward "
          f"({args.months} months), {settings.page_size} per page.")
    print("Pages are saved as they arrive -- interrupting and re-running resumes.\n")

    try:
        result = pull_jobs(
            client, out_dir, months=args.months, page_size=settings.page_size,
            visits=args.visits, date_field=args.date_field,
            refresh=args.refresh, max_pages=args.max_pages)
    except (JobberError, JobberAuthError) as exc:
        print(f"\nPull failed:\n\n{exc}\n")
        return 1
    except KeyboardInterrupt:
        print("\nStopped. Re-run the same command to resume.")
        return 130

    print(f"\n  pages fetched   {result.pages}"
          f"{f' (resumed after {result.resumed_from_page})' if result.resumed_from_page else ''}")
    print(f"  jobs seen       {result.fetched}")
    print(f"  inside window   {result.kept}")
    print(f"  API requests    {client.requests_made}")
    if result.dropped_fields:
        print(f"\n  Not in this schema, so not collected: "
              f"{', '.join(result.dropped_fields)}")
    if not result.complete:
        print("\n  More pages remain (stopped early or interrupted). "
              "Re-run to continue.")

    jobs = load_pages(out_dir)
    if not jobs:
        print("\nNo jobs inside the window. Widen it with --months, or check "
              "--date-field (createdAt vs updatedAt).")
        return 0
    written = jexport.write_all(jobs, out_dir, no_xlsx=args.no_xlsx)
    print(f"\nExported {len(jobs)} jobs:")
    for path in written:
        print(f"  {path}")
    return 0


def cmd_export(args) -> int:
    """Re-export from saved pages without touching the API."""
    out_dir = _out_dir(args)
    jobs = load_pages(out_dir)
    if not jobs:
        print(f"No saved pages in {out_dir / 'raw'}. Run `jobber pull` first.")
        return 2
    written = jexport.write_all(jobs, out_dir, no_xlsx=args.no_xlsx)
    print(f"Exported {len(jobs)} jobs:")
    for path in written:
        print(f"  {path}")
    return 0


def cmd_status(args) -> int:
    out_dir = _out_dir(args)
    state_file = out_dir / "state.json"
    if not state_file.exists():
        print(f"No pull has run yet (nothing at {state_file}).")
        return 0
    state = json.loads(state_file.read_text(encoding="utf-8"))
    pages = sorted((out_dir / "raw").glob("jobs-*.json"))
    print(f"Last pull state ({state_file}):")
    for key in ("pages", "fetched", "kept", "months", "date_field", "cutoff",
                "complete", "updated_at"):
        if key in state:
            print(f"  {key:<12} {state[key]}")
    print(f"  page files   {len(pages)}")
    print(f"  resumable    {'no -- finished' if state.get('complete') else 'yes'}")
    return 0


def register(subparsers) -> None:
    """Wire the jobber subcommands into the main parser."""
    parser = subparsers.add_parser(
        "jobber", help="pull maintenance jobs from Jobber (OAuth + GraphQL)")
    parser.add_argument("--api-version", help="X-JOBBER-GRAPHQL-VERSION to send")
    parser.add_argument("--redirect-uri", help="must match the app's registered URI")
    parser.add_argument("--page-size", type=int,
                        help="jobs per page (default 50; higher costs more points)")
    parser.add_argument("--request-delay", type=float,
                        help="seconds between API requests (default 0.3)")
    sub = parser.add_subparsers(dest="jobber_command", required=True)

    p = sub.add_parser("login", help="one-time OAuth consent as a Jobber admin")
    p.add_argument("--code", help="paste the code if you authorized elsewhere")
    p.add_argument("--no-browser", action="store_true")
    p.set_defaults(func=cmd_login)

    p = sub.add_parser("probe", help="check credentials, token, version and scopes")
    p.set_defaults(func=cmd_probe)

    p = sub.add_parser("schema", help="list the job fields this account exposes")
    p.set_defaults(func=cmd_schema)

    p = sub.add_parser("pull", help="fetch jobs and export them")
    p.add_argument("--months", type=int, default=24,
                   help="how far back to go (default 24)")
    p.add_argument("--date-field", default="createdAt",
                   choices=["createdAt", "updatedAt", "startAt", "completedAt"],
                   help="which date the window applies to (default createdAt)")
    p.add_argument("--visits", type=int, default=5,
                   help="visits to include per job, 0 to skip (default 5)")
    p.add_argument("--max-pages", type=int, help="stop after N pages (smoke test)")
    p.add_argument("--refresh", action="store_true",
                   help="ignore saved pages and start over")
    p.add_argument("--no-xlsx", action="store_true")
    p.set_defaults(func=cmd_pull)

    p = sub.add_parser("export", help="re-export from saved pages, no API calls")
    p.add_argument("--no-xlsx", action="store_true")
    p.set_defaults(func=cmd_export)

    p = sub.add_parser("status", help="show what the last pull got to")
    p.set_defaults(func=cmd_status)
