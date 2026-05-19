"""Async MongoDB layer for the leads collection.

Why `motor`?
  The rest of the pipeline is async (Playwright + Anthropic). Using
  `motor` keeps the event loop free; `pymongo`'s blocking driver would
  serialise everything behind a thread pool. `motor` is built on top of
  `pymongo` and is the officially-supported async driver.

Indexes
  * compound unique on (source_url, project_name) → idempotent upserts
  * source_host → cheap per-jurisdiction analytics
  * submission_deadline_or_permit_date → time-window queries
  * created_at (last_seen_at) descending → "what changed today" feeds
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorCollection
from pymongo import ASCENDING, DESCENDING, ReturnDocument
from pymongo.errors import PyMongoError

from config import get_settings
from logging_setup import get_logger
from models import Lead, StoredLead

logger = get_logger(__name__)


class LeadRepository:
    """Thin async repository around the `leads` collection.

    Keeping Mongo access behind a class means:
      * `pipeline.py` never touches motor primitives directly,
      * we can swap stores (e.g. Postgres) without touching callers,
      * tests can substitute a fake `LeadRepository` easily.
    """

    def __init__(self, client: AsyncIOMotorClient, db_name: str, collection_name: str) -> None:
        self._client = client
        self._collection: AsyncIOMotorCollection = client[db_name][collection_name]

    # ---- lifecycle -----------------------------------------------------

    @classmethod
    def from_settings(cls) -> "LeadRepository":
        settings = get_settings()
        client = AsyncIOMotorClient(
            settings.mongodb_uri.get_secret_value(),
            tz_aware=True,
            serverSelectionTimeoutMS=5_000,
        )
        return cls(client, settings.mongodb_db, settings.mongodb_collection)

    async def close(self) -> None:
        self._client.close()

    async def ensure_indexes(self) -> None:
        """Create indexes if they don't exist. Safe to call on every boot."""
        try:
            await self._collection.create_index(
                [("source_url", ASCENDING), ("project_name", ASCENDING)],
                unique=True,
                name="uniq_source_project",
            )
            await self._collection.create_index(
                [("source_host", ASCENDING)],
                name="by_source_host",
            )
            await self._collection.create_index(
                [("submission_deadline_or_permit_date", ASCENDING)],
                name="by_deadline",
            )
            await self._collection.create_index(
                [("last_seen_at", DESCENDING)],
                name="by_last_seen_desc",
            )
            logger.info("MongoDB indexes ensured on %s", self._collection.full_name)
        except PyMongoError:
            logger.exception("Failed to ensure indexes on %s", self._collection.full_name)
            raise

    async def ping(self) -> bool:
        """Round-trip the cluster. Surfaces auth/network issues at boot."""
        try:
            await self._client.admin.command("ping")
            return True
        except PyMongoError:
            logger.exception("MongoDB ping failed")
            return False

    # ---- writes --------------------------------------------------------

    async def upsert_lead(self, lead: Lead) -> dict[str, Any]:
        """Insert a new lead or refresh `last_seen_at` if we've seen it.

        Dedup key: (source_url, project_name). On insert we also set
        `first_seen_at`; on update we only bump `last_seen_at` and replace
        mutable fields.
        """
        stored = StoredLead.from_lead(lead)
        now = datetime.now(timezone.utc)

        update_doc = {
            "$set": {
                **stored.model_dump(exclude={"first_seen_at"}),
                "last_seen_at": now,
            },
            "$setOnInsert": {"first_seen_at": now},
        }

        try:
            result = await self._collection.find_one_and_update(
                {"source_url": lead.source_url, "project_name": lead.project_name},
                update_doc,
                upsert=True,
                return_document=ReturnDocument.AFTER,
            )
            logger.debug(
                "Upserted lead",
                extra={
                    "project_name": lead.project_name,
                    "source_url": lead.source_url,
                },
            )
            return result
        except PyMongoError:
            logger.exception(
                "Mongo upsert failed",
                extra={
                    "project_name": lead.project_name,
                    "source_url": lead.source_url,
                },
            )
            raise

    async def upsert_many(self, leads: list[Lead]) -> int:
        """Sequentially upsert a batch; returns the count actually written."""
        written = 0
        for lead in leads:
            try:
                await self.upsert_lead(lead)
                written += 1
            except PyMongoError:
                continue
        return written

    # ---- reads ---------------------------------------------------------

    async def find_by_url(self, source_url: str) -> list[dict[str, Any]]:
        cursor = self._collection.find({"source_url": source_url})
        return [doc async for doc in cursor]

    async def count(self) -> int:
        return await self._collection.count_documents({})

    async def latest(self, limit: int = 10) -> list[dict[str, Any]]:
        cursor = self._collection.find({}).sort("last_seen_at", DESCENDING).limit(limit)
        return [doc async for doc in cursor]


_repo: Optional[LeadRepository] = None


def get_repository() -> LeadRepository:
    """Module-level accessor (lazy singleton).

    Pipelines/CLIs should call this rather than instantiating the
    repository themselves so connection pooling works as intended.
    """
    global _repo
    if _repo is None:
        _repo = LeadRepository.from_settings()
    return _repo
