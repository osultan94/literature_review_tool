"""OpenAlex API client."""

from __future__ import annotations

import asyncio
from typing import Any, cast

import httpx

from lit_review import config, utils
from lit_review.models import ResolvedCandidate

BASE_URL = "https://api.openalex.org"

# OpenAlex asks polite users to stay under ~10 req/s. We target 8 req/s to leave
# headroom for network jitter and shared clients.
_rate_limiter = utils.RateLimiter(rate_per_second=8.0)


def _params() -> dict[str, str]:
    params = {"per-page": "5"}
    if config.OPENALEX_EMAIL:
        params["mailto"] = config.OPENALEX_EMAIL
    return params


def _extract_doi(work: dict[str, Any]) -> str | None:
    ids = work.get("ids") or {}
    return ids.get("doi") or work.get("doi")


def _to_candidate(item: dict[str, Any], query_title: str) -> ResolvedCandidate:
    authors = []
    for authorship in item.get("authorships") or []:
        author = authorship.get("author") or {}
        name = author.get("display_name")
        if name:
            authors.append(name)
    title = item.get("display_name") or ""
    primary_location = item.get("primary_location") or {}
    source = primary_location.get("source") or {}
    venue = source.get("display_name")
    return ResolvedCandidate(
        source_name="openalex",
        source_paper_id=item.get("id", ""),
        title=title,
        doi=_extract_doi(item),
        authors=authors,
        pub_year=utils.parse_year(item.get("publication_year")),
        abstract=item.get("abstract"),
        venue=venue,
        citation_count=item.get("cited_by_count"),
        similarity_ratio=utils.title_similarity(query_title, title),
    )


async def search_by_title(
    title: str,
    limit: int = 5,
    client: httpx.AsyncClient | None = None,
) -> list[ResolvedCandidate]:
    should_close = client is None
    client = client or httpx.AsyncClient(timeout=config.REQUEST_TIMEOUT)
    try:
        await _rate_limiter.acquire()
        params = _params()
        params["search"] = title
        params["per-page"] = str(limit)
        response = await client.get(
            f"{BASE_URL}/works",
            params=params,
        )
        response.raise_for_status()
        items = response.json().get("results", [])
        candidates = [_to_candidate(item, title) for item in items if item.get("id")]
        candidates.sort(key=lambda c: c.similarity_ratio, reverse=True)
        return candidates
    finally:
        if should_close:
            await client.aclose()


async def fetch_work(
    work_id: str,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any] | None:
    should_close = client is None
    client = client or httpx.AsyncClient(timeout=config.REQUEST_TIMEOUT)
    try:
        await _rate_limiter.acquire()
        # referenced_works returns canonical https://openalex.org/W... URLs.
        # Always route through the API endpoint to avoid 403s from the web URL.
        normalized_id = _normalize_work_id(work_id)
        response = await client.get(
            f"{BASE_URL}/works/{normalized_id}",
            params={"mailto": config.OPENALEX_EMAIL} if config.OPENALEX_EMAIL else {},
        )
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return cast(dict[str, Any], response.json())
    finally:
        if should_close:
            await client.aclose()


async def fetch_references(
    work_id: str,
    client: httpx.AsyncClient | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Fetch works referenced by the given OpenAlex work (backward snowball)."""
    should_close = client is None
    client = client or httpx.AsyncClient(timeout=config.REQUEST_TIMEOUT)
    try:
        work = await fetch_work(work_id, client=client)
        if not work:
            return []
        refs = [_normalize_work_id(rid) for rid in work.get("referenced_works", [])[:limit]]
        if not refs:
            return []
        # Fetch each referenced work in parallel batches to be polite
        results: list[dict[str, Any]] = []
        batch_size = 10
        for i in range(0, len(refs), batch_size):
            batch = refs[i : i + batch_size]
            batch_results = await asyncio.gather(
                *[fetch_work(rid, client=client) for rid in batch],
                return_exceptions=True,
            )
            for result in batch_results:
                if isinstance(result, dict):
                    results.append(result)
                # Ignore exceptions and None values
        return results
    finally:
        if should_close:
            await client.aclose()


def _normalize_work_id(work_id: str) -> str:
    """Return the bare OpenAlex work ID, stripping the canonical URL prefix."""
    prefix = "https://openalex.org/"
    if work_id.startswith(prefix):
        return work_id[len(prefix) :]
    return work_id


async def fetch_citations(
    work_id: str,
    client: httpx.AsyncClient | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Fetch works that cite the given OpenAlex work (forward snowball)."""
    should_close = client is None
    client = client or httpx.AsyncClient(timeout=config.REQUEST_TIMEOUT)
    try:
        params: dict[str, Any] = {
            "filter": f"cites:{_normalize_work_id(work_id)}",
            "per-page": min(limit, 200),
        }
        if config.OPENALEX_EMAIL:
            params["mailto"] = config.OPENALEX_EMAIL
        await _rate_limiter.acquire()
        response = await client.get(f"{BASE_URL}/works", params=params)
        response.raise_for_status()
        data = cast(dict[str, Any], response.json())
        return [item for item in data.get("results", []) if isinstance(item, dict)]
    finally:
        if should_close:
            await client.aclose()
