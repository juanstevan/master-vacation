# Master Vacation Homes — listing data export

Pulls the public listing data for every home on `mastervacationhomes.com` into
a spreadsheet, plus one Markdown document per home ready to feed a RAG /
knowledge-base index for customer service.

It reads the **public web pages** — the same ones a guest sees. No credentials,
no database access, and it never books anything.

A second, independent pipeline reads the maintenance jobs from **Jobber** over
its authenticated API — see [Maintenance jobs from Jobber](#maintenance-jobs-from-jobber).
That one does need credentials, and gets them via OAuth: no password is ever
handled and nothing secret is stored in this repository.

## Run it

```bash
pip install -r requirements.txt
python -m mvh run
```

That is the whole job: discover every home, download it, export. Roughly
1,070 homes at 3 requests each, deliberately spaced — about 40 minutes.
It is resumable: pages are cached under `data/raw/`, so if it stops halfway
just run it again and it picks up where it left off.

```bash
python -m mvh run --limit 5            # smoke test: first 5 homes
python -m mvh run --ids 390765,326851  # just these
python -m mvh run --no-prices          # skip the live quote lookups
python -m mvh --delay 2 run            # slower, gentler on the server
python -m mvh run --rediscover         # re-walk /search for new homes
```

Re-running after a parser change costs nothing: it re-reads the cached HTML
rather than the site. Only `--refresh` re-downloads.

## What you get

```
data/
  homes.xlsx      the spreadsheet (6 tabs)
  homes.csv       same rows, plain CSV
  parsed.jsonl    full nested records, one JSON object per home
  rag_corpus/     one Markdown doc per home -- what you load into the index
  raw/            the saved HTML, so a re-run never re-downloads
  ids.json        every home id found
```

`homes.xlsx` tabs: **Homes** (one row per home), **Amenities** (one row per
home per amenity), **Quote details**, **Other fields**, **Data sources**
(which part of which page each value came from), **Coverage** (fill rate per
column).

## Where the data comes from

Three sources per home, all belonging to the site itself:

| Source | What it gives |
|---|---|
| `/home/{id}` | summary line, Main Features, description, photos, blocked dates |
| `/home/{id}/quote` | the confirmation letter: street address, check-in times, WiFi, gate access, optional services |
| `/home/{id}/getquote` | JSON: property type, pool/spa flags, and live pricing |

`getquote` is the booking widget's own "Get Quote" price lookup. It needs
dates, so each home gets the first free 4-night window at least three weeks
out, read from that home's own datepicker calendar. `quote_dates` records the
window used, because **`rate` and `quote_total` are seasonal** — they are a
sample for those dates, not a fixed price. `cleaning_fee` and `service_fee`
are per-property and stable.

The datepicker also hands over each home's booked dates for free, which land
in `blocked_dates` (a snapshot as of the run, not a live calendar).

## Deliberately not collected

- **Coordinates.** The pages carry `place:location:latitude/longitude` meta
  tags, but they are the company office and identical on all 1,070 listings —
  a Miami condo would plot in Kissimmee. An empty column beats a wrong one.
- **Ratings, review counts, deposits.** The site does not publish them, so
  those columns were removed rather than shipped permanently blank.

## If a column looks wrong

`mvh/site.py` is the whole site-specific layer, written against the real
markup — roughly: `parse_home` (listing page), `parse_letter` (confirmation
letter), `build_record` (merge). Every selector in it was checked against live
pages. `mvh/extract.py` is the older, site-agnostic guessing layer, still used
by the `inspect` command as a tuning aid.

The **Data sources** tab tells you which page each value came from, so a wrong
value is traceable to one parser. After a fix, `python -m mvh run` re-parses
the cache — no re-downloading.

## Maintenance jobs from Jobber

A second, separate pipeline: `python -m mvh jobber ...` reads the maintenance
jobs (PH ON/OFF, BBQ CLEAN, Guest Reports) out of Jobber's API.

### Jobber has no API keys

There is nothing to find in the Jobber web app, because Jobber does not issue
API keys and its API has no password login. The only way in is OAuth 2.0
against a GraphQL endpoint, and the two live in different places:

| | |
|---|---|
| `secure.getjobber.com` | the web app people log into. No API surface. |
| `developer.getjobber.com` | where an app, and its credentials, are created. |
| `api.getjobber.com/api/graphql` | the API itself. |

### Option A: one-time setup on your own machine

Done once, by a **Jobber admin** of the account:

1. Create an app at `developer.getjobber.com`.
2. Set its redirect URI to exactly `http://localhost:8123/oauth/callback`
   (or set `JOBBER_REDIRECT_URI` to whatever you registered instead).
3. Give it **read** scopes for jobs, clients and properties.
4. Export the credentials it issues, then consent once in a browser:

```bash
export JOBBER_CLIENT_ID=...
export JOBBER_CLIENT_SECRET=...
python -m mvh jobber login
```

That stores a refresh token at `~/.config/mvh/jobber_token.json`, mode 0600,
outside this repository. **No credential is ever written into the source
tree.** On a server with no browser, authorize on a laptop and pass the code
with `jobber login --code <code>`, or seed `JOBBER_REFRESH_TOKEN` — but point
`JOBBER_TOKEN_FILE` somewhere writable too, because Jobber rotates the refresh
token and the new one has to be saved or the next run cannot authenticate.

### Option B: Supabase, when localhost will not do

If the Jobber app cannot use a `localhost` redirect URI — or the person who can
consent is not the person who runs the pull — deploy the Edge Function in
`supabase/functions/jobber-auth`. It becomes the redirect URI, and it is a
better place for the credentials than a laptop: the client secret and the
refresh token stay in Supabase, and the machine running the pull holds neither.

```bash
supabase link --project-ref <your-project-ref>
supabase db push                       # creates the jobber_oauth table

# Merge supabase/config.toml.example into supabase/config.toml, then:
supabase secrets set \
  JOBBER_CLIENT_ID=...        \
  JOBBER_CLIENT_SECRET=...    \
  JOBBER_REDIRECT_URI=https://<ref>.supabase.co/functions/v1/jobber-auth/callback \
  MVH_ADMIN_KEY=$(openssl rand -hex 32)   \
  MVH_TOKEN_KEY=$(openssl rand -hex 32)   \
  MVH_STATE_SECRET=$(openssl rand -hex 32)

supabase functions deploy jobber-auth
```

Register that same `JOBBER_REDIRECT_URI` on the app in the Developer Center —
it has to match character for character. Then a **Jobber admin** opens this
once in a browser:

```
https://<ref>.supabase.co/functions/v1/jobber-auth/start?key=<MVH_ADMIN_KEY>
```

On the machine that pulls the data, that is the whole configuration:

```bash
export JOBBER_TOKEN_ENDPOINT=https://<ref>.supabase.co/functions/v1/jobber-auth/token
export JOBBER_TOKEN_KEY=<MVH_TOKEN_KEY>
python -m mvh jobber probe
```

`probe` reports `auth mode remote` and confirms no Jobber credential is needed
locally. `curl https://<ref>.supabase.co/functions/v1/jobber-auth/health` says
what is configured and whether a grant is stored, naming missing secrets but
never printing their values.

**The function is public and knows it.** Jobber's redirect is a plain browser
request that cannot carry a Supabase JWT, so it deploys with
`verify_jwt = false` — which means it authenticates every caller itself:
`/start` needs `MVH_ADMIN_KEY`, `/callback` accepts only a `state` value it
signed itself (HMAC, 15-minute expiry), and `/token` needs `MVH_TOKEN_KEY`.
All three comparisons are constant-time. The tokens table has RLS on with no
policies at all, so nothing but the service role can read it.

### Run it

```bash
python -m mvh jobber probe               # is it working? which part is broken?
python -m mvh jobber pull --months 24    # the backfill
```

`probe` checks each link in the chain separately — credentials present, token
valid, API version accepted, jobs scope granted — and prints the remaining
query budget, so a failure names the step that failed rather than "not
loading".

```bash
python -m mvh jobber pull --max-pages 2   # smoke test before the real run
python -m mvh jobber pull --months 36     # a wider window
python -m mvh jobber pull --visits 0      # skip visits, cheaper per page
python -m mvh jobber export               # re-export from cache, no API calls
python -m mvh jobber status               # what the last pull got to
python -m mvh jobber schema               # what this account actually exposes
```

Output lands in `data/jobber/`: `jobs.xlsx` (Jobs, Visits, Custom fields,
Coverage), `jobs.csv`, `jobs.jsonl` (the full nested records), and `raw/` —
one file per page fetched.

### Why it is built this way

**Volume is not the problem; an unbounded query is.** Jobber prices each
query against a leaky bucket (10,000 points, refilling ~500/sec) and reports
the balance on every response. A connection sent without `first:` is priced as
if 100 nodes came back *at every level of nesting* — which is what actually
gets a "give me all the jobs" query rejected, whether the account holds 500
jobs or 500,000. Every connection here is bounded, and the client reads the
returned balance and waits for the bucket to refill before it runs dry.

**Access tokens expire after 60 minutes.** A full backfill takes longer than
that, so the token is refreshed mid-run. This is the usual reason a naive
backfill works on a 50-job test and dies two thirds of the way through a real
one.

**Pages are saved as they arrive.** An interrupted run resumes from its cursor
rather than starting over; re-exporting never touches the API. If the query
changes, the resume is abandoned rather than mixing two record shapes.

**The query is built from the live schema.** Jobber pins behaviour to a dated
API version and accounts differ in which optional fields they expose, so the
field list in `mvh/jobber/jobs.py` is a wish list: anything this account does
not have is dropped and named in the run summary, instead of a single unknown
field rejecting the whole request.

### Known open questions

- `--months 24` is applied after fetching, because whether the window can move
  server-side depends on the `filter` argument this account exposes.
  `jobber schema` reports whether it is there; moving the window into the query
  would cut a large backfill down and is the obvious next improvement.
- `JOBBER_API_VERSION` defaults to `2025-01-20`. If that version has been
  retired the server says so and lists the live ones — `probe` prints the
  message and tells you which variable to set.
- The `category` column (PH ON / PH OFF / BBQ CLEAN / GUEST REPORT / OTHER) is
  a first pass that reads job titles. The Coverage sheet counts how many land
  in OTHER; once real titles are in front of us the patterns in
  `mvh/jobber/export.py` should be tightened, or replaced by a custom field if
  the team records the problem type as structured data.

## Tests

```bash
python3 tests/test_site.py       # 16 tests, every one a bug found on the live site
python3 tests/test_extract.py    # 14 tests for the generic extraction layer
python3 tests/test_pipeline.py   # end to end against a local fake site
python3 tests/test_jobber.py     # 33 tests against a local fake Jobber API
bun tests/test_supabase_function.ts   # 17 tests for the Supabase function
```

`test_jobber.py` runs the whole Jobber path — OAuth, refresh, pagination,
throttling, export — against a fake API in `tests/fixtures/fake_jobber.py`, so
it needs no credentials. Each test stands for something that breaks a real
backfill: a token that dies at minute 61, a rotated refresh token that was
never saved, an unbounded query, a field the account does not have, a run
interrupted at page 400.

`test_supabase_function.ts` runs the Edge Function's handler directly against
an in-memory stand-in for Postgres and a fake Jobber, so the consent flow, the
key checks, the signed state and the refresh lease are all exercised without
deploying anything. It runs under `bun` or `deno run -A`.

## One note

The site is fed by **Ciirus** (the photos come from `cdn.ciirus.com`, and
owner login points at `owners.ciirus.com`). Ciirus has a proper owner/property
API. If whoever administers your Ciirus account can turn on API access, that
export will be cleaner and more complete than anything read off the web pages —
including the fields the site simply never renders. This tool is the good
fallback for when you can't get it, and for a customer-service index it is
more than adequate.
