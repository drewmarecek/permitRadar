"""Orchestrator: Scraper -> Extractor -> Mongo.

Run a single URL:

    python pipeline.py https://example.gov/permits

Run a batch from a file (one URL per line, '#' lines ignored):

    python pipeline.py --file urls.txt --concurrency 4

Dry-run mode (no Mongo writes, no Anthropic calls, but scrapes for real):

    python pipeline.py --dry-run https://example.gov/permits

Architectural note
------------------
`Pipeline` accepts its extractor and repository via duck-typed
`Protocol`s. That lets `--dry-run` plug in stand-ins (`DryRunExtractor`,
`DryRunRepository`) without polluting the production code path with
`if dry_run:` branches. The production-mode call graph is identical
whether or not the flag is set.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional, Protocol

from db import LeadRepository, get_repository
from extractor import LeadExtractor
from logging_setup import configure_logging, get_logger
from models import Lead
from scraper import EtrakitSearchParams, FetchResult, ScrapeError, Scraper

logger = get_logger(__name__)


# ---- Protocols ----------------------------------------------------------
# Defined as Protocols (not abstract base classes) so existing concrete
# classes satisfy them without inheritance changes.


class ScraperLike(Protocol):
    async def fetch(
        self, url: str, *, etrakit_search: Optional[EtrakitSearchParams] = None,
    ) -> FetchResult: ...


class ExtractorLike(Protocol):
    async def extract(
        self, *, source_url: str, page_text: str, content_type: str
    ) -> list[Lead]: ...
    async def aclose(self) -> None: ...


class RepositoryLike(Protocol):
    async def ping(self) -> bool: ...
    async def ensure_indexes(self) -> None: ...
    async def upsert_many(self, leads: list[Lead]) -> int: ...
    async def close(self) -> None: ...


# ---- Outcome ------------------------------------------------------------


@dataclass(slots=True)
class UrlOutcome:
    """Per-URL summary, useful for CLI reporting and future job records."""

    url: str
    ok: bool
    leads_extracted: int = 0
    leads_written: int = 0
    error: str | None = None


# ---- Pipeline -----------------------------------------------------------


class Pipeline:
    """Wires the three stages together.

    Owns nothing it didn't construct itself, so it cleans up on exit.
    """

    def __init__(
        self,
        scraper: Any,  # Scraper or any object with `async fetch(url) -> FetchResult`
        extractor: ExtractorLike,
        repo: RepositoryLike,
        *,
        concurrency: int = 4,
        etrakit_search: Optional[EtrakitSearchParams] = None,
    ) -> None:
        self._scraper = scraper
        self._extractor = extractor
        self._repo = repo
        self._sem = asyncio.Semaphore(concurrency)
        # Applied to every URL in the batch. Non-eTRAKiT URLs ignore it
        # (the scraper only invokes the eTRAKiT path when the URL matches).
        self._etrakit_search = etrakit_search

    async def process_one(self, url: str) -> UrlOutcome:
        """Full pipeline for one URL. Never raises; failures are reported."""
        async with self._sem:
            logger.info("Processing %s", url)
            try:
                fetched: FetchResult = await self._scraper.fetch(
                    url, etrakit_search=self._etrakit_search,
                )
            except ScrapeError as e:
                logger.warning("Scrape failed for %s: %s", url, e)
                return UrlOutcome(url=url, ok=False, error=f"scrape: {e}")

            try:
                leads = await self._extractor.extract(
                    source_url=fetched.final_url,
                    page_text=fetched.text,
                    content_type=fetched.content_type,
                )
            except Exception as e:  # noqa: BLE001
                logger.exception("Extraction failed for %s", url)
                return UrlOutcome(url=url, ok=False, error=f"extract: {e}")

            written = await self._repo.upsert_many(leads)
            logger.info(
                "Done %s: extracted=%d written=%d (raw_len=%d, type=%s)",
                url, len(leads), written, fetched.raw_length, fetched.content_type,
            )
            return UrlOutcome(
                url=url,
                ok=True,
                leads_extracted=len(leads),
                leads_written=written,
            )

    async def process_many(self, urls: Iterable[str]) -> list[UrlOutcome]:
        tasks = [asyncio.create_task(self.process_one(u)) for u in urls]
        return await asyncio.gather(*tasks)


# ---- Dry-run stand-ins --------------------------------------------------


class DryRunExtractor:
    """Drop-in for `LeadExtractor` that fabricates a placeholder lead.

    Never touches the Anthropic API. The fabricated lead embeds a slice
    of the actual scraped text so the operator can confirm the scraper
    reached the right page.
    """

    async def extract(
        self, *, source_url: str, page_text: str, content_type: str
    ) -> list[Lead]:
        # Bumped from a smaller initial value because shorter snippets
        # were hiding the real scrape behind login/banner chrome and
        # leading to misdiagnosis. 800 chars is the sweet spot for
        # confirming dashboards, search forms, and result rows.
        snippet = (page_text or "").strip()[:800] or "(empty page)"
        logger.info("[dry-run] skipping Anthropic call for %s", source_url)
        sample = Lead(
            project_name=f"[DRY-RUN] Synthetic lead from {source_url}",
            location_address="(dry-run: address would be extracted by Claude)",
            estimated_value=None,
            contractor_or_bidder=None,
            submission_deadline_or_permit_date="2099-01-01",
            source_url=source_url,
            raw_extracted_summary=(
                f"DRY-RUN PREVIEW ({content_type}, {len(page_text)} chars scraped): {snippet}"
            ),
        )
        return [sample]

    async def aclose(self) -> None:
        return None


class DryRunRepository:
    """Drop-in for `LeadRepository` that prints instead of writing.

    `ensure_indexes` and `ping` are no-ops so dry-run requires no
    MongoDB connectivity at all.
    """

    async def ping(self) -> bool:
        return True

    async def ensure_indexes(self) -> None:
        return None

    async def upsert_many(self, leads: list[Lead]) -> int:
        for lead in leads:
            logger.info(
                "[dry-run] WOULD upsert: %s",
                json.dumps(lead.model_dump(), default=str, ensure_ascii=False),
            )
        return len(leads)

    async def close(self) -> None:
        return None


# ---- CLI ----------------------------------------------------------------


def _read_urls_file(path: Path) -> list[str]:
    urls = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        urls.append(line)
    return urls


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="govradar",
        description="Scrape municipal pages and extract structured leads.",
    )
    p.add_argument("urls", nargs="*", help="One or more URLs to process.")
    p.add_argument(
        "--file", "-f", type=Path, default=None,
        help="Path to a newline-delimited URL file.",
    )
    p.add_argument(
        "--concurrency", "-c", type=int, default=4,
        help="Max URLs processed in parallel (default: 4).",
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help="Scrape for real, but skip MongoDB writes and the Anthropic API. "
             "Prints what would be saved.",
    )

    etrakit = p.add_argument_group(
        "eTRAKiT search",
        "Parameterise the search form on eTRAKiT permit portals. "
        "All three flags are optional; non-eTRAKiT URLs ignore them.",
    )
    etrakit.add_argument(
        "--etrakit-search-by", default=None,
        help='Value for the "Search By" dropdown (e.g. "PERMIT NO", '
             '"SITE ADDRESS", "CONTRACTOR NAME"). Case-insensitive.',
    )
    etrakit.add_argument(
        "--etrakit-search-operator", default=None,
        help='Value for the "Operator" dropdown ("Contains", "Begins With", '
             '"Equals", etc.). Optional; ignored if the dropdown is absent.',
    )
    etrakit.add_argument(
        "--etrakit-search-value", default=None,
        help='Text to type into the search-value input (e.g. "B26-").',
    )

    return p.parse_args(argv)


def _build_etrakit_params(args: argparse.Namespace) -> Optional[EtrakitSearchParams]:
    """Translate CLI flags into an `EtrakitSearchParams` (or `None`).

    UX defaults per project brief: when the operator supplies a
    `--etrakit-search-value` but omits the other two, default to the
    common "PERMIT NO Contains <value>" pattern. When no flags are
    passed at all, return None (preserves blank-click behaviour).
    """
    if not any((args.etrakit_search_by, args.etrakit_search_operator, args.etrakit_search_value)):
        return None
    search_by = args.etrakit_search_by
    search_operator = args.etrakit_search_operator
    if args.etrakit_search_value and not search_by:
        search_by = "PERMIT NO"
    if args.etrakit_search_value and not search_operator:
        search_operator = "Contains"
    return EtrakitSearchParams(
        search_by=search_by,
        search_operator=search_operator,
        search_value=args.etrakit_search_value,
    )


def _build_components(*, dry_run: bool) -> tuple[ExtractorLike, RepositoryLike]:
    if dry_run:
        logger.warning("DRY-RUN mode: no MongoDB writes, no Anthropic calls")
        return DryRunExtractor(), DryRunRepository()
    return LeadExtractor(), get_repository()


async def _run(
    urls: list[str],
    concurrency: int,
    dry_run: bool,
    etrakit_search: Optional[EtrakitSearchParams],
) -> int:
    configure_logging()

    extractor, repo = _build_components(dry_run=dry_run)

    if not await repo.ping():
        logger.error("MongoDB unreachable. Check MONGODB_URI.")
        return 2
    await repo.ensure_indexes()

    if etrakit_search is not None:
        logger.info("eTRAKiT search params: %s", etrakit_search)

    async with Scraper() as scraper:
        pipeline = Pipeline(
            scraper, extractor, repo,
            concurrency=concurrency,
            etrakit_search=etrakit_search,
        )
        outcomes = await pipeline.process_many(urls)

    await extractor.aclose()
    await repo.close()

    ok = sum(1 for o in outcomes if o.ok)
    failed = len(outcomes) - ok
    total_leads = sum(o.leads_extracted for o in outcomes)
    total_written = sum(o.leads_written for o in outcomes)
    logger.info(
        "Run complete (dry_run=%s): urls_ok=%d urls_failed=%d leads_extracted=%d leads_written=%d",
        dry_run, ok, failed, total_leads, total_written,
    )
    for o in outcomes:
        if not o.ok:
            logger.error("FAIL %s :: %s", o.url, o.error)

    return 0 if failed == 0 else 1


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv or sys.argv[1:])

    urls: list[str] = list(args.urls)
    if args.file:
        urls.extend(_read_urls_file(args.file))

    if not urls:
        print("error: provide at least one URL or --file <path>", file=sys.stderr)
        return 2

    etrakit_search = _build_etrakit_params(args)

    return asyncio.run(_run(
        urls,
        concurrency=args.concurrency,
        dry_run=args.dry_run,
        etrakit_search=etrakit_search,
    ))


if __name__ == "__main__":
    raise SystemExit(main())
