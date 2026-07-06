"""Firecrawl Web Search data source.

General web search via Firecrawl for discovering candidates mentioned in
news, blogs, company pages, and tech forums.

Supports keyless mode (rate-limited per IP, works from Node.js/browsers).
For Python access, get a free API key at https://firecrawl.dev/app/api-keys
(1000 credits/month free).
"""

import logging
import time

from oculai_mcp.db.provenance import log_source_call
from oculai_mcp.db.quotas import check_quota, try_consume_quota
from oculai_mcp.sources.base import HealthStatus, IDataSource, RawCandidate, SearchQuery
from oculai_mcp.tools import firecrawl_client
from oculai_mcp.utils.name_extract import extract_person_name_from_title

logger = logging.getLogger(__name__)


class FirecrawlSource(IDataSource):
    """Firecrawl Web Search data source.

    Uses Firecrawl's search API to discover candidates across the web.
    Supports keyless mode (rate-limited per IP) and authenticated mode
    (higher rate limits, 1000 free credits/month).

    Keyless mode works from browsers and Node.js. For Python access,
    set FIRECRAWL_API_KEY in .env (free key from firecrawl.dev).
    """

    name = "firecrawl"
    source_type = "api"
    description = (
        "Search the web via Firecrawl for candidate discovery across global "
        "web sources. Useful for finding candidates mentioned in news, blogs, "
        "company sites, and personal homepages. Keyless mode available "
        "(rate-limited per IP). Free API key gives 1000 credits/month. "
        "Get a key at: https://firecrawl.dev/app/api-keys"
    )
    supported_operations = ["search", "get_detail"]
    id_field_map = {}
    example_queries = [
        "machine learning researcher Stanford",
        "NLP scientist Google DeepMind",
        "AI engineer Beijing",
        "computer vision professor Tsinghua",
    ]
    auth_required = False
    rate_limit_notes = (
        "Keyless: rate-limited per IP (~10 req/min). "
        "With API key: 1000 free credits/month, higher limits."
    )

    async def search(self, query: SearchQuery) -> list[RawCandidate]:
        """Search Firecrawl web for candidate mentions.

        Never raises: on quota exhaustion or any Firecrawl failure (keyless
        403, HTTP error, ``success=False``) it logs a single provenance row
        and returns an empty list, consistent with sibling sources (F1/F2).
        """
        start = time.monotonic()

        # Quota exhaustion: bail out early (return [], never raise), matching
        # the sibling sources (duckduckgo, baidu, ...). Logged once here,
        # outside the try, so the broad except cannot double-log it.
        if not await check_quota(self.name):
            await log_source_call(
                source_name=self.name,
                source_type=self.source_type,
                query_params={"keywords": query.keywords},
                status="rate_limited",
                duration_ms=0,
            )
            return []

        candidates: list[RawCandidate] = []
        try:
            keywords = " ".join(query.keywords)
            max_results = min(query.limit, 20)

            # The shared client raises FirecrawlKeylessBlockedError (keyless
            # 403), FirecrawlApiError (success=False), or httpx.HTTPStatusError
            # on other HTTP failures. The broad except below logs a single
            # 'failed' provenance row and returns [] — search() never raises
            # and never double-logs (F1/F2).
            results = await firecrawl_client.search(query=keywords, limit=max_results)

            for r in results[:max_results]:
                title = r.get("title", "")
                url = r.get("url", "")
                snippet = r.get("description", "")

                name = extract_person_name_from_title(title, snippet)
                if name:
                    result_type = "profile_page"
                    confidence = "medium"
                    extraction_method = "inferred"
                else:
                    name = "Unknown"
                    result_type = "web_page"
                    confidence = "low"
                    extraction_method = "unverified"

                candidates.append(
                    RawCandidate(
                        name=name,
                        profile_url=url or None,
                        raw_metadata={
                            "source": "firecrawl",
                            "title": title,
                            "snippet": snippet,
                            "url": url,
                        },
                        result_type=result_type,
                        confidence=confidence,
                        extraction_method=extraction_method,
                    )
                )

            await try_consume_quota(self.name, amount=len(candidates))
            duration_ms = int((time.monotonic() - start) * 1000)
            await log_source_call(
                source_name=self.name,
                source_type=self.source_type,
                query_params={"keywords": query.keywords, "limit": query.limit},
                status="success",
                duration_ms=duration_ms,
                records_count=len(candidates),
            )

        except Exception as e:
            duration_ms = int((time.monotonic() - start) * 1000)
            await log_source_call(
                source_name=self.name,
                source_type=self.source_type,
                query_params={"keywords": query.keywords},
                status="failed",
                duration_ms=duration_ms,
                error_message=str(e),
            )
            logger.exception("Firecrawl search failed")

        return candidates

    async def get_detail(self, external_id: str) -> RawCandidate | None:
        """Scrape a profile URL via Firecrawl and extract candidate details.

        Returns ``None`` on any failure (keyless 403, HTTP error,
        ``success=False``); the failure is logged once via provenance.
        Confidence/extraction_method are gated on whether a real name was
        extracted from the page title (F12): an extracted name yields
        ``medium``/``direct``; a fallback to the raw title or ``"Unknown"``
        yields ``low``/``unverified``.
        """
        start = time.monotonic()

        try:
            # The shared client raises on keyless 403 / success=False / HTTP
            # errors; the except below logs once and returns None.
            data = await firecrawl_client.scrape(
                url=external_id, formats=["markdown"]
            )

            markdown = data.get("markdown") or ""
            metadata = data.get("metadata") or {}
            title = metadata.get("title", "")

            # Gate confidence/extraction_method on whether a name was actually
            # extracted vs. falling back to the raw title / "Unknown" (F12),
            # mirroring search()'s low/medium distinction.
            extracted_name = extract_person_name_from_title(title, "")
            if extracted_name:
                name = extracted_name
                confidence = "medium"
                extraction_method = "direct"
            else:
                name = title or "Unknown"
                confidence = "low"
                extraction_method = "unverified"

            duration_ms = int((time.monotonic() - start) * 1000)
            await log_source_call(
                source_name=f"{self.name}_detail",
                source_type=self.source_type,
                query_params={"external_id": external_id},
                status="success",
                duration_ms=duration_ms,
            )

            return RawCandidate(
                name=name,
                profile_url=external_id,
                raw_metadata={
                    "source": "firecrawl",
                    "title": title,
                    "markdown_preview": markdown[:500],
                    "url": external_id,
                },
                result_type="profile_page",
                confidence=confidence,
                extraction_method=extraction_method,
            )

        except Exception as e:
            duration_ms = int((time.monotonic() - start) * 1000)
            await log_source_call(
                source_name=f"{self.name}_detail",
                source_type=self.source_type,
                query_params={"external_id": external_id},
                status="failed",
                duration_ms=duration_ms,
                error_message=str(e),
            )
            logger.exception("Firecrawl get_detail failed")
            return None

    async def check_health(self) -> HealthStatus:
        """Probe Firecrawl connectivity via the shared client.

        Keyless 403 is treated as healthy-but-keyless (keyless is a supported
        mode, matching ``auth_required=False``); other non-200 statuses are
        unhealthy with a descriptive message (F11).
        """
        healthy, latency_ms, error_message = await firecrawl_client.check_health()
        return HealthStatus(
            healthy=healthy,
            latency_ms=latency_ms,
            error_message=error_message,
        )
