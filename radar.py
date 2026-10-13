#!/usr/bin/env python3
"""
GTM Radar (personal edition)

Pulls open roles from the public job feeds behind most company careers pages
(Greenhouse, Lever, Ashby, Workday, SmartRecruiters), tags each role by function,
seniority and region, and writes docs/jobs.json for the filterable page.

Usage:
  python radar.py                      # live run against companies.csv
  python radar.py --fixtures tests/fixtures --companies tests/companies_test.csv
  python radar.py --check              # just verify every company's board slug

No API keys needed. Standard library only.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).parent
COMPANIES = ROOT / "companies.csv"
OUT = ROOT / "docs" / "jobs.json"
STATE = ROOT / "data" / "seen.json"

UA = {"User-Agent": "personal-gtm-radar/1.0", "Accept": "application/json"}

ENDPOINTS = {
    "greenhouse": "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs",
    "lever": "https://api.lever.co/v0/postings/{slug}?mode=json",
    "ashby": "https://api.ashbyhq.com/posting-api/job-board/{slug}",
}
SIMPLE_ATS = list(ENDPOINTS)

# Workday has no single board API, but every Workday careers site exposes the
# same JSON search endpoint its own page uses. We search a few GTM keywords
# rather than paging through thousands of engineering roles.
WORKDAY_SEARCHES = ["marketing", "communications", "brand", "chief of staff",
                    "go-to-market", "revenue operations", "growth"]
WORKDAY_MAX_PER_SEARCH = 200


# --------------------------------------------------------------------------
# Reading a careers URL into (ats, slug)
# --------------------------------------------------------------------------
URL_PATTERNS = [
    ("greenhouse", re.compile(r"(?:boards|job-boards)(?:\.eu)?\.greenhouse\.io/(?:embed/job_board\?for=)?([\w-]+)", re.I)),
    ("greenhouse", re.compile(r"boards-api\.greenhouse\.io/v1/boards/([\w-]+)", re.I)),
    ("lever", re.compile(r"jobs\.(?:eu\.)?lever\.co/([\w.-]+)", re.I)),
    ("ashby", re.compile(r"jobs\.ashbyhq\.com/([\w.%-]+)", re.I)),
    # https://acme.wd5.myworkdayjobs.com/en-US/Acme_Careers  ->  slug "acme|wd5|Acme_Careers"
    ("workday", re.compile(r"([\w-]+)\.(wd\d+)\.myworkdayjobs\.com/(?:[a-z]{2}-[A-Z]{2}/)?([\w-]+)", re.I)),
    ("smartrecruiters", re.compile(r"(?:jobs|careers)\.smartrecruiters\.com/([\w-]+)", re.I)),
    ("smartrecruiters", re.compile(r"api\.smartrecruiters\.com/v1/companies/([\w-]+)", re.I)),
]
# Ignore links that are really Workday's own marketing or generic pages
BAD_SLUGS = {"embed", "jobs", "job", "careers", "v1", "api", "static", "assets", "wday", "en-us"}


def parse_careers_url(url: str):
    for ats, pat in URL_PATTERNS:
        for m in pat.finditer(url or ""):
            if ats == "workday":
                if m.group(3).lower() in {"wday", "static", "assets"}:
                    continue
                return ats, "|".join(m.groups())
            if m.group(1).lower() in BAD_SLUGS:
                continue
            return ats, m.group(1)
    return None, None


def http_text(url: str):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (personal-gtm-radar)"})
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.geturl(), r.read(2_000_000).decode("utf-8", "replace")
    except Exception:
        return None, ""


def discover_from_page(url: str, fixtures: Path | None):
    """Open a company's own careers page and look for the job system it links to."""
    if fixtures:
        f = fixtures / ("page_" + re.sub(r"[^a-z0-9]+", "_", url.lower()).strip("_") + ".html")
        final, html = url, (f.read_text() if f.exists() else "")
    else:
        final, html = http_text(url)
    # The page may itself redirect to the job board
    return parse_careers_url(final or "") if parse_careers_url(final or "")[0] else parse_careers_url(html)


def slug_candidates(name: str):
    """Likely board names for a company we only know by name."""
    name = re.sub(r"\(.*?\)", "", name).split("/")[0]  # "Splunk (part of Cisco)" -> "Splunk"
    name = re.sub(r"\b(inc|corp|corporation|co|llc|ltd)\.?$", "", name.strip(), flags=re.I)
    base = re.sub(r"[^a-z0-9 ]", "", name.lower()).strip()
    words = base.split()
    joined = "".join(words)
    cands = [joined, "-".join(words)]
    drop = {"inc", "ai", "security", "labs", "technologies", "technology", "software", "systems", "hq"}
    core = [w for w in words if w not in drop]
    if core and core != words:
        cands.append("".join(core))
    cands += [joined + "hq", joined + "inc"]
    seen, out = set(), []
    for c in cands:
        if c and c not in seen:
            seen.add(c); out.append(c)
    return out


# --------------------------------------------------------------------------
# Fetching
# --------------------------------------------------------------------------
def http_json(url: str, body: dict | None = None, retries: int = 2):
    data = json.dumps(body).encode() if body is not None else None
    headers = dict(UA, **({"Content-Type": "application/json"} if body is not None else {}))
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, data=data, headers=headers)
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code in (400, 404, 422):
                return None  # wrong slug or not on this ATS
            if attempt == retries:
                raise
        except (urllib.error.URLError, TimeoutError):
            if attempt == retries:
                raise
        time.sleep(1.5 * (attempt + 1))


def fetch_workday(slug: str):
    tenant, wd, site = slug.split("|")
    host = f"https://{tenant}.{wd}.myworkdayjobs.com"
    api = f"{host}/wday/cxs/{tenant}/{site}"
    postings = {}
    for kw in WORKDAY_SEARCHES:
        offset = 0
        while offset < WORKDAY_MAX_PER_SEARCH:
            page = http_json(f"{api}/jobs", {"appliedFacets": {}, "limit": 20,
                                             "offset": offset, "searchText": kw})
            if page is None:
                if kw == WORKDAY_SEARCHES[0] and offset == 0:
                    return None  # tenant/site wrong
                break
            items = page.get("jobPostings") or []
            for p in items:
                postings.setdefault(p.get("externalPath"), p)
            offset += 20
            if len(items) < 20:
                break
    # Postings in several cities only say "3 Locations"; look those up, but
    # only for titles that could pass our filters (keeps calls low).
    for p in postings.values():
        t = p.get("title") or ""
        if re.match(r"\d+ Locations?$", p.get("locationsText") or "") and \
                classify_function(t, "") and classify_seniority(t):
            info = http_json(f"{api}{p['externalPath']}") or {}
            jp = info.get("jobPostingInfo") or {}
            p["_locations"] = [jp.get("location")] + list(jp.get("additionalLocations") or [])
    return {"postings": list(postings.values()), "host": host, "site": site}


def fetch_smartrecruiters(slug: str):
    base = f"https://api.smartrecruiters.com/v1/companies/{slug}/postings"
    found = {}
    for kw in WORKDAY_SEARCHES:  # same GTM keywords
        offset = 0
        while offset < 400:
            q = urllib.parse.urlencode({"q": kw, "limit": 100, "offset": offset})
            page = http_json(f"{base}?{q}")
            if page is None:
                return None if not found else {"slug": slug, "postings": list(found.values())}
            items = page.get("content") or []
            for p in items:
                found.setdefault(p.get("id"), p)
            offset += 100
            if len(items) < 100:
                break
    if not found:  # confirm the company exists at all
        probe = http_json(f"{base}?limit=1")
        if not probe or not probe.get("totalFound"):
            return None
    return {"slug": slug, "postings": list(found.values())}


def try_board(ats: str, slug: str, fixtures: Path | None):
    if fixtures:
        f = fixtures / f"{ats}_{slug.replace('|', '_')}.json"
        return json.loads(f.read_text()) if f.exists() else None
    if ats == "workday":
        return fetch_workday(slug)
    if ats == "smartrecruiters":
        return fetch_smartrecruiters(slug)
    data = http_json(ENDPOINTS[ats].format(slug=slug))
    if ats == "lever" and isinstance(data, list) and not data:
        return None  # Lever answers [] even for unknown slugs; treat as not found
    if ats == "greenhouse" and isinstance(data, dict) and "jobs" not in data:
        return None
    return data


def fetch_company(c: dict, fixtures: Path | None):
    """Return (ats, slug, raw) or (None, None, None)."""
    trusted = None
    if c["slug"]:
        tries = [(c["ats"], c["slug"])] if c["ats"] in NORMALIZERS else [(a, c["slug"]) for a in SIMPLE_ATS]
    else:
        tries, trusted = [], None
        if c["careers_url"]:  # a company careers page: find the job system it links to
            ats, slug = discover_from_page(c["careers_url"], fixtures)
            if ats:
                trusted = (ats, slug)
                tries.append(trusted)
        # then guess board names across the three common systems
        tries += [(a, s) for s in slug_candidates(c["name"]) for a in SIMPLE_ATS]
    for ats, slug in tries:
        data = try_board(ats, slug, fixtures)
        if data is None:
            continue
        if not c["slug"] and (ats, slug) != trusted and not looks_like(c["name"], ats, data):
            continue  # a guessed board name landed on a different company
        return ats, slug, data
    return None, None, None


def looks_like(name: str, ats: str, data) -> bool:
    """Sanity check for guessed boards, using whatever company name the feed exposes."""
    name = re.sub(r"\(.*?\)", "", name)
    key = re.sub(r"[^a-z0-9]", "", name.lower())[:6]
    if ats == "greenhouse":
        names = {j.get("company_name") for j in data.get("jobs", []) if j.get("company_name")}
    elif ats in ("workday", "smartrecruiters"):
        return True
    elif ats == "lever":
        names = {urllib.parse.urlparse(j.get("hostedUrl", "")).path.split("/")[1] for j in data}
    else:
        names = {j.get("jobUrl", "").split("/")[3] for j in data.get("jobs", []) if j.get("jobUrl")}
    names = {re.sub(r"[^a-z0-9]", "", (n or "").lower()) for n in names}
    return not names or any(key in n or n[:6] in re.sub(r"[^a-z0-9]", "", name.lower()) for n in names if n)


# --------------------------------------------------------------------------
# Normalizing each ATS into one shape
# --------------------------------------------------------------------------
def norm_greenhouse(company, data):
    for j in data.get("jobs", []):
        family = " ".join(
            str(m.get("value") or "") for m in j.get("metadata") or []
            if isinstance(m.get("value"), str)
        )
        yield {
            "company": company,
            "title": (j.get("title") or "").strip(),
            "locations": [s.strip() for s in (j.get("location") or {}).get("name", "").split(";") if s.strip()],
            "dept": family,
            "remote_flag": False,
            "posted": j.get("first_published") or j.get("updated_at"),
            "url": j.get("absolute_url"),
            "id": f"gh-{j.get('id')}",
        }


def norm_lever(company, data):
    for j in data or []:
        cat = j.get("categories") or {}
        locs = cat.get("allLocations") or [cat.get("location")]
        created = j.get("createdAt")
        yield {
            "company": company,
            "title": (j.get("text") or "").strip(),
            "locations": [l for l in locs if l],
            "dept": " ".join(filter(None, [cat.get("department"), cat.get("team")])),
            "remote_flag": (j.get("workplaceType") or "").lower() == "remote",
            "posted": datetime.fromtimestamp(created / 1000, timezone.utc).isoformat() if created else None,
            "url": j.get("hostedUrl"),
            "id": f"lv-{j.get('id')}",
        }


def norm_ashby(company, data):
    for j in data.get("jobs", []):
        if j.get("isListed") is False:
            continue
        locs = [j.get("location")] + [s.get("location") for s in j.get("secondaryLocations") or []]
        yield {
            "company": company,
            "title": (j.get("title") or "").strip(),
            "locations": [l for l in locs if l],
            "dept": " ".join(filter(None, [j.get("department"), j.get("team")])),
            "remote_flag": (j.get("workplaceType") or "").lower() == "remote",
            "posted": j.get("publishedAt"),
            "url": j.get("jobUrl"),
            "id": f"ab-{j.get('id')}",
        }


def workday_posted(text, now=None):
    """'Posted 3 Days Ago' -> ISO date. Workday only gives relative text."""
    now = now or datetime.now(timezone.utc)
    t = (text or "").lower()
    if "today" in t:
        days = 0
    elif "yesterday" in t:
        days = 1
    else:
        m = re.search(r"(\d+)\+?\s*day", t)
        if not m:
            return None
        days = int(m.group(1)) + (1 if "+" in t else 0)
    return (now - timedelta(days=days)).isoformat(timespec="seconds")


def norm_workday(company, data):
    host, site = data["host"], data["site"]
    for p in data.get("postings", []):
        locs = p.get("_locations") or [p.get("locationsText")]
        path = p.get("externalPath") or ""
        yield {
            "company": company,
            "title": (p.get("title") or "").strip(),
            "locations": [l for l in locs if l],
            "dept": "",
            "remote_flag": "remote" in (p.get("remoteType") or "").lower(),
            "posted": workday_posted(p.get("postedOn")),
            "url": f"{host}/{site}{path}",
            "id": "wd-" + (path.rsplit("_", 1)[-1] if "_" in path else path),
        }


def norm_smartrecruiters(company, data):
    slug = data["slug"]
    for p in data.get("postings", []):
        loc = p.get("location") or {}
        where = loc.get("fullLocation") or ", ".join(filter(None, [loc.get("city"), loc.get("region"), loc.get("country", "").upper()]))
        yield {
            "company": company,
            "title": (p.get("name") or "").strip(),
            "locations": [where] if where else [],
            "dept": " ".join(filter(None, [(p.get("department") or {}).get("label"), (p.get("function") or {}).get("label")])),
            "remote_flag": bool(loc.get("remote")) and (loc.get("country") or "").lower() in ("us", ""),
            "posted": p.get("releasedDate"),
            "url": f"https://jobs.smartrecruiters.com/{slug}/{p.get('id')}",
            "id": f"sr-{p.get('id')}",
        }


NORMALIZERS = {"greenhouse": norm_greenhouse, "lever": norm_lever, "ashby": norm_ashby,
               "workday": norm_workday, "smartrecruiters": norm_smartrecruiters}


# --------------------------------------------------------------------------
# Classification: function, seniority, region
# --------------------------------------------------------------------------
def rx(*words):
    return re.compile(r"\b(?:" + "|".join(words) + r")\b", re.I)

FUNCTIONS = [
    # order matters: first match wins
    ("strategy-ops", rx(r"chief of staff", r"strategy (?:&|and) operations", r"gtm strategy",
                        r"go[- ]to[- ]market strategy", r"revenue operations", r"revops",
                        r"gtm operations", r"business operations")),
    ("marketing", rx(r"marketing", r"cmo", r"demand gen(?:eration)?", r"growth",
                     r"brand", r"communications", r"comms", r"public relations", r"pr",
                     r"content", r"field events?", r"events", r"digital experiences?",
                     r"web(?:site)? (?:strategy|experience|operations)", r"martech",
                     r"abm", r"lifecycle", r"social media", r"analyst relations",
                     r"creative", r"storytelling", r"narrative", r"go[- ]to[- ]market")),
]
# Titles that contain a marketing-ish word but are not marketing jobs
NOT_FUNCTION = rx(r"engineer(?:ing)?", r"account executive", r"sales engineer", r"recruit(?:er|ing)",
                  r"developer", r"data scientist", r"designer", r"growth engineer",
                  r"product manager", r"accountant", r"counsel", r"intern(?:ship)?",
                  r"representative", r"\w*sdr", r"\w*bdr")

SENIORITY = [
    ("c-level", rx(r"chief", r"cmo", r"cgo", r"cro", r"c[- ]suite")),
    ("vp-head", rx(r"vp", r"vice president", r"svp", r"evp", r"head of", r"general manager")),
    ("director", rx(r"director", r"sr\.? director", r"senior director")),
]
# "Associate Creative Director", "Associate Director" are usually below Director
DEMOTE = rx(r"associate (?:creative )?director", r"art director", r"creative director")

BAY_AREA = rx(
    r"san francisco", r"sf", r"sf bay area", r"bay area", r"silicon valley", r"south bay",
    r"peninsula", r"east bay", r"redwood city", r"menlo park", r"palo alto", r"mountain view",
    r"sunnyvale", r"santa clara", r"san jose", r"cupertino", r"san mateo", r"foster city",
    r"burlingame", r"san carlos", r"belmont", r"san bruno", r"south san francisco",
    r"brisbane, ca", r"millbrae", r"los altos", r"milpitas", r"fremont", r"campbell",
    r"los gatos", r"saratoga", r"oakland", r"berkeley", r"emeryville", r"alameda",
    r"pleasanton", r"dublin, ca", r"walnut creek", r"san ramon", r"hayward", r"union city",
    r"half moon bay", r"woodside", r"atherton", r"san rafael", r"mill valley", r"sausalito",
)
WEST_COAST = rx(
    r"california", r"ca", r"los angeles", r"la", r"santa monica", r"irvine", r"san diego",
    r"sacramento", r"pasadena", r"culver city", r"orange county", r"long beach", r"santa barbara",
    r"oregon", r"(?<=, )or", r"portland", r"beaverton", r"washington state", r"wa", r"seattle",
    r"bellevue", r"redmond", r"kirkland", r"tacoma", r"pacific time", r"pst", r"west coast",
    r"us west", r"west us",
)
REMOTE = rx(r"remote", r"anywhere", r"distributed", r"virtual", r"work from home", r"wfh")
US_HINT = rx(r"us", r"usa", r"u\.s\.", r"united states", r"north america", r"americas")
NON_US = rx(r"canada", r"toronto", r"vancouver", r"montreal", r"uk", r"united kingdom", r"london",
            r"emea", r"europe", r"germany", r"france", r"dach", r"india", r"bengaluru",
            r"bangalore", r"apac", r"singapore", r"australia", r"sydney", r"japan", r"tokyo",
            r"israel", r"tel aviv", r"mexico", r"brazil", r"latam", r"ireland", r"dublin(?!, ca)",
            r"netherlands", r"amsterdam", r"spain", r"poland")


def classify_function(title, dept):
    if NOT_FUNCTION.search(title) and not re.search(r"marketing", title, re.I):
        return None
    for name, pat in FUNCTIONS:
        if pat.search(title):
            return name
    # Fall back on department only for generic titles like "Director, Programs"
    if re.search(r"\bmarketing\b", dept or "", re.I) and not NOT_FUNCTION.search(title):
        return "marketing"
    return None


def classify_seniority(title):
    if DEMOTE.search(title):
        return None
    if re.search(r"\bchief of staff\b", title, re.I):
        return "director"  # senior, but not a C-level seat
    for name, pat in SENIORITY:
        if pat.search(title):
            return name
    return None


def classify_regions(locations, remote_flag):
    """Return a set of region tags: bay-area, west-coast, remote-us."""
    tags = set()
    for loc in locations or []:
        is_remote = bool(REMOTE.search(loc))
        if BAY_AREA.search(loc):
            tags.update({"bay-area", "west-coast"})
            if is_remote:
                tags.add("remote-us")  # e.g. "Remote - SF Bay Area"
        elif WEST_COAST.search(loc) and not NON_US.search(loc):
            tags.add("west-coast")
            if is_remote:
                tags.add("remote-us")
        elif is_remote and (US_HINT.search(loc) or not NON_US.search(loc)) and not NON_US.search(loc):
            tags.add("remote-us")
    if remote_flag and not any(NON_US.search(l) for l in locations or []):
        tags.add("remote-us")
    return tags


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
FIELDS = ["name", "careers_url", "ats", "slug", "segment", "source", "notes"]


def load_companies(path=None):
    with (path or COMPANIES).open(newline="", encoding="utf-8") as f:
        rows = [r for r in csv.DictReader(f)
                if (r.get("name") or "").strip() and not r["name"].startswith("#")]
    for r in rows:
        for k in FIELDS:
            r[k] = (r.get(k) or "").strip()
        r["ats"] = r["ats"].lower()
        if not r["slug"] and r["careers_url"]:
            ats, slug = parse_careers_url(r["careers_url"])
            if ats:
                r["ats"], r["slug"] = ats, slug
    return rows


def save_companies(rows, path):
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def run(fixtures: Path | None, check_only: bool, companies_path: Path | None = None):
    companies = load_companies(companies_path)
    now = datetime.now(timezone.utc)

    def work(c):
        try:
            ats, slug, data = fetch_company(c, fixtures)
            return c, ats, slug, data, None
        except Exception as e:  # network, JSON, etc.
            return c, None, None, None, f"{type(e).__name__}: {e}"

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(work, companies))

    status, roles, learned = [], [], 0
    for c, ats, slug, data, err in results:
        if err or not ats:
            note = err or ("no public board found; add the careers URL" if not c["slug"]
                           else "board not found; check the slug or careers URL")
            status.append({"company": c["name"], "slug": c["slug"], "ok": False, "note": note,
                           "source": c["source"]})
            continue
        if (c["ats"], c["slug"]) != (ats, slug):
            if not c["slug"]:
                c["notes"] = "auto-found; spot-check once"
            c["ats"], c["slug"] = ats, slug  # remember what worked
            learned += 1
        all_jobs = list(NORMALIZERS[ats](c["name"], data))
        status.append({"company": c["name"], "slug": slug, "ats": ats, "ok": True,
                       "open_roles": len(all_jobs), "source": c["source"],
                       "guessed": c["notes"].startswith("auto-found")})
        if check_only:
            continue
        for j in all_jobs:
            fn = classify_function(j["title"], j["dept"])
            if not fn:
                continue
            sen = classify_seniority(j["title"])
            if not sen:
                continue
            regions = classify_regions(j["locations"], j["remote_flag"])
            if not regions:
                continue  # outside Bay Area / West Coast / US-remote
            j.update(fn=fn, sen=sen, regions=sorted(regions),
                     segment=c["segment"], source=c["source"], ats=ats)
            j.pop("remote_flag", None)
            roles.append(j)

    # Write discovered board names back so tomorrow's run goes straight there
    if learned and not fixtures:
        save_companies(companies, companies_path or COMPANIES)
        print(f"Saved {learned} discovered board name(s) to {(companies_path or COMPANIES).name}")

    if check_only:
        for s in status:
            mark = "OK " if s["ok"] else "-- "
            extra = f"{s.get('ats')}: {s.get('open_roles')} open" if s["ok"] else s["note"]
            print(f"{mark}{s['company']:<30} {s['slug'] or '(name only)':<26} {extra}")
        ok = sum(1 for s in status if s["ok"])
        print(f"\n{ok}/{len(status)} companies have a readable job feed")
        return

    # first_seen tracking so you can see what's new since your last look
    seen = json.loads(STATE.read_text()) if STATE.exists() else {}
    today = now.date().isoformat()
    for r in roles:
        r["first_seen"] = seen.setdefault(r["id"], today)
    live_ids = {r["id"] for r in roles}
    seen = {k: v for k, v in seen.items() if k in live_ids}  # drop closed roles
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(seen, indent=0, sort_keys=True))

    roles.sort(key=lambda r: r.get("posted") or "", reverse=True)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({
        "generated": now.isoformat(timespec="seconds"),
        "companies_tracked": len(companies),
        "companies_ok": sum(1 for s in status if s["ok"]),
        "status": status,
        "roles": roles,
    }, indent=1))

    bad = [s for s in status if not s["ok"]]
    print(f"{len(roles)} matching roles from {len(companies) - len(bad)}/{len(companies)} companies -> {OUT}")
    for s in bad:
        print(f"  ! {s['company']} ({s['slug']}): {s['note']}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--fixtures", type=Path, help="read saved API JSON instead of calling the APIs")
    ap.add_argument("--check", action="store_true", help="only verify each company's board")
    ap.add_argument("--companies", type=Path, help="alternate companies CSV (default companies.csv)")
    a = ap.parse_args()
    run(a.fixtures, a.check, a.companies)
