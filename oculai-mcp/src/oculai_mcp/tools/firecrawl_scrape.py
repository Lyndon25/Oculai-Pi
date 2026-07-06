"""Firecrawl scrape tool — single-page web scraping via Firecrawl API.

Provides high-quality single-page scraping with native markdown output,
supporting static HTML, JavaScript SPAs, and PDF parsing.

Supports keyless mode (rate-limited per IP, works from Node.js/browsers).
For Python access, set FIRECRAWL_API_KEY in .env.
"""

import time
from typing import Any

import httpx

from oculai_mcp.db.provenance import log_source_call
from oculai_mcp.tools import firecrawl_client
from oculai_mcp.tools.firecrawl_client import (
    FirecrawlApiError,
    FirecrawlKeylessBlockedError,
)


async def scrape_page(
    url: str,
    formats: list[str] | None = None,
    wait_for: int | None = None,
    run_id: Any = None,
) -> dict[str, Any]:
    """Scrape a single web page via Firecrawl and return clean markdown.

    Supports static HTML, JavaScript SPAs (with waitFor), and PDF parsing.
    Uses keyless mode when no API key is configured.

    Args:
        url: The URL to scrape
        formats: Output formats — ["markdown"] (default), ["html"], ["screenshot"], etc.
        wait_for: Milliseconds to wait for JS rendering (SPA pages)
        run_id: Optional run UUID for provenance tracking

    Returns:
        {"status": "success", "data": {"markdown": "...", "metadata": {...}}}
    """
    if not url:
        return {"status": "error", "error": {"code": "empty_url", "message": "URL must not be empty."}}

    start = time.monotonic()

    if formats is None:
        formats = ["markdown"]

    query_params: dict[str, Any] = {"url": url, "formats": formats}

    try:
        result = await firecrawl_client.scrape(url, formats, wait_for)
    except FirecrawlKeylessBlockedError as e:
        elapsed_ms = int((time.monotonic() - start) * 1000)
        await log_source_call(
            source_name="firecrawl_scrape",
            source_type="api",
            query_params=query_params,
            status="failed",
            duration_ms=elapsed_ms,
            error_message=str(e),
            run_id=run_id,
        )
        return {
            "status": "error",
            "error": {"code": "keyless_blocked", "message": str(e)},
        }
    except httpx.HTTPStatusError as e:
        elapsed_ms = int((time.monotonic() - start) * 1000)
        await log_source_call(
            source_name="firecrawl_scrape",
            source_type="api",
            query_params=query_params,
            status="failed",
            duration_ms=elapsed_ms,
            error_message=str(e),
            run_id=run_id,
        )
        return {
            "status": "error",
            "error": {
                "code": "http_error",
                "message": str(e),
                "status_code": e.response.status_code,
            },
        }
    except FirecrawlApiError as e:
        elapsed_ms = int((time.monotonic() - start) * 1000)
        await log_source_call(
            source_name="firecrawl_scrape",
            source_type="api",
            query_params=query_params,
            status="failed",
            duration_ms=elapsed_ms,
            error_message=str(e),
            run_id=run_id,
        )
        return {
            "status": "error",
            "error": {"code": "scrape_failed", "message": str(e)},
        }
    except Exception as e:
        # Transport errors (timeouts, connection resets) and any other
        # unexpected failure. The shared client does not mask these, so we
        # catch them here to preserve the tool's never-raises contract.
        elapsed_ms = int((time.monotonic() - start) * 1000)
        await log_source_call(
            source_name="firecrawl_scrape",
            source_type="api",
            query_params=query_params,
            status="failed",
            duration_ms=elapsed_ms,
            error_message=str(e),
            run_id=run_id,
        )
        return {"status": "error", "error": {"code": "scrape_failed", "message": str(e)}}

    elapsed_ms = int((time.monotonic() - start) * 1000)
    await log_source_call(
        source_name="firecrawl_scrape",
        source_type="api",
        query_params=query_params,
        status="success",
        duration_ms=elapsed_ms,
        run_id=run_id,
    )
    return {
        "status": "success",
        "data": {
            "markdown": result.get("markdown", ""),
            "metadata": result.get("metadata", {}),
        },
        "meta": {"latency_ms": elapsed_ms, "provider": "firecrawl"},
    }
