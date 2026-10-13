#!/usr/bin/env python3
"""
Merge your company tracker into companies.csv.

  python import_tracker.py my_tracker.csv
  python import_tracker.py my_tracker.xlsx      # needs: pip install openpyxl

It looks for a company-name column (Company, Name, Company Name, Employer...)
and, if present, a careers-URL column (Careers URL, Careers Page, Jobs URL,
URL, Link...). Rows already in companies.csv are updated in place: your
careers URL wins, and the row is tagged source=tracker. New companies are
appended. Nothing is deleted.
"""
import csv
import re
import sys
from pathlib import Path

ROOT = Path(__file__).parent
COMPANIES = ROOT / "companies.csv"
FIELDS = ["name", "careers_url", "ats", "slug", "segment", "source", "notes"]

NAME_COLS = re.compile(r"^(company( name)?|name|employer|organization|org|account)$", re.I)
CAREER_COLS = re.compile(r"(careers?|jobs?)( page| site)?( url| link)?$", re.I)
URL_COLS = re.compile(r"^(url|link)$", re.I)   # fallback only; never the plain Website column
SEG_COLS = re.compile(r"^(segment|industry|sector|category|vertical|focus.*)$", re.I)
PRI_COLS = re.compile(r"^priority$", re.I)

# Turn free-text focus ("Cybersecurity", "Enterprise data platform") into a segment
SEGMENT_RULES = [
    ("cyber", r"secur|identity|firewall|threat"),
    ("ai", r"\bai\b|machine learning|llm"),
    ("data-infra", r"data|database|analytics|observab|stream|storage|backup"),
    ("semis-infra", r"semicon|chip|eda|network|data cent|infrastructure"),
    ("devtools", r"dev|developer|incident"),
    ("gtm-tech", r"marketing|crm|revenue|account-based|customer|experience|survey"),
    ("fintech", r"payment|financ|spend|insurance|wealth|asset management|payroll|equity"),
]


def to_segment(text):
    t = (text or "").lower()
    if t in {"cyber", "ai", "data-infra", "devtools", "gtm-tech", "b2b-saas", "fintech", "semis-infra"}:
        return t
    for seg, pat in SEGMENT_RULES:
        if re.search(pat, t):
            return seg
    return "b2b-saas" if t else ""


def read_rows(path: Path):
    if path.suffix.lower() in (".xlsx", ".xlsm"):
        import openpyxl
        ws = openpyxl.load_workbook(path, read_only=True, data_only=True).active
        grid = [["" if v is None else str(v).strip() for v in r] for r in ws.iter_rows(values_only=True)]
    else:
        with path.open(newline="", encoding="utf-8-sig") as f:
            grid = [[c.strip() for c in r] for r in csv.reader(f)]
    # The header is the first row that has a company-name column (titles above it are skipped)
    for i, row in enumerate(grid[:15]):
        if any(NAME_COLS.search(c) for c in row if c):
            header = row
            return header, [dict(zip(header, r)) for r in grid[i + 1:]]
    sys.exit("Couldn't find a header row with a Company/Name column in the first 15 rows.")


def pick(header, pat):
    return next((h for h in header if pat.search(h)), None)


SUFFIX = re.compile(r"\b(inc|corp|corporation|co|llc|ltd|software|systems|networks|"
                    r"communications|technologies|technology|labs|hq)\b\.?", re.I)


def key(name):
    """'CrowdStrike (major CA hub)' and 'Zoom Communications' match 'CrowdStrike' and 'Zoom'."""
    name = re.sub(r"\(.*?\)", "", name).split("/")[0]
    return re.sub(r"[^a-z0-9]", "", SUFFIX.sub("", name.lower()))


def main(src):
    header, rows = read_rows(Path(src))
    ncol = pick(header, NAME_COLS)
    ucol = pick(header, CAREER_COLS) or pick(header, URL_COLS)
    scol, pcol = pick(header, SEG_COLS), pick(header, PRI_COLS)
    if not ncol:
        sys.exit(f"Couldn't find a company-name column in: {header}")
    print(f"Using name column '{ncol}'" + (f", careers URL column '{ucol}'" if ucol else ", no careers URL column"))

    existing = list(csv.DictReader(COMPANIES.open(newline="", encoding="utf-8"))) if COMPANIES.exists() else []
    by_key = {key(r["name"]): r for r in existing}
    added = updated = 0
    for r in rows:
        name = (r.get(ncol) or "").strip()
        if not name:
            continue
        url = (r.get(ucol) or "").strip() if ucol else ""
        focus = (r.get(scol) or "").strip() if scol else ""
        seg = to_segment(focus)
        pri = (r.get(pcol) or "").strip() if pcol else ""
        note = "; ".join(filter(None, [f"priority {pri}" if pri else "", focus]))
        row = by_key.get(key(name))
        if row:
            was_broad = row.get("source") == "broad"
            row["source"] = "tracker"
            row["name"] = name  # use your tracker's naming
            if url and url != row.get("careers_url") and not row.get("slug"):
                row["careers_url"] = url  # keep any feed already confirmed
            if seg and (not row.get("segment") or was_broad):
                row["segment"] = seg
            if note and not row.get("notes"):
                row["notes"] = note
            updated += 1
        else:
            row = {"name": name, "careers_url": url, "ats": "", "slug": "",
                   "segment": seg, "source": "tracker", "notes": note}
            existing.append(row)
            by_key[key(name)] = row
            added += 1

    with COMPANIES.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(existing)
    print(f"{added} added, {updated} already listed (now tagged tracker). {len(existing)} companies total.")
    print("Next: python radar.py --check   to see which feeds resolve.")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    main(sys.argv[1])
