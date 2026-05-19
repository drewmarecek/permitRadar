"""Client-delivery utilities.

Queries the `leads` collection over a recent time window and produces
two artefacts:

  1. `leads_export_YYYY-MM-DD.csv` — flat CSV mirroring the Pydantic
     `Lead` schema plus storage metadata. Ready to drop into Excel,
     Sheets, HubSpot, or a CRM importer.
  2. `outreach_YYYY-MM-DD.md` — a persuasive Markdown summary block
     suitable for pasting into a cold-email tool.

Both formatters are pure functions of `list[dict]`, so they're trivial
to unit-test and can be reused by an HTTP delivery endpoint later.

CLI examples
------------

    # last 7 days, both outputs, default contact name
    python delivery.py

    # last 30 days, into a custom dir, customising the email body
    python delivery.py --days 30 --output-dir out/ \
        --contact-name "Sarah" --sender-name "Drew @ GovRadar"

    # CSV only
    python delivery.py --no-markdown
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from pymongo import DESCENDING

from db import LeadRepository, get_repository
from logging_setup import configure_logging, get_logger

logger = get_logger(__name__)


# CSV column order is intentionally schema-stable so downstream pipelines
# (Sheets imports, CRM mappings) don't break when we add fields. New
# fields should be appended to the end of this list, never inserted.
CSV_FIELDS: list[str] = [
    "project_name",
    "location_address",
    "estimated_value",
    "contractor_or_bidder",
    "submission_deadline_or_permit_date",
    "source_url",
    "source_host",
    "first_seen_at",
    "last_seen_at",
    "raw_extracted_summary",
]


# ---- Mongo queries -----------------------------------------------------


async def fetch_recent_leads(repo: LeadRepository, *, days: int) -> list[dict[str, Any]]:
    """Return leads whose `last_seen_at` falls within the past `days`.

    Sorted newest-first so the outreach Markdown leads with the freshest
    project. `last_seen_at` (not `first_seen_at`) is the right anchor:
    it reflects the most recent run that *confirmed* the lead.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    # Reach across the repository to issue a custom query. Acceptable
    # here because `delivery.py` is the canonical reporting consumer;
    # add a `repo.recent(days)` helper if a second consumer appears.
    cursor = (
        repo._collection
        .find({"last_seen_at": {"$gte": cutoff}})
        .sort("last_seen_at", DESCENDING)
    )
    return [doc async for doc in cursor]


# ---- CSV export --------------------------------------------------------


def _coerce_csv_value(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, datetime):
        return v.isoformat()
    return str(v)


def export_csv(leads: Iterable[dict[str, Any]], output_path: Path) -> int:
    """Write leads to CSV. Returns the number of rows written.

    `extrasaction="ignore"` ensures Mongo's `_id` (and any future
    fields we haven't whitelisted) never silently leak into client
    deliverables.
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with output_path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for lead in leads:
            writer.writerow({f: _coerce_csv_value(lead.get(f)) for f in CSV_FIELDS})
            written += 1
    logger.info("Wrote %d leads to %s", written, output_path)
    return written


# ---- Outreach Markdown -------------------------------------------------


def format_value(v: Any) -> str:
    """Pretty-print a dollar amount for prose."""
    if v is None:
        return "value undisclosed"
    if isinstance(v, (int, float)):
        return f"${v:,.0f}"
    return str(v)


def build_outreach_markdown(
    leads: list[dict[str, Any]],
    *,
    contact_name: str = "there",
    sender_name: str = "[Your Name]",
    days: int = 7,
    max_leads: int = 5,
) -> str:
    """Compose a persuasive Markdown cold-email body.

    Keeps the email scannable: lead with a one-line hook, list the
    featured projects as numbered bullets (project / value / address /
    deadline / one-sentence summary / source link), then a soft CTA.

    Pure function — no I/O, no globals. Easy to unit-test and reuse.
    """
    if not leads:
        return (
            f"Hey {contact_name},\n\n"
            f"Heads up — no new commercial projects surfaced in our radar "
            f"in the last {days} days. We'll keep watching and ping you when something hits.\n\n"
            f"— {sender_name}\n"
        )

    featured = leads[:max_leads]
    overflow = len(leads) - len(featured)

    project_blocks: list[str] = []
    for i, lead in enumerate(featured, start=1):
        name = lead.get("project_name") or "Unnamed project"
        value = format_value(lead.get("estimated_value"))
        address = lead.get("location_address") or "address pending"
        deadline = lead.get("submission_deadline_or_permit_date") or "TBD"
        contractor = lead.get("contractor_or_bidder")
        contractor_str = f" — awarded to **{contractor}**" if contractor else ""
        summary = (lead.get("raw_extracted_summary") or "").strip()
        url = lead.get("source_url") or ""

        project_blocks.append(
            f"{i}. **{name}** — {value} — {address}\n"
            f"   Deadline / Permit date: {deadline}{contractor_str}\n"
            f"   {summary}\n"
            f"   Source: {url}"
        )

    overflow_line = (
        f"\n\n…plus {overflow} more we tracked this week — happy to send the full list."
        if overflow > 0
        else ""
    )

    return (
        f"Hey {contact_name},\n\n"
        f"Here are {len(featured)} new commercial projects we surfaced in the last "
        f"{days} days that look like a fit for your pipeline:\n\n"
        + "\n\n".join(project_blocks)
        + overflow_line
        + "\n\nWant this delivered to your inbox (or piped into your CRM) every "
        "Monday morning? Happy to wire it up.\n\n"
        f"— {sender_name}\n"
    )


# ---- Runner ------------------------------------------------------------


async def run(
    *,
    days: int,
    output_dir: Path,
    contact_name: str,
    sender_name: str,
    max_leads: int,
    write_csv: bool,
    write_markdown: bool,
    print_markdown: bool,
) -> int:
    configure_logging()
    repo = get_repository()
    if not await repo.ping():
        logger.error("MongoDB unreachable. Check MONGODB_URI.")
        return 2

    try:
        leads = await fetch_recent_leads(repo, days=days)
    except Exception:  # noqa: BLE001
        logger.exception("Failed to query leads from MongoDB")
        return 1

    logger.info("Found %d leads from the last %d day(s)", len(leads), days)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    if write_csv:
        csv_path = output_dir / f"leads_export_{today}.csv"
        export_csv(leads, csv_path)

    if write_markdown:
        md = build_outreach_markdown(
            leads,
            contact_name=contact_name,
            sender_name=sender_name,
            days=days,
            max_leads=max_leads,
        )
        md_path = output_dir / f"outreach_{today}.md"
        md_path.parent.mkdir(parents=True, exist_ok=True)
        md_path.write_text(md, encoding="utf-8")
        logger.info("Wrote outreach Markdown to %s", md_path)
        if print_markdown:
            sep = "-" * 60
            print(f"\n{sep}\n{md}{sep}\n")

    await repo.close()
    return 0


# ---- CLI ---------------------------------------------------------------


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="delivery",
        description="Export GovRadar leads to CSV and/or a cold-email Markdown.",
    )
    p.add_argument("--days", type=int, default=7,
                   help="Lookback window in days (default: 7).")
    p.add_argument("--output-dir", type=Path, default=Path("exports"),
                   help="Where to write artefacts (default: ./exports).")
    p.add_argument("--contact-name", default="there",
                   help="First name used in the email greeting.")
    p.add_argument("--sender-name", default="[Your Name]",
                   help="Sign-off name.")
    p.add_argument("--max-leads", type=int, default=5,
                   help="Cap the number of featured projects in the Markdown.")
    p.add_argument("--no-csv", action="store_true",
                   help="Skip the CSV export.")
    p.add_argument("--no-markdown", action="store_true",
                   help="Skip the Markdown outreach file.")
    p.add_argument("--quiet", action="store_true",
                   help="Don't echo the rendered Markdown to stdout.")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv or sys.argv[1:])
    if args.no_csv and args.no_markdown:
        print("error: nothing to do (both --no-csv and --no-markdown set)",
              file=sys.stderr)
        return 2
    return asyncio.run(run(
        days=args.days,
        output_dir=args.output_dir,
        contact_name=args.contact_name,
        sender_name=args.sender_name,
        max_leads=args.max_leads,
        write_csv=not args.no_csv,
        write_markdown=not args.no_markdown,
        print_markdown=not args.quiet,
    ))


if __name__ == "__main__":
    raise SystemExit(main())
