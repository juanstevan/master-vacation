# Master Vacation Homes — listing data export

Pulls the public listing information for every home on
`mastervacationhomes.com` into a spreadsheet, plus a set of Markdown documents
ready to feed a RAG / knowledge-base system for customer service.

For each home it reads the two public pages you already use:

| Page | URL | What it gives |
|---|---|---|
| Listing | `/home/{id}` | name, home number, bedrooms, bathrooms, sleeps, community, amenities, description, photos |
| Quote | `/home/{id}/quote` | confirmation letter text, rates, fees, taxes, totals, cancellation policy |

It reads the **public web pages** — the same ones a guest sees. It does not
connect to any database and needs no credentials.

## What you get

```
data/
  homes.xlsx        <- the spreadsheet (6 tabs)
  homes.csv         <- same rows, plain CSV
  parsed.jsonl      <- full nested records, one JSON object per home
  rag_corpus/       <- one Markdown doc per home: what you load into the RAG index
  raw/              <- the saved HTML, so a re-run never re-downloads
```

`homes.xlsx` tabs:

- **Homes** — one row per home, ~50 columns
- **Amenities** — one row per home per amenity, easy to filter
- **Quote details** — rates, fees and totals off the quote page
- **Other fields** — anything the site shows that didn't map to a known column
- **Data sources** — which part of the page each value came from
- **Coverage** — how full each column is, so you can see what needs tuning

To see the layout before pointing this at the real site, run the demo — it
builds a workbook from three invented test homes served locally:

```bash
python3 tests/fixtures/fake_site.py 8899 &
python3 -m mvh --base-url http://127.0.0.1:8899 --out demo_data --delay 0 all
```

## Setup (once)

```bash
pip install -r requirements.txt
```

## Run it

**Step 1 — check one home first.** This costs one page load and tells you
whether the parser reads your site correctly:

```bash
python -m mvh inspect --id 390765
```

It prints every field it found and marks empty ones with `!`. If something
important is empty, send me the label text as it appears on the page and I'll
map it.

**Step 2 — find all the homes:**

```bash
python -m mvh discover
```

Writes `data/ids.json`. If it finds nothing, the listing index is probably
rendered by JavaScript — in that case put your home ids in a text file, one per
line, and skip to step 3 with `--ids-file my_ids.txt`.

**Step 3 — the whole job:**

```bash
python -m mvh all
```

or run the stages separately (`fetch`, `parse`, `export`). Everything is
resumable: `fetch` skips pages already saved in `data/raw/`, so if it stops
halfway just run it again.

Useful flags:

```bash
python -m mvh --delay 2 all              # slower, gentler on the server
python -m mvh fetch --ids 390765,390766  # just these homes
python -m mvh fetch --ids-file ids.txt   # ids from a file
python -m mvh fetch --no-quote           # skip the /quote pages
python -m mvh export --reparse           # re-parse saved HTML without re-downloading
```

Roughly 2 requests per home at 1 second apart: 300 homes ≈ 10 minutes. It is
deliberately slow so it doesn't load the site — leave it running.

## If a column comes out empty

Every site labels things differently, and the parser reads six different layers
of the page (structured data, embedded JSON, meta tags, spec tables, label/value
pairs, and finally the visible text). If a column is empty:

1. Run `python -m mvh inspect --id <a home id>`.
2. Look at the "Label/value pairs found on the page" section for the label the
   site actually uses (e.g. `Max Guests` rather than `Sleeps`).
3. Add it to `LABEL_MAP` in `mvh/extract.py` — one line, `"max guests": "sleeps"`.
4. Re-run `python -m mvh export --reparse`. No re-downloading needed.

Nothing is thrown away in the meantime: unmapped fields still land on the
**Other fields** tab, and the full page text is always kept in `rag_corpus/`.

## Tests

```bash
python3 tests/test_extract.py    # 14 unit tests
python3 tests/test_pipeline.py   # end-to-end against a local fake site
```

`tests/fixtures/fake_site.py` serves three invented homes in three different
markup styles, so the pipeline can be verified without touching the real site.

## Two notes before you run this at scale

- **Ask whoever runs the website first.** This only reads public pages, but a
  few hundred automated requests is the kind of thing IT likes to know about.
- **A proper export beats scraping.** The site is fed by a property management
  system, and those systems (Streamline, Track, Escapia, Barefoot and the like)
  almost always offer a CSV export or an API. If your PMS admin can hand you
  that, the data will be cleaner and complete — this tool is the fallback for
  when you can't get it.
