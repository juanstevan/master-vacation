# Master Vacation Homes — listing data export

Pulls the public listing data for every home on `mastervacationhomes.com` into
a spreadsheet, plus one Markdown document per home ready to feed a RAG /
knowledge-base index for customer service.

It reads the **public web pages** — the same ones a guest sees. No credentials,
no database access, and it never books anything.

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

## Tests

```bash
python3 tests/test_site.py       # 16 tests, every one a bug found on the live site
python3 tests/test_extract.py    # 14 tests for the generic extraction layer
python3 tests/test_pipeline.py   # end to end against a local fake site
```

## One note

The site is fed by **Ciirus** (the photos come from `cdn.ciirus.com`, and
owner login points at `owners.ciirus.com`). Ciirus has a proper owner/property
API. If whoever administers your Ciirus account can turn on API access, that
export will be cleaner and more complete than anything read off the web pages —
including the fields the site simply never renders. This tool is the good
fallback for when you can't get it, and for a customer-service index it is
more than adequate.
