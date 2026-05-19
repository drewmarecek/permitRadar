# GovRadar-Pipeline

Autonomous scraping + LLM extraction pipeline for U.S. municipal
government websites and PDFs. Targets RFPs, IFBs, building permits,
zoning decisions, and awarded contracts; stores structured leads in
MongoDB.

```
URL ──▶ scraper.py ──▶ extractor.py ──▶ db.py
        (Playwright)   (Claude 3.5)     (Mongo upsert)
```

## Architecture

| File | Role |
| --- | --- |
| `config.py` | Typed env-var loader (`pydantic-settings`) with a cached singleton. |
| `models.py` | Shared Pydantic schemas (`Lead`, `StoredLead`) — single source of truth. |
| `logging_setup.py` | Root logger configuration; human-readable or JSON output. |
| `db.py` | Async `LeadRepository` (motor) with indexes and idempotent upserts. |
| `scraper.py` | Async Playwright scraper with eTRAKiT form automation, HTML cleaner, PDF extraction. |
| `extractor.py` | Anthropic Claude 3.5 Sonnet via **tool-use** for guaranteed JSON. |
| `pipeline.py` | CLI orchestrator: fan-out scrape → extract → store. Supports `--dry-run`. |
| `delivery.py` | CSV export + cold-email Markdown generator for client outreach. |
| `test_pipeline.py` | Offline integration tests (mocked Anthropic, real local Mongo). |

### Why these choices

- **Tool-use over JSON-mode prompting.** We register a `record_leads`
  tool whose `input_schema` is generated from the `Lead` Pydantic
  model and force `tool_choice` to it. Claude can no longer reply in
  prose, which kills the entire class of "model returned almost-JSON"
  parse failures.
- **`motor` async MongoDB driver.** Keeps the event loop free; the
  blocking `pymongo` would serialise everything behind a thread pool.
  `motor` is built on `pymongo` and is officially supported.
- **Idempotent upserts via a compound unique index** on
  `(source_url, project_name)`. Re-running the pipeline refreshes
  `last_seen_at` instead of duplicating rows.
- **HTML boilerplate stripped before the LLM call.** BeautifulSoup
  removes `<script>`, `<style>`, `<noscript>`, and `<svg>` only — we
  keep `<form>`, `<nav>`, etc. because ASP.NET portals (eTRAKiT) wrap
  real content inside them. Token spend is capped by `SCRAPER_MAX_HTML_CHARS`.
- **eTRAKiT-aware scraping.** For URLs containing `/etrakit/`, the
  scraper drives the search form (dropdowns + value input + Search
  click), waits for the results grid, and captures the populated DOM.
- **`tenacity` retries with exponential backoff** around both
  Playwright navigation and the Anthropic call. Handles transient
  timeouts and 5xx/529 from the API.

## Setup

Python 3.10+ is required.

```bash
# 1. Clone & create a venv
python -m venv .venv
source .venv/bin/activate

# 2. Install deps
pip install -r requirements.txt

# 3. Install the Playwright browser
playwright install chromium

# 4. Copy env template and fill in your secrets
cp .env.example .env
# edit .env: set ANTHROPIC_API_KEY and (optionally) MONGODB_URI

# 5. Make sure MongoDB is reachable
#    Local docker quickstart:
#    docker run -d --name govradar-mongo -p 27017:27017 mongo:7
```

## Run

### Single URL

```bash
python pipeline.py https://www.cityof<example>.gov/permits/recent
```

### Batch (newline-delimited file)

```bash
# urls.txt — one URL per line, '#' lines ignored
python pipeline.py --file urls.txt --concurrency 4
```

### Dry-run (no DB writes, no Anthropic spend)

Useful for smoke-testing a new URL list or debugging a scraper change.
Real Playwright fetch happens; the LLM and Mongo writes are stubbed.

```bash
python pipeline.py --dry-run https://example.gov/permits
```

You'll see lines like:

```
[dry-run] skipping Anthropic call for https://example.gov/permits
[dry-run] WOULD upsert: {"project_name": "[DRY-RUN] Synthetic lead ...", ...}
```

Dry-run requires neither `ANTHROPIC_API_KEY` nor a running MongoDB.

### eTRAKiT permit portals

Many U.S. cities use **eTRAKiT** (CentralSquare) for online permits.
The search UI loads on the main page (not in an iframe on most installs)
and requires filling **Search By**, **Operator**, and **Search Value**
before results appear.

When the URL path contains `/etrakit/`, `scraper.py` automatically:

1. Waits for the search form to hydrate in the DOM.
2. Fills the dropdowns and text input (if you pass CLI flags).
3. Clicks **Search** and waits for the results grid or an empty-state message.
4. Captures the populated HTML for Claude extraction.

Non-eTRAKiT URLs ignore these flags entirely.

#### CLI flags

| Flag | Description |
| --- | --- |
| `--etrakit-search-by` | **Search By** dropdown value. Examples: `PERMIT NO`, `SITE ADDRESS`, `CONTRACTOR NAME`, `PARENT PROJECT NO`. Case-insensitive. |
| `--etrakit-search-operator` | **Operator** dropdown value. Examples: `Contains`, `Begins With`, `Equals`, `At Least`, `At Most`. Optional if the portal hides this control. |
| `--etrakit-search-value` | Text typed into the search-value input (e.g. a permit prefix like `B26-`). |

**Defaults:** If you pass `--etrakit-search-value` without the other two flags, the pipeline assumes:

- `search_by` = `PERMIT NO`
- `search_operator` = `Contains`

If you omit all three flags on an eTRAKiT URL, the scraper clicks **Search** with an empty query (works on permissive portals; strict ones like Frisco return "no results").

#### Examples

**Frisco, TX — dry-run with permit-number prefix (recommended first test):**

```bash
python pipeline.py --dry-run \
  "https://etrakit.friscotexas.gov/etrakit/Search/permit.aspx" \
  --etrakit-search-by "PERMIT NO" \
  --etrakit-search-value "B26-"
```

Equivalent shorthand (defaults apply `PERMIT NO` + `Contains`):

```bash
python pipeline.py --dry-run \
  "https://etrakit.friscotexas.gov/etrakit/Search/permit.aspx" \
  --etrakit-search-value "B26-"
```

**Explicit operator:**

```bash
python pipeline.py --dry-run \
  "https://etrakit.friscotexas.gov/etrakit/Search/permit.aspx" \
  --etrakit-search-by "PERMIT NO" \
  --etrakit-search-operator "Contains" \
  --etrakit-search-value "B26-"
```

**Site address search:**

```bash
python pipeline.py --dry-run \
  "https://etrakit.friscotexas.gov/etrakit/Search/permit.aspx" \
  --etrakit-search-by "SITE ADDRESS" \
  --etrakit-search-operator "Begins With" \
  --etrakit-search-value "1280 Oak"
```

**Live run (writes to MongoDB, calls Claude):**

```bash
# Requires ANTHROPIC_API_KEY in .env and a running MongoDB instance.
python pipeline.py \
  "https://etrakit.friscotexas.gov/etrakit/Search/permit.aspx" \
  --etrakit-search-value "B26-"
```

**Batch file — same search applied to every eTRAKiT URL in the list:**

```bash
# urls.txt
# https://etrakit.friscotexas.gov/etrakit/Search/permit.aspx
# https://etrakit.othercity.gov/etrakit/Search/permit.aspx

python pipeline.py --file urls.txt --concurrency 2 \
  --etrakit-search-by "CONTRACTOR NAME" \
  --etrakit-search-value "ACME"
```

On success, logs include lines like:

```
eTRAKiT search params: EtrakitSearchParams(search_by='PERMIT NO', search_operator='Contains', search_value='B26-')
eTRAKiT: parameterised search on https://... (search_by='PERMIT NO' operator='Contains' value='B26-')
eTRAKiT: search results surfaced for https://...
```

The dry-run preview snippet should mention result counts (e.g. `Your search returned 1`) when permits match.

Exit codes:
- `0` — every URL processed successfully
- `1` — at least one URL failed (see logs)
- `2` — usage error or MongoDB unreachable

### Programmatic use

```python
import asyncio
from db import get_repository
from extractor import LeadExtractor
from pipeline import Pipeline
from scraper import EtrakitSearchParams, Scraper


async def main() -> None:
    repo = get_repository()
    await repo.ensure_indexes()
    extractor = LeadExtractor()

    etrakit = EtrakitSearchParams(
        search_by="PERMIT NO",
        search_operator="Contains",
        search_value="B26-",
    )

    async with Scraper() as scraper:
        pipeline = Pipeline(
            scraper, extractor, repo,
            concurrency=2,
            etrakit_search=etrakit,  # ignored for non-eTRAKiT URLs
        )
        outcomes = await pipeline.process_many([
            "https://etrakit.friscotexas.gov/etrakit/Search/permit.aspx",
            "https://example2.gov/permits.pdf",
        ])

    for o in outcomes:
        print(o)

    await extractor.aclose()
    await repo.close()


asyncio.run(main())
```

## Configuration reference

All settings are env vars; see `.env.example`. The most useful knobs:

| Var | Default | Purpose |
| --- | --- | --- |
| `ANTHROPIC_API_KEY` | _required_ | Claude API auth. |
| `ANTHROPIC_MODEL` | `claude-sonnet-4-6` | See [Anthropic models](https://docs.anthropic.com/en/docs/about-claude/models). Avoid retired IDs like `claude-3-5-sonnet-20241022`. |
| `MONGODB_URI` | `mongodb://localhost:27017` | Full connection string. |
| `SCRAPER_HEADLESS` | `true` | Set `false` to watch the browser drive. |
| `SCRAPER_MAX_HTML_CHARS` | `120000` | Hard cap on chars sent to Claude. |
| `LOG_LEVEL` | `INFO` | Use `DEBUG` to see Playwright/Mongo plumbing. |
| `LOG_JSON` | `false` | Set `true` for line-delimited JSON logs. |

## Lead schema

Every record written to `leads` (Mongo collection) conforms to
`models.StoredLead`:

```jsonc
{
  "project_name": "Re-roofing of Fire Station #4",
  "location_address": "1280 Oak Ave, Springfield, IL",
  "estimated_value": 425000.0,        // or "TBD" or null
  "contractor_or_bidder": null,
  "submission_deadline_or_permit_date": "2026-06-12",
  "source_url": "https://springfield.gov/rfp/2026-118",
  "raw_extracted_summary": "City of Springfield issued RFP 2026-118 ...",

  // storage-only metadata (added by db.py)
  "source_host": "springfield.gov",
  "first_seen_at": "2026-05-18T14:02:11.000Z",
  "last_seen_at":  "2026-05-18T14:02:11.000Z"
}
```

## Indexes created on boot

| Name | Keys | Why |
| --- | --- | --- |
| `uniq_source_project` | `(source_url, project_name)` unique | Idempotent upserts. |
| `by_source_host` | `source_host` | Per-jurisdiction queries. |
| `by_deadline` | `submission_deadline_or_permit_date` | "Closing this week" feeds. |
| `by_last_seen_desc` | `last_seen_at DESC` | "What changed today" feeds. |

## Delivering leads to clients

`delivery.py` queries the recent window of `leads` and produces two
artefacts in `./exports/` (configurable):

- `leads_export_YYYY-MM-DD.csv` — schema-stable CSV ready for Sheets,
  Excel, HubSpot, or any CRM importer.
- `outreach_YYYY-MM-DD.md` — a persuasive Markdown cold-email body
  featuring the top N projects from the window.

```bash
# default: last 7 days, both outputs, into ./exports/
python delivery.py

# personalise the email and widen the window
python delivery.py --days 30 \
    --contact-name "Sarah" \
    --sender-name "Drew @ GovRadar" \
    --max-leads 5

# CSV only, into a custom dir, no stdout echo
python delivery.py --no-markdown --output-dir out/ --quiet
```

The two formatters (`export_csv`, `build_outreach_markdown`) are pure
functions of `list[dict]`, so they're trivial to reuse from a future
HTTP endpoint or scheduled job.

## Testing

Offline integration tests live in `test_pipeline.py`. They mock the
Anthropic API entirely (zero token spend) but exercise a real MongoDB
to verify index creation and upsert idempotency.

```bash
# spin up Mongo if you don't have one
docker run -d --name govradar-mongo -p 27017:27017 mongo:7

# run the suite
python test_pipeline.py            # or: python -m unittest test_pipeline -v
```

The DB tests **auto-skip** with a clear message if no Mongo is
reachable, so the extractor tests still run on machines without Docker.
The test process forces `MONGODB_DB=govradar_test` so production data
is never touched.

## Operational notes

- **Token spend.** `SCRAPER_MAX_HTML_CHARS` is your spend dial. Lower
  it before scaling out a new vertical.
- **eTRAKiT empty searches.** A blank **Search** click on strict portals
  returns "no results" — always pass `--etrakit-search-value` (and
  usually `--etrakit-search-by`) for production crawls.
- **eTRAKiT debugging.** On interaction failure the scraper logs the full
  frame inventory and writes `_debug_capture_<host>_<ts>_main.html` to
  the project root for offline inspection.
- **Scanned-image PDFs** raise `ScrapeError` (no extractable text). Add
  an OCR step (e.g. Tesseract) if your sources include those.
- **Sites with bot protection** (Cloudflare/Akamai) may need a stealth
  plugin or a residential proxy; this boilerplate intentionally ships
  vanilla Chromium so behaviour stays predictable.
