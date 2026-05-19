"""Shared Pydantic models for GovRadar-Pipeline.

Centralising the `Lead` schema in one place gives us:
  * a single source of truth shared by `extractor.py` (LLM contract),
    `db.py` (storage shape) and `pipeline.py` (runtime validation),
  * an auto-generated JSON Schema we hand to Claude as a tool definition,
  * cheap, fast validation of every record before it reaches MongoDB.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator


class Lead(BaseModel):
    """A single high-value lead extracted from a municipal source.

    All fields mirror the schema requested in the project brief. Nullable
    fields are explicitly typed `Optional[...]` so the LLM is allowed to
    omit them rather than hallucinate a value.
    """

    model_config = ConfigDict(
        str_strip_whitespace=True,
        extra="ignore",
        populate_by_name=True,
    )

    project_name: str = Field(
        ...,
        min_length=2,
        description="Short, human-readable name of the project, permit, or RFP.",
    )
    location_address: str = Field(
        ...,
        min_length=2,
        description="Street address, parcel ID, or best available locator.",
    )
    estimated_value: Optional[Union[float, str]] = Field(
        default=None,
        description=(
            "Estimated dollar value as a float (preferred) or original string "
            "(e.g. '$2.5M', 'TBD'). Null if not stated."
        ),
    )
    contractor_or_bidder: Optional[str] = Field(
        default=None,
        description="Awarded contractor, applicant, or short bidder list. Null if unknown.",
    )
    submission_deadline_or_permit_date: str = Field(
        ...,
        description=(
            "ISO-8601 date (YYYY-MM-DD) when possible, otherwise the raw "
            "date string as it appears on the source."
        ),
    )
    source_url: str = Field(
        ...,
        description="The exact URL the lead was scraped from.",
    )
    raw_extracted_summary: str = Field(
        ...,
        min_length=10,
        description="2-4 sentence factual summary of the lead in plain English.",
    )

    @field_validator("estimated_value", mode="before")
    @classmethod
    def _coerce_value(cls, v: Any) -> Any:
        """Accept empty strings and common 'unknown' tokens as null."""
        if v is None:
            return None
        if isinstance(v, str) and v.strip().lower() in {"", "n/a", "na", "null", "tbd", "unknown"}:
            return None
        return v


class StoredLead(Lead):
    """A `Lead` enriched with storage-only metadata.

    Kept distinct from `Lead` so the LLM contract (`Lead`) stays minimal
    while MongoDB documents carry provenance information.
    """

    first_seen_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    last_seen_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    source_host: Optional[str] = Field(
        default=None,
        description="Hostname of source_url; precomputed to make per-jurisdiction queries cheap.",
    )

    @classmethod
    def from_lead(cls, lead: Lead) -> "StoredLead":
        # Use HttpUrl just to parse hostname robustly, then discard.
        try:
            host = HttpUrl(lead.source_url).host
        except Exception:
            host = None
        return cls(**lead.model_dump(), source_host=host)
