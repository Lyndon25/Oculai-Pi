"""Shared Firecrawl HTTP client.

Centralizes the Firecrawl API base URL, User-Agent, auth-header construction,
httpx client lifecycle, keyless-403 detection, and ``success=False`` handling
so that every call site (``sources/firecrawl.py``, ``tools/firecrawl_scrape.py``,
``tools/web_search.py``, and the Firecrawl fast-path in ``tools/site_crawler.py``)
speaks the same dialect.

Keyless mode (no API key) works from browsers and Node.js but is blocked from
Python due to TLS fingerprinting. When a 403 is observed without a key we raise
``FirecrawlKeylessBlockedError`` so callers can surface a clear, actionable
message instead of a cryptic HTTP error. ``success=False`` responses raise
``FirecrawlApiError``. Other HTTP errors propagate via ``raise_for_status``.

This module performs NO logging and NO provenance writes — callers own those
concerns, which keeps the single-failure / single-log contract (F1/F2) intact.
"""

import time
from typing import Any

import httpx

from oculai_mcp.config import get_settings

FIRECRAWL_API_BASE = "https://api.firecrawl.dev/v1"
USER_AGENT = "Oculai/1.0 (+https://github.com/oculai)"


class FirecrawlKeylessBlockedError(RuntimeError):
    """Raised when keyless mode is blocked from Python (403 + no API key)."""


class FirecrawlApiError(RuntimeError):
    """Raised when Firecrawl returns ``success=False`` or a malformed response."""


def get_api_key() -> str | None:
    """Return the configured Firecrawl API key, or ``None`` (keyless mode)."""
    return getattr(get_settings(), "firecrawl_api_key", None)


def build_headers(api_key: str | None = None) -> dict[str, str]:
    """Build request headers. Adds Bearer auth only when a key is present."""
    headers: dict[str, str] = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def is_keyless_blocked(resp: httpx.Response, api_key: str | None) -> bool:
    """True when the response is a 403 and we are running keyless."""
    return resp.status_code == 403 and not api_key


def keyless_blocked_message() -> str:
    """Standard, actionable message for keyless-blocked errors."""
    return (
        "Firecrawl keyless blocked from Python (TLS fingerprint). "
        "Get a free API key at https://firecrawl.dev/app/api-keys "
        "and set FIRECRAWL_API_KEY in .env"
    )


def _client() -> httpx.AsyncClient:
    """Create a short-lived httpx client with the standard UA and timeout.

    Each public async function builds its own client via this helper so that
    connections are scoped to a single request lifecycle (matching the
    pre-refactor call sites).
    """
    return httpx.AsyncClient(
        timeout=30.0,
        headers={"User-Agent": USER_AGENT},
    )


async def search(
    query: str,
    limit: int = 20,
    include_domains: list[str] | None = None,
    exclude_domains: list[str] | None = None,
) -> list[dict[str, Any]]:
    """POST /v1/search and return the ``data`` array.

    Args:
        query: Search query string.
        limit: Max results (clamped to 20).
        include_domains: Optional domain allowlist.
        exclude_domains: Optional domain blocklist.

    Returns:
        The list of result objects from ``data`` (empty list if none).

    Raises:
        FirecrawlKeylessBlockedError: 403 while running keyless.
        FirecrawlApiError: ``success=False`` response.
        httpx.HTTPStatusError: any other non-2xx status.
    """
    api_key = get_api_key()
    body: dict[str, Any] = {"query": query, "limit": min(limit, 20)}
    if include_domains:
        body["includeDomains"] = include_domains
    if exclude_domains:
        body["excludeDomains"] = exclude_domains

    async with _client() as client:
        resp = await client.post(
            f"{FIRECRAWL_API_BASE}/search", json=body, headers=build_headers(api_key)
        )
        if is_keyless_blocked(resp, api_key):
            raise FirecrawlKeylessBlockedError(keyless_blocked_message())
        resp.raise_for_status()
        data = resp.json()
        if not data.get("success"):
            raise FirecrawlApiError(
                data.get("error", "Firecrawl search returned unsuccessful response")
            )
        return data.get("data", [])


async def scrape(
    url: str,
    formats: list[str] | None = None,
    wait_for: int | None = None,
) -> dict[str, Any]:
    """POST /v1/scrape and return the ``data`` object.

    Args:
        url: The URL to scrape.
        formats: Output formats (defaults to ``["markdown"]``).
        wait_for: Optional milliseconds to wait for JS rendering.

    Returns:
        The scrape ``data`` object (empty dict if absent).

    Raises:
        FirecrawlKeylessBlockedError: 403 while running keyless.
        FirecrawlApiError: ``success=False`` response.
        httpx.HTTPStatusError: any other non-2xx status.
    """
    api_key = get_api_key()
    body: dict[str, Any] = {
        "url": url,
        "formats": formats if formats is not None else ["markdown"],
    }
    if wait_for is not None:
        body["waitFor"] = wait_for

    async with _client() as client:
        resp = await client.post(
            f"{FIRECRAWL_API_BASE}/scrape", json=body, headers=build_headers(api_key)
        )
        if is_keyless_blocked(resp, api_key):
            raise FirecrawlKeylessBlockedError(keyless_blocked_message())
        resp.raise_for_status()
        data = resp.json()
        if not data.get("success"):
            raise FirecrawlApiError(
                data.get("error", "Firecrawl scrape returned unsuccessful response")
            )
        return data.get("data") or {}


async def start_crawl(
    url: str,
    max_pages: int,
    max_depth: int | None = None,
    allow_external: bool = False,
) -> str:
    """POST /v1/crawl to start an async crawl job; return the job id.

    Args:
        url: Starting URL for the crawl.
        max_pages: Maximum pages to fetch (sent as ``limit``).
        max_depth: Optional maximum link depth (sent as ``maxDepth``). ``None``
            omits the field; ``0`` is sent as-is (Firecrawl treats 0 as
            "homepage only" — callers that want the default should pass ``None``).
        allow_external: Whether to follow external links (sent as
            ``allowExternalLinks``).

    Returns:
        The Firecrawl crawl job id.

    Raises:
        FirecrawlApiError: ``success=False`` or no job id in the response.
        httpx.HTTPStatusError: any non-2xx status.
    """
    api_key = get_api_key()
    body: dict[str, Any] = {
        "url": url,
        "limit": max_pages,
        "allowExternalLinks": allow_external,
    }
    if max_depth is not None:
        body["maxDepth"] = max_depth

    async with _client() as client:
        resp = await client.post(
            f"{FIRECRAWL_API_BASE}/crawl", json=body, headers=build_headers(api_key)
        )
        resp.raise_for_status()
        data = resp.json()
        if not data.get("success") or not data.get("id"):
            raise FirecrawlApiError(
                data.get("error", "Firecrawl crawl start returned no job id")
            )
        return data["id"]


async def get_crawl(job_id: str) -> dict[str, Any]:
    """GET /v1/crawl/{id}; return the full parsed JSON.

    The caller inspects ``status`` and ``data`` (e.g. ``completed``,
    ``processing``, ``failed``, ``cancelled``).

    Raises:
        httpx.HTTPStatusError: any non-2xx status.
    """
    api_key = get_api_key()
    async with _client() as client:
        resp = await client.get(
            f"{FIRECRAWL_API_BASE}/crawl/{job_id}", headers=build_headers(api_key)
        )
        resp.raise_for_status()
        return resp.json()


async def cancel_crawl(job_id: str) -> bool:
    """DELETE /v1/crawl/{id}; best-effort cancel. Never raises.

    Returns ``True`` on HTTP 200, ``False`` otherwise (including transport
    errors). Safe to call from a ``finally``/fall-through path.
    """
    api_key = get_api_key()
    try:
        async with _client() as client:
            resp = await client.delete(
                f"{FIRECRAWL_API_BASE}/crawl/{job_id}", headers=build_headers(api_key)
            )
            return resp.status_code == 200
    except Exception:
        return False


async def check_health() -> tuple[bool, int, str | None]:
    """Probe /v1/search to verify connectivity.

    Returns ``(healthy, latency_ms, error_message)``. Keyless 403 is treated as
    healthy-but-keyless (keyless is a supported mode) with a note, matching
    ``auth_required=False`` on the source (F11).

    * ``200`` -> ``(True, latency_ms, None)``
    * ``403`` + no key -> ``(True, latency_ms, "keyless, rate-limited per IP")``
    * other status -> ``(False, latency_ms, "HTTP <code>: <text>")``
    * transport error -> ``(False, latency_ms, str(e))``
    """
    api_key = get_api_key()
    start = time.monotonic()
    try:
        async with _client() as client:
            resp = await client.post(
                f"{FIRECRAWL_API_BASE}/search",
                json={"query": "test", "limit": 1},
                headers=build_headers(api_key),
            )
            latency_ms = int((time.monotonic() - start) * 1000)
            if resp.status_code == 200:
                return (True, latency_ms, None)
            if is_keyless_blocked(resp, api_key):
                return (True, latency_ms, "keyless, rate-limited per IP")
            return (
                False,
                latency_ms,
                f"HTTP {resp.status_code}: {resp.text[:200]}",
            )
    except Exception as e:
        latency_ms = int((time.monotonic() - start) * 1000)
        return (False, latency_ms, str(e))
