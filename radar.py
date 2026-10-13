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

# Workday and SmartRecruiters are paged APIs. We read every posting up to a
# cap so very large employers can't stall the daily run.
WORKDAY_MAX_POSTINGS = 3000        # per company
WORKDAY_MAX_DETAIL_LOOKUPS = 400   # "3 Locations" postings we open to see where they are
SMARTRECRUITERS_MAX_POSTINGS = 3000


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
    postings, offset = {}, 0
    while offset < WORKDAY_MAX_POSTINGS:
        page = http_json(f"{api}/jobs", {"appliedFacets": {}, "limit": 20,
                                         "offset": offset, "searchText": ""})
        if page is None:
            if offset == 0:
                return None  # tenant/site wrong
            break
        items = page.get("jobPostings") or []
        for p in items:
            postings.setdefault(p.get("externalPath"), p)
        offset += 20
        if len(items) < 20:
            break
    # Postings in several cities only say "3 Locations"; open those to see where.
    lookups = 0
    for p in postings.values():
        if lookups >= WORKDAY_MAX_DETAIL_LOOKUPS:
            break
        if re.match(r"\d+ Locations?$", p.get("locationsText") or ""):
            lookups += 1
            info = http_json(f"{api}{p['externalPath']}") or {}
            jp = info.get("jobPostingInfo") or {}
            p["_locations"] = [jp.get("location")] + list(jp.get("additionalLocations") or [])
    return {"postings": list(postings.values()), "host": host, "site": site}


def fetch_smartrecruiters(slug: str):
    base = f"https://api.smartrecruiters.com/v1/companies/{slug}/postings"
    found, offset = {}, 0
    while offset < SMARTRECRUITERS_MAX_POSTINGS:
        page = http_json(f"{base}?{urllib.parse.urlencode({'limit': 100, 'offset': offset})}")
        if page is None:
            break
        items = page.get("content") or []
        for p in items:
            found.setdefault(p.get("id"), p)
        offset += 100
        if len(items) < 100:
            break
    if not found:
        return None  # unknown company, or nothing posted
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

# Function: first match wins, so specific phrases come before broad words
# ("Sales Engineer" is sales engineering, not engineering; "Product Marketing"
# is marketing, not product).
FUNCTIONS = [
    ("engineering", rx(r"design verification", r"physical design", r"asic", r"rtl", r"analog design",
                       r"circuit design", r"silicon", r"chip design", r"design engineer(?:ing)?",
                       r"security (?:analyst|operations|researcher|engineer(?:ing)?)", r"soc analyst")),
    ("strategy-ops", rx(r"chief of staff", r"strategy (?:&|and) operations", r"strategic operations",
                        r"business operations", r"bizops", r"revenue operations", r"revops",
                        r"gtm (?:operations|strategy|planning)", r"go[- ]to[- ]market (?:operations|strategy)",
                        r"sales operations", r"sales strategy", r"deal desk", r"sales planning",
                        r"corporate strategy", r"strategy (?:&|and) planning", r"strategic initiatives",
                        r"pricing")),
    ("people", rx(r"recruit(?:er|ing|ment)", r"talent", r"sourcer", r"people", r"human resources",
                  r"hr", r"hrbp", r"total rewards", r"compensation", r"benefits", r"learning (?:&|and) development",
                  r"workplace experience", r"employee experience", r"dei", r"mobility")),
    ("sales-engineering", rx(r"sales engineer(?:ing)?", r"solutions? engineer(?:ing)?", r"solutions? consultant",
                             r"solutions? architect", r"pre[- ]?sales", r"field cto", r"value engineer(?:ing)?",
                             r"technical sales", r"forward deployed")),
    ("marketing", rx(r"marketing", r"cmo", r"demand gen(?:eration)?", r"brand", r"communications",
                     r"comms", r"public relations", r"pr", r"content", r"events?", r"field events",
                     r"digital experiences?", r"web(?:site)? (?:strategy|experience|operations|producer)",
                     r"martech", r"abm", r"lifecycle", r"social media", r"analyst relations",
                     r"creative", r"copywriter", r"storytelling", r"narrative", r"seo", r"community",
                     r"developer relations", r"devrel", r"evangelis[tm]", r"growth marketing",
                     r"customer marketing", r"advocacy", r"market research", r"market intelligence")),
    ("partnerships", rx(r"(?<!business )partner(?:ship)?s?", r"alliances?", r"channel", r"business development(?! rep)",
                        r"bizdev", r"ecosystem", r"marketplace")),
    ("sales", rx(r"account executive", r"ae", r"sales", r"(?<!technical )account manager", r"account director",
                 r"business development representative", r"sales development", r"\w*sdr", r"\w*bdr",
                 r"account development", r"inside sales", r"enterprise account", r"territory",
                 r"commercial (?:lead|director|manager)", r"revenue (?:lead|director)", r"cro",
                 r"chief revenue officer", r"major account", r"strategic account", r"named account")),
    ("customer-success", rx(r"customer success", r"csm", r"customer experience", r"customer support",
                            r"support (?:engineer|specialist|manager|lead|agent|representative)",
                            r"technical support", r"technical account manager", r"tam",
                            r"implementation", r"onboarding", r"professional services",
                            r"customer education", r"customer enablement", r"renewals?",
                            r"customer operations", r"client services", r"engagement manager",
                            r"delivery (?:manager|lead|consultant)", r"consultant")),
    ("legal", rx(r"counsel", r"legal", r"attorney", r"paralegal", r"compliance", r"privacy",
                 r"regulatory", r"contracts?", r"policy", r"government affairs", r"trust (?:&|and) safety")),
    ("finance", rx(r"finance", r"financial", r"fp&a", r"accounting", r"accountant", r"controller",
                   r"tax", r"treasury", r"payroll", r"audit", r"revenue accounting", r"billing",
                   r"procure(?:ment)?", r"cfo", r"investor relations", r"credit", r"risk analyst")),
    ("design", rx(r"designer", r"design", r"ux", r"ui", r"user research(?:er)?", r"researcher, ux",
                  r"illustrat(?:or|ion)", r"motion")),
    ("data", rx(r"data scien(?:ce|tist)", r"data analyst", r"analytics", r"business intelligence", r"bi",
                r"analyst", r"insights", r"statistic(?:ian|s)", r"economist", r"quantitative")),
    ("product", rx(r"product manager", r"product management", r"product lead", r"product owner",
                   r"product operations", r"product ops", r"group product", r"director of product",
                   r"vp,? product", r"head of product", r"chief product", r"product strategy",
                   r"technical program manager", r"tpm", r"product")),
    ("engineering", rx(r"engineer(?:ing)?", r"developer", r"software", r"sre", r"devops", r"architect",
                       r"scientist", r"machine learning", r"ml", r"ai researcher", r"research(?:er)?",
                       r"firmware", r"hardware", r"programmer", r"technical lead", r"cto",
                       r"security (?:analyst|operations|researcher)", r"infrastructure", r"qa",
                       r"quality assurance", r"test")),
    ("it", rx(r"it", r"information technology", r"help ?desk", r"service desk", r"systems administrator",
              r"sysadmin", r"desktop support", r"cio", r"business systems", r"enterprise applications",
              r"salesforce administrator", r"netsuite", r"workday (?:analyst|administrator)")),
    ("operations", rx(r"operations", r"ops", r"program manager", r"project manager", r"supply chain",
                      r"logistics", r"manufacturing", r"facilities", r"workplace", r"office manager",
                      r"executive assistant", r"administrative", r"coordinator", r"planner",
                      r"procurement", r"sourcing", r"fulfillment", r"warehouse", r"technician",
                      r"coo", r"general manager")),
]
FUNCTION_LABELS = {  # shown on the page; order here is the order of the filter chips
    "marketing": "Marketing", "strategy-ops": "Strategy & Ops / RevOps / CoS",
    "sales": "Sales", "sales-engineering": "Sales Engineering", "partnerships": "Partnerships",
    "customer-success": "Customer Success & Support", "product": "Product", "design": "Design",
    "engineering": "Engineering", "data": "Data & Analytics", "it": "IT", "finance": "Finance",
    "people": "People / HR / Recruiting", "legal": "Legal & Compliance", "operations": "Operations",
    "other": "Other",
}

# Seniority: first match wins
SENIORITY = [
    ("intern", rx(r"intern(?:ship)?", r"co[- ]op", r"apprentice", r"new grad(?:uate)?", r"university grad(?:uate)?",
                  r"campus", r"graduate program", r"summer")),
    ("c-level", rx(r"chief (?!of staff)\w+", r"c[a-z]o", r"(?<!vice )(?<!vice-)president(?! of)", r"c[- ]suite", r"founder")),
    ("vp-head", rx(r"vp", r"svp", r"evp", r"avp", r"vice[- ]president", r"head of", r"head", r"general manager", r"gm")),
    ("director", rx(r"director", r"chief of staff")),
]
SENIOR_IC = rx(r"senior", r"sr", r"staff", r"principal", r"lead", r"distinguished", r"fellow", r"expert",
               r"(?:specialist|engineer|consultant|analyst|scientist|manager|representative) (?:iii|iv|v|3|4|5)")
# Below Director despite the word: these rank as manager level
DEMOTE_TO_MANAGER = rx(r"associate director", r"associate (?:creative|art) director", r"assistant director")
# "Manager" that means managing people...
PEOPLE_MANAGER = re.compile(r"^\s*(?:sr\.?\s+|senior\s+)?manager\b|\b(?:senior|sr\.?|group|engineering|people|"
                            r"general)\s+(?:\w+\s+)?manager\b|\bmanager\s*(?:,|of\b|[-–:]|\bii\b|\biii\b)"
                            r"|\bsupervisor\b|\bteam lead\b|\blead,? (?:team|people)\b", re.I)
# ...versus a role name that happens to contain it ("Product Manager", "Customer Success Manager")
ROLE_MANAGER = rx(r"(?:product|program|project|account|success|relationship|partner|territory|case|community|"
                  r"engagement|marketing|campaign|content|brand|events?|channel|alliances?|solutions?|"
                  r"implementation|release|support|office|category|vendor|social media|lifecycle|"
                  r"demand generation|field marketing|operations|enablement|renewals?|onboarding) manager")
SENIORITY_LABELS = {
    "c-level": "C-level", "vp-head": "VP / Head", "director": "Director", "manager": "Manager",
    "senior-ic": "Senior / Staff / Principal IC", "ic": "Individual contributor", "intern": "Intern / New grad",
}

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


def classify_function(title, dept=""):
    for name, pat in FUNCTIONS:
        if pat.search(title):
            return name
    # Generic titles ("Director, Programs"): fall back on the department name
    for name, pat in FUNCTIONS:
        if dept and pat.search(dept):
            return name
    return "other"


REPORTS_TO = re.compile(r"\s+(?:to|for|supporting|reporting to)\s+(?:the\s+|our\s+)?(?:office of the\s+)?"
                        r"(?:co-?)?(?:c[a-z]o|chief|president|founder|vp|svp|evp|head|exec)\b.*$", re.I)


def classify_seniority(title):
    title = REPORTS_TO.sub("", title)  # "Chief of Staff to the CMO" is not itself C-level
    if DEMOTE_TO_MANAGER.search(title):
        return "manager"
    for name, pat in SENIORITY:
        if pat.search(title):
            return name
    if re.search(r"\b(?:manager|mgr|supervisor|team lead)\b", title, re.I):
        # "Senior Product Manager" is a senior individual role; "Manager, Product Marketing" leads people
        if ROLE_MANAGER.search(title) and not re.search(r"\b(?:group|engineering|people)\s+(?:\w+\s+)?manager\b|"
                                                         r"^\s*(?:sr\.?\s+|senior\s+)?manager\b|\bmanager\s+of\b", title, re.I):
            return "senior-ic" if SENIOR_IC.search(title) else "ic"
        if PEOPLE_MANAGER.search(title) or not ROLE_MANAGER.search(title):
            return "manager"
    if SENIOR_IC.search(title):
        return "senior-ic"
    return "ic"


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
            sen = classify_seniority(j["title"])
            regions = classify_regions(j["locations"], j["remote_flag"])
            if not regions:
                continue  # outside Bay Area / West Coast / US-remote
            j.update(fn=fn, sen=sen, regions=sorted(regions),
                     segment=c["segment"], source=c["source"], ats=ats)
            for k in ("remote_flag", "dept"):
                j.pop(k, None)
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
        "functions": FUNCTION_LABELS,
        "seniorities": SENIORITY_LABELS,
        "roles": roles,
    }, separators=(",", ":")))

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
