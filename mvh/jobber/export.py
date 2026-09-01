"""Flatten Jobber jobs into rows and write them out.

The nested JSON stays intact in ``jobs.jsonl`` -- that is the record of what
the API actually returned. The spreadsheet is the flattened, human-readable
view built from it.
"""
from __future__ import annotations

import csv
import json
import logging
import re
from pathlib import Path
from typing import Any, Optional

from openpyxl import Workbook

from ..export import _safe, _style_sheet

log = logging.getLogger("mvh.jobber.export")

COLUMNS = [
    "job_id", "job_number", "title", "category", "job_status", "job_type",
    "created_at", "updated_at", "start_at", "end_at", "completed_at",
    "client_id", "client_name",
    "property_id", "property_name", "address", "city", "province", "postal_code",
    "total", "visit_count", "first_visit_at", "last_visit_at", "assigned_to",
    "custom_fields", "instructions",
]

# A first pass at the job types described by the team. It reads the title, so
# it is only as good as the titles are consistent -- the Category counts on the
# Coverage sheet are there to show how much lands in OTHER. Tighten these once
# real titles are in front of us.
CATEGORY_PATTERNS = [
    ("PH OFF",       re.compile(r"\bp\.?\s?h\.?\b[^a-z]{0,12}off\b|pool\s+heat\w*\s+off\b", re.I)),
    ("PH ON",        re.compile(r"\bp\.?\s?h\.?\b[^a-z]{0,12}on\b|pool\s+heat\w*\s+on\b", re.I)),
    ("BBQ CLEAN",    re.compile(r"\bbbq\b|barbe?c", re.I)),
    ("GUEST REPORT", re.compile(r"guest\s*report|\bg\.?\s?r\.?\b", re.I)),
]


def categorize(job: dict) -> str:
    text = " ".join(str(job.get(k) or "") for k in ("title", "instructions"))
    for label, pattern in CATEGORY_PATTERNS:
        if pattern.search(text):
            return label
    return "OTHER"


def _address(job: dict) -> dict:
    prop = job.get("property") or {}
    addr = prop.get("address") or {}
    street = addr.get("street") or " ".join(
        part for part in (addr.get("street1"), addr.get("street2")) if part)
    return {
        "property_id": prop.get("id", ""),
        "property_name": prop.get("name", ""),
        "address": (street or "").strip(),
        "city": addr.get("city", ""),
        "province": addr.get("province", ""),
        "postal_code": addr.get("postalCode", ""),
    }


def custom_field_pairs(job: dict) -> list[tuple[str, str]]:
    """Custom fields arrive as a union -- one member type per value type -- so
    the value lands under a different key depending on the field."""
    pairs: list[tuple[str, str]] = []
    for entry in job.get("customFields") or []:
        if not isinstance(entry, dict):
            continue
        label = entry.get("label") or entry.get("__typename") or "custom"
        value = next(
            (entry[key] for key in
             ("valueText", "value", "valueNumeric", "valueBool", "valueArea")
             if entry.get(key) not in (None, "")), "")
        if isinstance(value, (dict, list)):
            value = json.dumps(value, ensure_ascii=False)
        pairs.append((str(label), str(value)))
    return pairs


def visit_rows(job: dict) -> list[dict]:
    visits = (job.get("visits") or {}).get("nodes") or []
    rows = []
    for visit in visits:
        if not isinstance(visit, dict):
            continue
        users = ((visit.get("assignedUsers") or {}).get("nodes")) or []
        rows.append({
            "job_id": job.get("id", ""),
            "job_number": job.get("jobNumber", ""),
            "visit_id": visit.get("id", ""),
            "visit_title": visit.get("title", ""),
            "visit_status": visit.get("visitStatus", ""),
            "start_at": visit.get("startAt", ""),
            "end_at": visit.get("endAt", ""),
            "completed_at": visit.get("completedAt", ""),
            "assigned_to": ", ".join(_user_name(u) for u in users if _user_name(u)),
        })
    return rows


def _user_name(user: Any) -> str:
    if not isinstance(user, dict):
        return ""
    name = user.get("name")
    if isinstance(name, str):
        return name
    if isinstance(name, dict):
        return (name.get("full")
                or " ".join(p for p in (name.get("first"), name.get("last")) if p)
                or "")
    return ""


def to_row(job: dict) -> dict:
    client = job.get("client") or {}
    visits = job.get("visits") or {}
    visit_nodes = [v for v in (visits.get("nodes") or []) if isinstance(v, dict)]
    starts = sorted(v.get("startAt") for v in visit_nodes if v.get("startAt"))
    assignees: list[str] = []
    for visit in visit_nodes:
        for user in ((visit.get("assignedUsers") or {}).get("nodes")) or []:
            name = _user_name(user)
            if name and name not in assignees:
                assignees.append(name)

    row = {
        "job_id": job.get("id", ""),
        "job_number": job.get("jobNumber", ""),
        "title": job.get("title", ""),
        "category": categorize(job),
        "job_status": job.get("jobStatus", ""),
        "job_type": job.get("jobType", ""),
        "created_at": job.get("createdAt", ""),
        "updated_at": job.get("updatedAt", ""),
        "start_at": job.get("startAt", ""),
        "end_at": job.get("endAt", ""),
        "completed_at": job.get("completedAt", ""),
        "client_id": client.get("id", ""),
        "client_name": client.get("name") or client.get("companyName") or "",
        "total": job.get("total", ""),
        "visit_count": visits.get("totalCount", len(visit_nodes)),
        "first_visit_at": starts[0] if starts else "",
        "last_visit_at": starts[-1] if starts else "",
        "assigned_to": ", ".join(assignees),
        "custom_fields": "; ".join(f"{k}: {v}" for k, v in custom_field_pairs(job)),
        "instructions": job.get("instructions", ""),
    }
    row.update(_address(job))
    return {column: row.get(column, "") for column in COLUMNS}


# ------------------------------------------------------------------- writers
def write_jsonl(jobs: list[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        for job in jobs:
            handle.write(json.dumps(job, ensure_ascii=False) + "\n")
    log.info("wrote %s (%d jobs)", path, len(jobs))


def write_csv(jobs: list[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for job in jobs:
            writer.writerow(to_row(job))
    log.info("wrote %s (%d rows)", path, len(jobs))


def write_xlsx(jobs: list[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    wb = Workbook()

    ws = wb.active
    ws.title = "Jobs"
    ws.append(COLUMNS)
    for job in jobs:
        ws.append([_safe(v) for v in to_row(job).values()])
    widths = [50 if c in ("title", "instructions", "custom_fields", "address")
              else max(12, min(30, len(c) + 6)) for c in COLUMNS]
    _style_sheet(ws, widths, wrap_columns=[COLUMNS.index("title") + 1,
                                           COLUMNS.index("instructions") + 1])

    visits = wb.create_sheet("Visits")
    visit_columns = ["job_id", "job_number", "visit_id", "visit_title",
                     "visit_status", "start_at", "end_at", "completed_at",
                     "assigned_to"]
    visits.append(visit_columns)
    for job in jobs:
        for row in visit_rows(job):
            visits.append([_safe(row.get(c, "")) for c in visit_columns])
    _style_sheet(visits, [22, 12, 22, 40, 14, 22, 22, 22, 30])

    custom = wb.create_sheet("Custom fields")
    custom.append(["job_id", "job_number", "label", "value"])
    for job in jobs:
        for label, value in custom_field_pairs(job):
            custom.append([job.get("id", ""), job.get("jobNumber", ""),
                           _safe(label), _safe(value)])
    _style_sheet(custom, [22, 12, 30, 60], wrap_columns=[4])

    cover = wb.create_sheet("Coverage")
    cover.append(["field", "filled", "total", "percent_filled"])
    total = len(jobs) or 1
    rows = [to_row(job) for job in jobs]
    for column in COLUMNS:
        filled = sum(1 for r in rows if str(r.get(column, "")).strip())
        cover.append([column, filled, len(jobs), round(100.0 * filled / total, 1)])
    cover.append([])
    cover.append(["category", "jobs", "", "percent"])
    counts: dict[str, int] = {}
    for row in rows:
        counts[row["category"]] = counts.get(row["category"], 0) + 1
    for name, count in sorted(counts.items(), key=lambda kv: -kv[1]):
        cover.append([name, count, "", round(100.0 * count / total, 1)])
    _style_sheet(cover, [26, 10, 10, 16])

    wb.save(path)
    log.info("wrote %s (%d jobs)", path, len(jobs))


def write_all(jobs: list[dict], out_dir: Path, no_xlsx: bool = False) -> list[Path]:
    out_dir = Path(out_dir)
    written = [out_dir / "jobs.jsonl", out_dir / "jobs.csv"]
    write_jsonl(jobs, written[0])
    write_csv(jobs, written[1])
    if not no_xlsx:
        write_xlsx(jobs, out_dir / "jobs.xlsx")
        written.append(out_dir / "jobs.xlsx")
    return written
