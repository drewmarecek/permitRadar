"""Offline integration tests for the GovRadar pipeline.

What we test
------------
1. `LeadExtractor` parses Claude tool-use responses correctly, drops
   invalid leads, and never mutates `source_url`.
2. `LeadRepository` creates the right indexes (including the unique
   compound one) and upserts idempotently.
3. End-to-end: mocked Scraper + mocked Anthropic + REAL Mongo round-trip
   via `Pipeline.process_one`.

Design notes
------------
* The Anthropic SDK is mocked with `unittest.mock.AsyncMock` so this
  suite never spends a single token.
* MongoDB is real (motor + a running mongod). We use a `_test` suffix
  on the configured DB so we never touch production data, and we drop
  the test collection before and after every test. The DB-touching
  tests auto-skip if mongod isn't reachable.

Run it
------
    # one-shot
    python test_pipeline.py

    # verbose
    python test_pipeline.py -v

    # subset
    python test_pipeline.py TestExtractorWithMockedAnthropic
"""

from __future__ import annotations

import os
import unittest
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

# Critical: set env defaults BEFORE importing project modules, because
# `config.Settings()` is built at first import of `logging_setup`.
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-test-stub-not-used-by-mock")
# Force a dedicated DB so we never touch a real `govradar` collection.
os.environ["MONGODB_DB"] = os.environ.get("MONGODB_DB", "govradar") + "_test"

from config import get_settings  # noqa: E402
from db import LeadRepository  # noqa: E402
from extractor import LeadExtractor  # noqa: E402
from models import Lead  # noqa: E402
from pipeline import Pipeline  # noqa: E402
from scraper import FetchResult  # noqa: E402


# ---- Fixtures -----------------------------------------------------------


MOCK_HTML_TEXT = """\
City of Testville — Recent Permits (Week of May 12, 2026)

1. Permit #2026-001: Construction of a new fire station at 100 Main St.
   Estimated cost: $2,500,000. Awarded to Acme Builders.
   Bid deadline closed 2026-06-15.

2. Permit #2026-002: Demolition of warehouse at 200 Oak Ave.
   Estimated cost: $150,000. Applicant: TBD.
   Bid deadline: 2026-05-30.

(c) 2026 City of Testville. Privacy Policy. Cookie Notice.
"""

MOCK_PDF_TEXT = """\
PUBLIC NOTICE — INVITATION FOR BID 2026-118

The City of Testville invites sealed bids for the re-roofing of
Fire Station #4, 1280 Oak Ave, Testville, IL.

Estimated value: $425,000.
Bid submission deadline: 2026-06-12 at 2:00 PM local time.
"""


def make_html_fetch(url: str = "https://test.gov/permits") -> FetchResult:
    return FetchResult(
        url=url, final_url=url, content_type="html",
        text=MOCK_HTML_TEXT, raw_length=len(MOCK_HTML_TEXT),
    )


def make_pdf_fetch(url: str = "https://test.gov/rfp/2026-118.pdf") -> FetchResult:
    return FetchResult(
        url=url, final_url=url, content_type="pdf",
        text=MOCK_PDF_TEXT, raw_length=len(MOCK_PDF_TEXT),
    )


SAMPLE_LEAD_PAYLOAD = [
    {
        "project_name": "Fire station construction",
        "location_address": "100 Main St, Testville",
        "estimated_value": 2_500_000.0,
        "contractor_or_bidder": "Acme Builders",
        "submission_deadline_or_permit_date": "2026-06-15",
        "source_url": "https://test.gov/permits",
        "raw_extracted_summary": (
            "City of Testville issued Permit 2026-001 for the construction of a "
            "new fire station at 100 Main St. Estimated cost $2.5M, awarded to Acme Builders."
        ),
    },
    {
        "project_name": "Warehouse demolition",
        "location_address": "200 Oak Ave, Testville",
        "estimated_value": 150_000.0,
        "contractor_or_bidder": None,
        "submission_deadline_or_permit_date": "2026-05-30",
        "source_url": "https://test.gov/permits",
        "raw_extracted_summary": (
            "Permit 2026-002 covers demolition of the warehouse at 200 Oak Ave. "
            "Estimated $150K, applicant not yet determined."
        ),
    },
]


def make_mock_anthropic_response(leads_payload: list[dict[str, Any]]) -> SimpleNamespace:
    """Build a stand-in for the Anthropic `Message` object.

    Mirrors the shape `LeadExtractor._extract_tool_payload` expects: a
    `content` list containing one `tool_use` block whose `input` carries
    `{"leads": [...]}`.
    """
    tool_use_block = SimpleNamespace(
        type="tool_use",
        name="record_leads",
        input={"leads": leads_payload},
    )
    return SimpleNamespace(content=[tool_use_block], stop_reason="tool_use")


def _mongo_reachable() -> bool:
    """Sync probe of the configured Mongo server with a tight timeout."""
    try:
        from pymongo import MongoClient
        from pymongo.errors import PyMongoError
        client = MongoClient(
            get_settings().mongodb_uri.get_secret_value(),
            serverSelectionTimeoutMS=1500,
        )
        try:
            client.admin.command("ping")
            return True
        finally:
            client.close()
    except Exception:  # noqa: BLE001
        return False


MONGO_AVAILABLE = _mongo_reachable()
SKIP_MONGO_REASON = (
    "Local MongoDB not reachable; start it with "
    "`docker run -d -p 27017:27017 mongo:7` to run integration DB tests."
)


# ---- Extractor tests (no Mongo needed) ---------------------------------


class TestExtractorWithMockedAnthropic(unittest.IsolatedAsyncioTestCase):
    """All Anthropic calls are mocked; zero tokens spent."""

    async def _patched_extractor(self, payload: list[dict[str, Any]]) -> LeadExtractor:
        """Build an extractor whose `messages.create` returns `payload`."""
        extractor = LeadExtractor()
        # The Anthropic SDK constructs `messages` once in __init__, so a
        # direct attribute assignment sticks for the lifetime of the
        # client. We restore nothing because each test builds a fresh
        # extractor and the test process exits at the end.
        extractor._client.messages.create = AsyncMock(
            return_value=make_mock_anthropic_response(payload)
        )
        return extractor

    async def test_parses_tool_use_response(self) -> None:
        extractor = await self._patched_extractor(SAMPLE_LEAD_PAYLOAD)
        leads = await extractor.extract(
            source_url="https://test.gov/permits",
            page_text=MOCK_HTML_TEXT,
            content_type="html",
        )
        self.assertEqual(len(leads), 2)
        self.assertEqual(leads[0].project_name, "Fire station construction")
        self.assertEqual(leads[0].estimated_value, 2_500_000.0)
        self.assertEqual(leads[0].contractor_or_bidder, "Acme Builders")
        self.assertIsNone(leads[1].contractor_or_bidder)
        # source_url is overwritten by the extractor for safety.
        for lead in leads:
            self.assertEqual(lead.source_url, "https://test.gov/permits")

    async def test_drops_invalid_leads_keeps_valid(self) -> None:
        bad_then_good = [
            {"project_name": "x"},  # missing required fields => ValidationError
            SAMPLE_LEAD_PAYLOAD[0],
        ]
        extractor = await self._patched_extractor(bad_then_good)
        leads = await extractor.extract(
            source_url="https://test.gov/permits",
            page_text="anything",
            content_type="html",
        )
        self.assertEqual(len(leads), 1)
        self.assertEqual(leads[0].project_name, "Fire station construction")

    async def test_extract_handles_pdf_content(self) -> None:
        pdf_payload = [{
            "project_name": "Fire Station #4 re-roof",
            "location_address": "1280 Oak Ave, Testville, IL",
            "estimated_value": 425_000.0,
            "contractor_or_bidder": None,
            "submission_deadline_or_permit_date": "2026-06-12",
            "source_url": "https://test.gov/rfp/2026-118.pdf",
            "raw_extracted_summary": (
                "IFB 2026-118 invites sealed bids for re-roofing Fire Station #4. "
                "Estimated $425K, bids due 2026-06-12 at 2:00 PM."
            ),
        }]
        extractor = await self._patched_extractor(pdf_payload)
        leads = await extractor.extract(
            source_url="https://test.gov/rfp/2026-118.pdf",
            page_text=MOCK_PDF_TEXT,
            content_type="pdf",
        )
        self.assertEqual(len(leads), 1)
        self.assertEqual(leads[0].estimated_value, 425_000.0)

    async def test_empty_page_text_short_circuits(self) -> None:
        extractor = LeadExtractor()
        # Should never call the API for empty input. If it did, this
        # AsyncMock would record a call we could assert on.
        extractor._client.messages.create = AsyncMock(return_value=None)
        leads = await extractor.extract(
            source_url="https://test.gov", page_text="", content_type="html",
        )
        self.assertEqual(leads, [])
        extractor._client.messages.create.assert_not_called()

    async def test_value_coercion_treats_unknown_as_null(self) -> None:
        payload = [{
            **SAMPLE_LEAD_PAYLOAD[0],
            "estimated_value": "TBD",
        }]
        extractor = await self._patched_extractor(payload)
        leads = await extractor.extract(
            source_url="https://test.gov/permits",
            page_text="x",
            content_type="html",
        )
        self.assertEqual(len(leads), 1)
        self.assertIsNone(leads[0].estimated_value)


# ---- DB integration tests (real Mongo) ---------------------------------


@unittest.skipUnless(MONGO_AVAILABLE, SKIP_MONGO_REASON)
class TestDbIntegration(unittest.IsolatedAsyncioTestCase):
    """Exercises motor + a real local MongoDB instance."""

    async def asyncSetUp(self) -> None:
        self.repo = LeadRepository.from_settings()
        await self.repo._collection.drop()
        await self.repo.ensure_indexes()

    async def asyncTearDown(self) -> None:
        await self.repo._collection.drop()
        await self.repo.close()

    async def test_indexes_created_correctly(self) -> None:
        indexes = await self.repo._collection.index_information()
        self.assertIn("uniq_source_project", indexes)
        self.assertIn("by_source_host", indexes)
        self.assertIn("by_deadline", indexes)
        self.assertIn("by_last_seen_desc", indexes)
        # The compound key must be unique to guarantee idempotency.
        self.assertTrue(indexes["uniq_source_project"].get("unique", False))

    async def test_upsert_is_idempotent(self) -> None:
        lead = Lead.model_validate(SAMPLE_LEAD_PAYLOAD[0])
        await self.repo.upsert_lead(lead)
        await self.repo.upsert_lead(lead)
        await self.repo.upsert_lead(lead)
        self.assertEqual(await self.repo.count(), 1)

    async def test_distinct_projects_coexist(self) -> None:
        for payload in SAMPLE_LEAD_PAYLOAD:
            await self.repo.upsert_lead(Lead.model_validate(payload))
        self.assertEqual(await self.repo.count(), 2)

    async def test_last_seen_at_advances_first_seen_at_stable(self) -> None:
        import asyncio
        lead = Lead.model_validate(SAMPLE_LEAD_PAYLOAD[0])
        doc1 = await self.repo.upsert_lead(lead)
        await asyncio.sleep(0.02)
        doc2 = await self.repo.upsert_lead(lead)
        self.assertEqual(doc1["first_seen_at"], doc2["first_seen_at"])
        self.assertGreater(doc2["last_seen_at"], doc1["last_seen_at"])

    async def test_source_host_precomputed(self) -> None:
        await self.repo.upsert_lead(Lead.model_validate(SAMPLE_LEAD_PAYLOAD[0]))
        docs = await self.repo.find_by_url("https://test.gov/permits")
        self.assertEqual(len(docs), 1)
        self.assertEqual(docs[0]["source_host"], "test.gov")


# ---- End-to-end ---------------------------------------------------------


@unittest.skipUnless(MONGO_AVAILABLE, SKIP_MONGO_REASON)
class TestEndToEnd(unittest.IsolatedAsyncioTestCase):
    """Mocked scraper + mocked Anthropic + REAL Mongo, via `Pipeline`."""

    async def asyncSetUp(self) -> None:
        self.repo = LeadRepository.from_settings()
        await self.repo._collection.drop()
        await self.repo.ensure_indexes()

    async def asyncTearDown(self) -> None:
        await self.repo._collection.drop()
        await self.repo.close()

    async def test_html_page_round_trip(self) -> None:
        mock_scraper = SimpleNamespace(fetch=AsyncMock(return_value=make_html_fetch()))
        extractor = LeadExtractor()
        extractor._client.messages.create = AsyncMock(
            return_value=make_mock_anthropic_response(SAMPLE_LEAD_PAYLOAD)
        )

        pipeline = Pipeline(mock_scraper, extractor, self.repo, concurrency=1)
        outcome = await pipeline.process_one("https://test.gov/permits")

        self.assertTrue(outcome.ok, outcome.error)
        self.assertEqual(outcome.leads_extracted, 2)
        self.assertEqual(outcome.leads_written, 2)
        self.assertEqual(await self.repo.count(), 2)

        # Idempotency at the pipeline level: re-run the same URL.
        outcome2 = await pipeline.process_one("https://test.gov/permits")
        self.assertTrue(outcome2.ok)
        self.assertEqual(await self.repo.count(), 2, "second run must not duplicate")

    async def test_pdf_page_round_trip(self) -> None:
        pdf_payload = [{
            "project_name": "Fire Station #4 re-roof",
            "location_address": "1280 Oak Ave, Testville, IL",
            "estimated_value": 425_000.0,
            "contractor_or_bidder": None,
            "submission_deadline_or_permit_date": "2026-06-12",
            "source_url": "https://test.gov/rfp/2026-118.pdf",
            "raw_extracted_summary": "IFB 2026-118 re-roofing of Fire Station #4.",
        }]
        mock_scraper = SimpleNamespace(fetch=AsyncMock(return_value=make_pdf_fetch()))
        extractor = LeadExtractor()
        extractor._client.messages.create = AsyncMock(
            return_value=make_mock_anthropic_response(pdf_payload)
        )

        pipeline = Pipeline(mock_scraper, extractor, self.repo, concurrency=1)
        outcome = await pipeline.process_one("https://test.gov/rfp/2026-118.pdf")

        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.leads_written, 1)


if __name__ == "__main__":
    unittest.main()
