"""Aggregate academic source search / fetch helpers."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

from lit_review.models import ResolvedCandidate
from lit_review.sources import arxiv, crossref, openalex, semantic_scholar

__all__ = ["search_all", "fetch_all"]


async def search_all(
    title: str,
    client: httpx.AsyncClient | None = None,
) -> list[ResolvedCandidate]:
    """Query all configured sources in parallel and merge candidates."""
    coros = [
        semantic_scholar.search_by_title(title, client=client),
        openalex.search_by_title(title, client=client),
        crossref.search_by_title(title, client=client),
        arxiv.search_by_title(title, client=client),
    ]
    results = await asyncio.gather(*coros, return_exceptions=True)
    candidates: list[ResolvedCandidate] = []
    for result in results:
        if isinstance(result, Exception):
            # Log and continue; don't let one source kill resolution.
            continue
        if isinstance(result, list):
            candidates.extend(result)
    candidates.sort(key=lambda c: c.similarity_ratio, reverse=True)
    return candidates


async def fetch_all(
    source_name: str,
    source_paper_id: str,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any] | None:
    """Fetch raw metadata from a specific source by its native ID."""
    if source_name == "semantic_scholar":
        return await semantic_scholar.fetch_paper(source_paper_id, client=client)
    if source_name == "openalex":
        return await openalex.fetch_work(source_paper_id, client=client)
    if source_name == "crossref":
        return await crossref.fetch_work(source_paper_id, client=client)
    # arXiv fetch not needed for Milestone 1; search provides enough metadata.
    return None
