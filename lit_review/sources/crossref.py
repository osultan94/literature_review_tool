"""CrossRef API client."""

from __future__ import annotations

from typing import Any, cast

import httpx

from lit_review import config, utils
from lit_review.models import ResolvedCandidate

BASE_URL = "https://api.crossref.org/works"


def _params() -> dict[str, str]:
    params: dict[str, str] = {}
    if config.CROSSREF_EMAIL:
        params["mailto"] = config.CROSSREF_EMAIL
    return params


def _to_candidate(item: dict[str, Any], query_title: str) -> ResolvedCandidate:
    authors = []
    for author in item.get("author") or []:
        given = author.get("given", "")
        family = author.get("family", "")
        name = f"{given} {family}".strip()
        if name:
            authors.append(name)
    title = ""
    if item.get("title"):
        title = item["title"][0]
    elif item.get("original-title"):
        title = item["original-title"][0]
    return ResolvedCandidate(
        source_name="crossref",
        source_paper_id=item.get("DOI", ""),
        title=title,
        doi=item.get("DOI"),
        authors=authors,
        pub_year=utils.parse_year(
            item.get("published-print") or item.get("published-online") or item.get("published")
        ),
        abstract=item.get("abstract"),
        venue=(item.get("container-title") or [None])[0],
        citation_count=item.get("is-referenced-by-count"),
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
        params = _params()
        params["query.title"] = title
        params["rows"] = str(limit)
        response = await client.get(
            BASE_URL,
            params=params,
        )
        response.raise_for_status()
        items = response.json().get("message", {}).get("items", [])
        candidates = [_to_candidate(item, title) for item in items]
        candidates.sort(key=lambda c: c.similarity_ratio, reverse=True)
        return candidates
    finally:
        if should_close:
            await client.aclose()


async def fetch_work(
    doi: str,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any] | None:
    should_close = client is None
    client = client or httpx.AsyncClient(timeout=config.REQUEST_TIMEOUT)
    try:
        params = _params()
        response = await client.get(
            f"{BASE_URL}/{doi}",
            params=params,
        )
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return cast(dict[str, Any], response.json().get("message", {}))
    finally:
        if should_close:
            await client.aclose()
