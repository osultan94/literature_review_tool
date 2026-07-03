"""OpenAlex API client."""

from __future__ import annotations

from typing import Any, cast

import httpx

from lit_review import config, utils
from lit_review.models import ResolvedCandidate

BASE_URL = "https://api.openalex.org"


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
    return ResolvedCandidate(
        source_name="openalex",
        source_paper_id=item.get("id", ""),
        title=title,
        doi=_extract_doi(item),
        authors=authors,
        pub_year=utils.parse_year(item.get("publication_year")),
        abstract=item.get("abstract"),
        venue=(item.get("primary_location") or {}).get("source", {}).get("display_name"),
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
        response = await client.get(
            work_id if work_id.startswith("http") else f"{BASE_URL}/works/{work_id}",
            params={"mailto": config.OPENALEX_EMAIL} if config.OPENALEX_EMAIL else {},
        )
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return cast(dict[str, Any], response.json())
    finally:
        if should_close:
            await client.aclose()
