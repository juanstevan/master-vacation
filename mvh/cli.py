"""Command line entry point: python -m mvh <command>"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from .config import Settings
from .discover import discover, read_id_file
from .export import write_csv, write_jsonl, write_rag_corpus, write_xlsx
from .extract import extract_layers
from .fetch import fetch_all, raw_ids
from .httpclient import PoliteSession
from .parse import load_records, parse_raw_dir
from .records import FIELDS, build_record


def build_settings(args) -> Settings:
    return Settings(
        base_url=args.base_url,
        delay=args.delay,
        timeout=args.timeout,
        respect_robots=not args.allow_disallowed,
    )


def make_session(args, settings: Settings, cache_dir: Path) -> PoliteSession:
    return PoliteSession(settings, cache_dir=cache_dir,
                         allow_disallowed=args.allow_disallowed)


def resolve_ids(args, session=None, settings=None) -> list[str]:
    ids: set[str] = set()
    if getattr(args, "ids", None):
        ids |= {i.strip() for i in args.ids.split(",") if i.strip()}
    if getattr(args, "ids_file", None):
        ids |= read_id_file(args.ids_file)
    if getattr(args, "id_range", None):
        start, end = args.id_range
        ids |= {str(i) for i in range(start, end + 1)}
    return sorted(ids, key=lambda v: int(v) if v.isdigit() else 0)


# --------------------------------------------------------------------- commands
def cmd_discover(args) -> int:
    settings = build_settings(args)
    out_dir = Path(args.out)
    session = make_session(args, settings, out_dir / "raw")
    id_range = tuple(args.id_range) if args.id_range else None
    ids, report = discover(session, settings, not args.no_sitemap, not args.no_index, id_range)

    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "ids.json").write_text(json.dumps(ids, indent=2), encoding="utf-8")
    (out_dir / "discovery_report.txt").write_text("\n".join(report), encoding="utf-8")

    print(f"\nFound {len(ids)} home ids -> {out_dir / 'ids.json'}")
    for line in report:
        print("  " + line)
    if not ids:
        print(
            "\nNothing found. The site may render its listing index with JavaScript.\n"
            "Fall back to: paste your home ids into a text file and run\n"
            "  python -m mvh fetch --ids-file my_ids.txt"
        )
    return 0


def cmd_fetch(args) -> int:
    settings = build_settings(args)
    out_dir = Path(args.out)
    raw_dir = out_dir / "raw"
    session = make_session(args, settings, raw_dir)

    ids = resolve_ids(args)
    if not ids:
        ids_file = out_dir / "ids.json"
        if ids_file.exists():
            ids = json.loads(ids_file.read_text(encoding="utf-8"))
    if not ids:
        print("No ids. Run 'discover' first, or pass --ids / --ids-file / --id-range.")
        return 2

    print(f"Fetching {len(ids)} homes at {settings.delay}s intervals "
          f"(~{len(ids) * settings.delay * (2 if not args.no_quote else 1) / 60:.0f} min)")
    fetch_all(session, settings, ids, raw_dir, with_quote=not args.no_quote,
              refresh=args.refresh)
    print(f"Saved raw pages to {raw_dir}")
    return 0


def cmd_parse(args) -> int:
    settings = build_settings(args)
    out_dir = Path(args.out)
    records = parse_raw_dir(out_dir / "raw", settings)
    if not records:
        print(f"No saved HTML in {out_dir / 'raw'}. Run 'fetch' first.")
        return 2
    write_jsonl(records, out_dir / "parsed.jsonl")
    print(f"Parsed {len(records)} homes -> {out_dir / 'parsed.jsonl'}")
    return 0


def cmd_export(args) -> int:
    settings = build_settings(args)
    out_dir = Path(args.out)
    parsed = out_dir / "parsed.jsonl"
    records = (
        load_records(parsed) if parsed.exists() and not args.reparse
        else parse_raw_dir(out_dir / "raw", settings)
    )
    if not records:
        print("Nothing to export. Run 'fetch' and 'parse' first.")
        return 2

    write_csv(records, out_dir / "homes.csv")
    write_xlsx(records, out_dir / "homes.xlsx")
    write_jsonl(records, parsed)
    if not args.no_rag:
        write_rag_corpus(records, out_dir / "rag_corpus")

    filled = {f: sum(1 for r in records if str(r.get(f, "")).strip()) for f in FIELDS}
    print(f"\nExported {len(records)} homes:")
    print(f"  {out_dir / 'homes.xlsx'}")
    print(f"  {out_dir / 'homes.csv'}")
    print(f"  {out_dir / 'parsed.jsonl'}")
    if not args.no_rag:
        print(f"  {out_dir / 'rag_corpus'}/  ({len(records)} markdown docs)")
    print("\nColumn fill rates (low numbers mean the parser needs tuning "
          "-- run 'inspect' on one home):")
    for field in FIELDS:
        if field in ("url", "quote_url", "scraped_at", "listing_id"):
            continue
        pct = 100.0 * filled[field] / len(records)
        bar = "#" * int(pct / 5)
        print(f"  {field:<22} {pct:5.1f}%  {bar}")
    return 0


def cmd_inspect(args) -> int:
    """Show what each extraction layer sees on one page -- the tuning tool."""
    settings = build_settings(args)
    out_dir = Path(args.out)

    if args.file:
        html = Path(args.file).read_text(encoding="utf-8", errors="replace")
        quote_html, home_id, url = None, args.id or "local", args.file
    else:
        home_id = args.id
        cached = out_dir / "raw" / f"{home_id}.html"
        if cached.exists() and not args.refresh:
            html = cached.read_text(encoding="utf-8", errors="replace")
        else:
            session = make_session(args, settings, out_dir / "raw")
            _, html = session.get(settings.home_url(home_id), cache_key=home_id,
                                  refresh=args.refresh)
        quote_path = out_dir / "raw" / f"{home_id}.quote.html"
        quote_html = (
            quote_path.read_text(encoding="utf-8", errors="replace")
            if quote_path.exists() else None
        )
        url = settings.home_url(home_id)

    layers = extract_layers(html, url)
    quote_layers = extract_layers(quote_html, settings.quote_url(home_id)) if quote_html else None

    print("=" * 72)
    print(f"INSPECT  {url}   ({len(html):,} bytes of HTML)")
    print("=" * 72)
    print(f"\nJSON-LD blocks: {len(layers['jsonld'])}")
    for block in layers["jsonld"][:5]:
        types = block.get("@type") if isinstance(block, dict) else None
        keys = list(block.keys())[:12] if isinstance(block, dict) else "list"
        print(f"  @type={types}  keys={keys}")
    print(f"\nEmbedded JSON payloads: {len(layers['state'])}")
    for block in layers["state"][:5]:
        if isinstance(block, dict):
            print(f"  top-level keys: {list(block.keys())[:14]}")
    print(f"\nMeta tags: {len(layers['meta'])}")
    for key, value in list(layers["meta"].items())[:12]:
        print(f"  {key}: {value[:90]}")
    print(f"\nMicrodata itemprops: {len(layers['microdata'])}")
    for key, value in list(layers["microdata"].items())[:12]:
        print(f"  {key}: {value[:70]}")
    print(f"\nLabel/value pairs found on the page: {len(layers['kv'])}")
    for key, value in list(layers["kv"].items())[:40]:
        print(f"  {key!r}: {value[:70]!r}")
    print(f"\nAmenities: {len(layers['amenities'])}")
    print("  " + ", ".join(layers["amenities"][:25]))
    print(f"\nImages: {len(layers['images'])}")
    for image in layers["images"][:5]:
        print(f"  {image[:100]}")
    print(f"\nHeadings: {layers['headings'][:10]}")
    print(f"\nBody text: {len(layers['text']):,} chars. First 600:")
    print("  " + layers["text"][:600].replace("\n", "\n  "))

    record = build_record(home_id, layers, quote_layers, url,
                          settings.quote_url(home_id) if quote_layers else "")
    print("\n" + "=" * 72)
    print("RESULTING SPREADSHEET ROW")
    print("=" * 72)
    for field in FIELDS:
        value = record.get(field, "")
        if field in ("description",) and value:
            value = str(value)[:120] + "..."
        source = record.sources.get(field, "")
        flag = " " if str(value).strip() else "!"
        print(f" {flag} {field:<22} {str(value)[:80]:<80} {source}")
    if record.extras:
        print("\nUnmapped fields also captured (they land on the 'Other fields' sheet):")
        for key, value in list(record.extras.items())[:30]:
            print(f"   {key}: {value[:70]}")
    print("\nFields marked '!' are empty. If an important one is empty but you can "
          "see it on the page,\nsend me the label text and I'll map it in "
          "mvh/extract.py LABEL_MAP.")
    return 0


def cmd_all(args) -> int:
    for step in (cmd_discover, cmd_fetch, cmd_parse, cmd_export):
        code = step(args)
        if code:
            return code
    return 0


# ------------------------------------------------------------------------ main
def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m mvh",
        description="Export Master Vacation Homes listing data to a spreadsheet.",
    )
    parser.add_argument("--base-url", default=Settings().base_url)
    parser.add_argument("--out", default="data", help="output directory (default: data)")
    parser.add_argument("--delay", type=float, default=1.0,
                        help="seconds between requests (default: 1.0)")
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--allow-disallowed", action="store_true",
                        help="ignore robots.txt (only for a site you operate)")
    parser.add_argument("-v", "--verbose", action="store_true")

    subparsers = parser.add_subparsers(dest="command", required=True)

    def add_id_args(sub):
        sub.add_argument("--ids", help="comma separated home ids")
        sub.add_argument("--ids-file", help="text file with one home id per line")
        sub.add_argument("--id-range", nargs=2, type=int, metavar=("START", "END"))

    p = subparsers.add_parser("discover", help="find all home ids")
    p.add_argument("--no-sitemap", action="store_true")
    p.add_argument("--no-index", action="store_true")
    p.add_argument("--id-range", nargs=2, type=int, metavar=("START", "END"))
    p.set_defaults(func=cmd_discover)

    p = subparsers.add_parser("fetch", help="download listing + quote pages")
    add_id_args(p)
    p.add_argument("--no-quote", action="store_true", help="skip the /quote pages")
    p.add_argument("--refresh", action="store_true", help="re-download cached pages")
    p.set_defaults(func=cmd_fetch)

    p = subparsers.add_parser("parse", help="turn saved HTML into records")
    p.set_defaults(func=cmd_parse)

    p = subparsers.add_parser("export", help="write xlsx/csv/jsonl/rag corpus")
    p.add_argument("--reparse", action="store_true")
    p.add_argument("--no-rag", action="store_true")
    p.set_defaults(func=cmd_export)

    p = subparsers.add_parser("inspect", help="show what one page yields (tuning aid)")
    p.add_argument("--id", help="home id, e.g. 390765")
    p.add_argument("--file", help="a saved .html file instead of fetching")
    p.add_argument("--refresh", action="store_true")
    p.set_defaults(func=cmd_inspect)

    p = subparsers.add_parser("all", help="discover -> fetch -> parse -> export")
    add_id_args(p)
    p.add_argument("--no-sitemap", action="store_true")
    p.add_argument("--no-index", action="store_true")
    p.add_argument("--no-quote", action="store_true")
    p.add_argument("--refresh", action="store_true")
    p.add_argument("--reparse", action="store_true")
    p.add_argument("--no-rag", action="store_true")
    p.set_defaults(func=cmd_all)

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    if args.command == "inspect" and not (args.id or args.file):
        parser.error("inspect needs --id or --file")
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
