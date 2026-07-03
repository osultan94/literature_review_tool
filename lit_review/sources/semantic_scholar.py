"""Semantic Scholar API client."""

from __future__ import annotations

from typing import Any, cast

import httpx

from lit_review import config, utils
from lit_review.models import ResolvedCandidate

BASE_URL = "https://api.semanticscholar.org/graph/v1"
PAPER_FIELDS = (
    "paperId,title,abstract,venue,year,publicationDate,authors,citationCount,externalIds"
)


def _headers() -> dict[str, str]:
    headers = {}
    if config.SEMANTIC_SCHOLAR_API_KEY:
        headers["x-api-key"] = config.SEMANTIC_SCHOLAR_API_KEY
    return headers


def _extract_doi(external_ids: dict[str, Any] | None) -> str | None:
    if not external_ids:
        return None
    return external_ids.get("DOI") or external_ids.get("doi")


def _to_candidate(item: dict[str, Any], query_title: str) -> ResolvedCandidate:
    authors = []
    for author in item.get("authors") or []:
        name = author.get("name")
        if name:
            authors.append(name)
    title = item.get("title") or ""
    return ResolvedCandidate(
        source_name="semantic_scholar",
        source_paper_id=str(item.get("paperId", "")),
        title=title,
        doi=_extract_doi(item.get("externalIds")),
        authors=authors,
        pub_year=utils.parse_year(item.get("year")),
        abstract=item.get("abstract"),
        venue=item.get("venue"),
        citation_count=item.get("citationCount"),
        similarity_ratio=utils.title_similarity(query_title, title),
    )


async def search_by_title(
    title: str,
    limit: int = 5,
    client: httpx.AsyncClient | None = None,
) -> list[ResolvedCandidate]:
    """Search Semantic Scholar by title and return ranked candidates."""
    should_close = client is None
    client = client or httpx.AsyncClient(timeout=config.REQUEST_TIMEOUT)
    try:
        response = await client.get(
            f"{BASE_URL}/paper/search",
            headers=_headers(),
            params={
                "query": title,
                "fields": PAPER_FIELDS,
                "limit": limit,
            },
        )
        response.raise_for_status()
        data = response.json()
        items = data.get("data", [])
        candidates = [_to_candidate(item, title) for item in items if item.get("paperId")]
        candidates.sort(key=lambda c: c.similarity_ratio, reverse=True)
        return candidates
    finally:
        if should_close:
            await client.aclose()


async def fetch_paper(
    paper_id: str,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any] | None:
    """Fetch full paper metadata by Semantic Scholar paper ID."""
    should_close = client is None
    client = client or httpx.AsyncClient(timeout=config.REQUEST_TIMEOUT)
    try:
        response = await client.get(
            f"{BASE_URL}/paper/{paper_id}",
            headers=_headers(),
            params={"fields": PAPER_FIELDS},
        )
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return cast(dict[str, Any], response.json())
    finally:
        if should_close:
            await client.aclose()


async def fetch_citations(
    paper_id: str,
    client: httpx.AsyncClient | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Fetch papers that cite this paper (forward snowball)."""
    should_close = client is None
    client = client or httpx.AsyncClient(timeout=config.REQUEST_TIMEOUT)
    try:
        response = await client.get(
            f"{BASE_URL}/paper/{paper_id}/citations",
            headers=_headers(),
            params={"fields": PAPER_FIELDS, "limit": limit},
        )
        response.raise_for_status()
        return [entry.get("citingPaper", {}) for entry in response.json().get("data", [])]
    finally:
        if should_close:
            await client.aclose()


async def fetch_references(
    paper_id: str,
    client: httpx.AsyncClient | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Fetch papers referenced by this paper (backward snowball)."""
    should_close = client is None
    client = client or httpx.AsyncClient(timeout=config.REQUEST_TIMEOUT)
    try:
        response = await client.get(
            f"{BASE_URL}/paper/{paper_id}/references",
            headers=_headers(),
            params={"fields": PAPER_FIELDS, "limit": limit},
        )
        response.raise_for_status()
        return [entry.get("citedPaper", {}) for entry in response.json().get("data", [])]
    finally:
        if should_close:
            await client.aclose()
