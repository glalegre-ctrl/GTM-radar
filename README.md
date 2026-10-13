# GTM Radar (personal edition)

A private version of Christine Castro's GTM Radar for **Marketing, Strategy & Operations, and RevOps** roles at every level in the **Bay Area, West Coast, and US-remote**. You narrow it down on the page. It covers your 100-company tracker plus a broader list of about 160 smaller and mid-size Bay Area / West Coast cyber, AI, data, devtools, GTM-tech and B2B SaaS companies (259 in total).

## How it works

1. `companies.csv` lists the companies you track. The `source` column says whether each one came from your tracker (`tracker`) or the broader list (`broad`).
2. Every morning a GitHub Action runs `radar.py`, which reads each company's **public** job feed. No scraping of LinkedIn or Indeed, no API keys.
3. Each role is tagged by function, seniority and region. Only Marketing, Strategy & Operations / Chief of Staff, and RevOps / Sales & GTM Ops roles in the Bay Area, West Coast or US-remote are kept.
4. Results land in `docs/jobs.json`. `docs/index.html` is the filterable page, served free by GitHub Pages.

Supported job systems: **Greenhouse, Lever, Ashby, Workday, SmartRecruiters**.

## Filters and URLs

The page keeps your filters in the URL, so you can bookmark views:

Functions: Marketing (including Marketing Ops), Strategy & Operations / Chief of Staff, RevOps / Sales & GTM Ops. To track another function, add its key to `FUNCTION_LABELS` in `radar.py`.
Levels: C-level, VP / Head, Director, Manager, Senior / Staff / Principal IC, Individual contributor, Intern / New grad.
"All" (or leaving a row unselected) means no filter on that row. Numbers on each button show how many roles you'd get.

| View | URL ending |
|---|---|
| Default: all three functions, all levels, Bay Area + remote, last 7 days | `index.html` |
| Director and up | `index.html?sen=c-level,vp-head,director` |
| Bay Area only | `index.html?region=bay-area` |
| Whole West Coast, last 30 days | `index.html?region=bay-area,west-coast,remote-us&posted=30` |
| Only companies from your tracker | `index.html?src=tracker` |
| Only the broader list | `index.html?src=broad` |
| Cybersecurity only | `index.html?seg=cyber` |

## How each company's job feed is found

You don't have to look up feed addresses. For each company the script tries, in order:

1. **A known feed** in the `ats` + `slug` columns, or a careers URL that already points at a job system (`boards.greenhouse.io/...`, `jobs.lever.co/...`, `jobs.ashbyhq.com/...`, `*.myworkdayjobs.com/...`, `jobs.smartrecruiters.com/...`).
2. **Your careers page.** If `careers_url` is the company's own page (e.g. `cloudflare.com/careers`), it opens that page and looks for the job system it links to.
3. **The company name.** It tries likely board names on Greenhouse, Lever and Ashby, and rejects a match if the feed names a different company.

Whatever works is written back into `companies.csv` (the Action commits it), so later runs go straight to the right place. Feeds found by name guessing are marked `auto-found; spot-check once` in the notes column and in the page's feed-status list.

**What won't resolve automatically:**
- Careers pages that only load their job list with JavaScript (no link in the page itself).
- Companies on systems not covered here: Oracle Recruiting Cloud (Oracle), Microsoft's own site (LinkedIn, GitHub), iCIMS, Taleo, Eightfold, and Phenom-only portals.

These show up under **Company feed status** at the bottom of the page as "not reading". To fix one, open any job on that company's site, copy the job's URL, and paste it into the `careers_url` column. If it's a Workday, Greenhouse, Lever, Ashby or SmartRecruiters link, it works from the next run. If it's one of the unsupported systems, keep that company in your tracker spreadsheet.

## Setup (about 15 minutes)

1. Create a new GitHub repo (e.g. `gtm-radar`) and upload these files, keeping the folder structure. If the Actions tab shows no workflow afterwards, the hidden `.github` folder was skipped: use **Add file > Create new file**, name it `.github/workflows/refresh.yml`, and paste in the contents of that file.
2. **Settings > Actions > General > Workflow permissions**: choose **Read and write permissions** and save.
3. **Settings > Pages**: Source "Deploy from a branch", branch `main`, folder `/docs`. Save.
4. **Actions tab**: open "Refresh GTM Radar" and click **Run workflow**. The first run takes longer (a few minutes) because it is finding every company's feed.
5. Open `https://<your-username>.github.io/gtm-radar/` and bookmark your preferred view.

Privacy note: on a free GitHub account, Pages requires a **public** repo, which means your company list is visible to anyone. If that matters, use a paid plan (private repo with Pages), or keep the repo private and run it locally (below).

## Running it on your computer instead

```
python3 radar.py --check            # which feeds resolve
python3 radar.py                    # pull jobs into docs/jobs.json
python3 -m http.server -d docs 8000 # then open http://localhost:8000
```

## Adding companies later

- **One company:** add a row to `companies.csv` with just `name`, `segment`, and `source` set. Add `careers_url` if you have it.
- **A whole spreadsheet** (CSV or Excel), e.g. an updated tracker:
  ```
  pip install openpyxl                          # once, for .xlsx files
  python3 import_tracker.py my_tracker.xlsx
  ```
  It finds the Company and Careers URL columns on its own (title rows above the header are fine), tags rows as `tracker`, keeps your priority and focus text in `notes`, and never deletes anything.

## Tuning the filters

All matching rules are near the top of the classification section in `radar.py`:
- `FUNCTIONS`: title words for each function. First match wins, so specific phrases sit above broad words ("Sales Engineer" before "Engineer", "Product Marketing" before "Product")
- `SENIORITY`, `ROLE_MANAGER`, `PEOPLE_MANAGER`: level rules. "Product Manager" or "Customer Success Manager" counts as an individual role; "Manager, Product Marketing" or "Senior Manager" counts as Manager. "Associate Director" counts as Manager; "Chief of Staff" counts as Director; "...to the CEO" doesn't make a title C-level
- `BAY_AREA`, `WEST_COAST`: city lists
- `SEARCH_KEYWORDS`: what large Workday and SmartRecruiters employers are searched for (they aren't downloaded in full, which keeps the run quick)

## Testing offline

```
python3 radar.py --fixtures tests/fixtures --companies tests/companies_test.csv
```
Fixtures include real Samsara listings plus `[TEST]` rows covering every job system and each way of finding a feed. Delete the resulting `docs/jobs.json` and `data/seen.json` before your first real run so test rows don't linger.
