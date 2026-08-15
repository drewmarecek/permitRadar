# GovRadar

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

## How to run

```bash
python pipeline.py https://www.cityof<example>.gov/permits/recent
```
## Notes

- **Token spend.** `SCRAPER_MAX_HTML_CHARS` is your spend dial. Lower
  it before scaling out a new vertical.

