"""LLM-based lead extraction using Anthropic Claude 3.5 Sonnet.

Why tool-use instead of "please return JSON"?
  Tool-use is the Anthropic-recommended path to *guaranteed* structured
  output. We declare a `record_leads` tool whose `input_schema` is
  derived from our `Lead` Pydantic model, then force the model to call
  it via `tool_choice`. Claude can no longer reply in prose; it must
  emit a JSON object that conforms to the schema. We validate the
  payload with Pydantic on the way out for belt-and-braces safety.

Why batch into one tool call?
  Many municipal pages list multiple permits/RFPs on one screen. A
  single tool call returning `leads: [...]` keeps us at one round-trip
  per page, which dominates both latency and API cost at scale.
"""

from __future__ import annotations

from typing import Any

from anthropic import APIError, APIStatusError, APITimeoutError, AsyncAnthropic
from pydantic import ValidationError
from tenacity import (
    AsyncRetrying,
    RetryError,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from config import get_settings
from logging_setup import get_logger
from models import Lead

logger = get_logger(__name__)


SYSTEM_PROMPT = """\
You are GovRadar-Extractor, a world-class data parser specialised in
United States municipal procurement, planning, and permitting websites.

Your job is to read raw text scraped from a single government page (HTML
or PDF) and return only the high-value business leads it contains. A
"high-value lead" is one that a contractor, supplier, real-estate
investor, or business-development team could act on, such as:

  * Open Requests for Proposals (RFPs), Invitations for Bid (IFBs),
    Requests for Quotation (RFQs), or Notices of Funding Opportunity.
  * Awarded contracts that name a winning vendor.
  * Building permits, demolition permits, or zoning variances with a
    stated address and applicant.
  * Planning-commission agenda items for new development projects.

You MUST follow these rules:

  1. STRIP boilerplate ruthlessly. Ignore navigation, headers, footers,
     accessibility statements, ADA notices, contact forms, generic site
     copy, cookie banners, social-media calls-to-action, and anything
     resembling "© City of …".
  2. NEVER invent data. If a field is missing from the source, set it
     to null (for nullable fields) or omit the lead entirely if a
     required field is unrecoverable.
  3. PREFER ISO-8601 dates (YYYY-MM-DD). If only a fiscal year or
     month/year is given, return the source's exact string.
  4. COERCE dollar amounts to a number when unambiguous (e.g.
     "$2.5 million" -> 2_500_000.0). If formatting is ambiguous, return
     the original string verbatim.
  5. DEDUPLICATE within the page. If the same project appears twice
     (e.g. in a summary table and a detail section), emit it once with
     the most specific information available.
  6. The `raw_extracted_summary` must be 2-4 plain-English sentences a
     business-development analyst could skim. No marketing language, no
     editorialising, only facts present in the source.
  7. If the page contains NO qualifying leads, call the tool with an
     empty `leads` array. Never refuse, never apologise.

You will respond ONLY by calling the `record_leads` tool. Do not write
any natural-language response.
"""


def _build_tool_schema() -> dict[str, Any]:
    """Anthropic tool definition derived from the `Lead` Pydantic model."""
    lead_schema = Lead.model_json_schema()
    # Strip pydantic metadata Anthropic doesn't need.
    lead_schema.pop("title", None)
    return {
        "name": "record_leads",
        "description": (
            "Record every distinct high-value lead found on the page. "
            "Call exactly once. Pass an empty array if none are present."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "leads": {
                    "type": "array",
                    "description": "All leads extracted from the page.",
                    "items": lead_schema,
                }
            },
            "required": ["leads"],
        },
    }


class LeadExtractor:
    """Thin async wrapper around the Anthropic client.

    Constructed once per process and reused; the underlying HTTP client
    pools connections, so churning instances throws away that pool.
    """

    def __init__(self) -> None:
        self._settings = get_settings()
        if self._settings.anthropic_api_key is None:
            raise RuntimeError(
                "ANTHROPIC_API_KEY is not set. Required for live extraction. "
                "Use `python pipeline.py --dry-run ...` to preview without calling the API."
            )
        self._client = AsyncAnthropic(
            api_key=self._settings.anthropic_api_key.get_secret_value(),
            timeout=self._settings.anthropic_timeout_seconds,
        )
        self._tool = _build_tool_schema()

    async def extract(self, *, source_url: str, page_text: str, content_type: str) -> list[Lead]:
        """Extract zero-or-more `Lead`s from a single page's text.

        `source_url` is injected into the user message (and into the
        validated `Lead`s afterwards) so Claude doesn't have to guess.
        """
        if not page_text or not page_text.strip():
            logger.warning("Empty page_text for %s, skipping extraction", source_url)
            return []

        user_message = (
            f"SOURCE_URL: {source_url}\n"
            f"CONTENT_TYPE: {content_type}\n"
            f"---BEGIN SOURCE---\n{page_text}\n---END SOURCE---"
        )

        try:
            response = await self._call_with_retries(user_message)
        except RetryError as e:
            logger.exception("Anthropic call exhausted retries for %s", source_url)
            raise RuntimeError(f"Extraction failed for {source_url}") from e

        leads_payload = self._extract_tool_payload(response, source_url=source_url)
        return self._validate(leads_payload, source_url=source_url)

    # ---- internals -----------------------------------------------------

    async def _call_with_retries(self, user_message: str) -> Any:
        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(3),
            wait=wait_exponential(multiplier=2, min=2, max=30),
            retry=retry_if_exception_type((APITimeoutError, APIStatusError, APIError)),
            reraise=True,
        ):
            with attempt:
                return await self._client.messages.create(
                    model=self._settings.anthropic_model,
                    max_tokens=self._settings.anthropic_max_tokens,
                    system=SYSTEM_PROMPT,
                    tools=[self._tool],
                    tool_choice={"type": "tool", "name": "record_leads"},
                    messages=[{"role": "user", "content": user_message}],
                )

    def _extract_tool_payload(self, response: Any, *, source_url: str) -> list[dict[str, Any]]:
        """Pull the `leads` list out of the tool_use response block."""
        for block in response.content:
            if getattr(block, "type", None) == "tool_use" and block.name == "record_leads":
                payload = block.input or {}
                leads = payload.get("leads", [])
                if not isinstance(leads, list):
                    logger.error(
                        "record_leads returned non-list payload for %s: %r",
                        source_url, leads,
                    )
                    return []
                return leads
        logger.error(
            "No tool_use block in Claude response for %s. stop_reason=%s",
            source_url, getattr(response, "stop_reason", "?"),
        )
        return []

    def _validate(self, raw_leads: list[dict[str, Any]], *, source_url: str) -> list[Lead]:
        """Pydantic-validate each lead, dropping (and logging) bad records."""
        out: list[Lead] = []
        for i, raw in enumerate(raw_leads):
            # Always overwrite source_url with the one we actually fetched;
            # protects against the model hallucinating a different URL.
            raw["source_url"] = source_url
            try:
                out.append(Lead.model_validate(raw))
            except ValidationError as e:
                logger.warning(
                    "Dropped invalid lead #%d from %s: %s",
                    i, source_url, e.errors(include_url=False),
                )
        logger.info("Extracted %d valid leads from %s", len(out), source_url)
        return out

    async def aclose(self) -> None:
        """Release the underlying HTTP client. Optional but tidy."""
        await self._client.close()
