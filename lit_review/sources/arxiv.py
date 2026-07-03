"""arXiv API client."""

from __future__ import annotations

from typing import Any

import httpx

from lit_review import config, utils
from lit_review.models import ResolvedCandidate

BASE_URL = "https://export.arxiv.org/api/query"


def _to_candidate(entry: dict[str, Any], query_title: str) -> ResolvedCandidate:
    authors = [a.get("name", "") for a in entry.get("authors", []) if a.get("name")]
    title = entry.get("title", "").replace("\n", " ").strip()
    return ResolvedCandidate(
        source_name="arxiv",
        source_paper_id=entry.get("id", ""),
        title=title,
        doi=entry.get("doi"),
        authors=authors,
        pub_year=utils.parse_year(entry.get("published", "")),
        abstract=(entry.get("summary") or "").replace("\n", " ").strip(),
        venue="arXiv",
        citation_count=None,
        similarity_ratio=utils.title_similarity(query_title, title),
    )


def _parse_atom(xml_text: str) -> list[dict[str, Any]]:
    """Very small Atom XML parser for arXiv responses."""
    import xml.etree.ElementTree as ET

    ns = {"atom": "http://www.w3.org/2005/Atom"}
    root = ET.fromstring(xml_text)
    entries = []

    def _text(entry: Any, tag: str) -> str:
        el = entry.find(tag, ns)
        return (el.text or "").strip() if el is not None and el.text else ""

    for entry in root.findall("atom:entry", ns):
        if entry.find("atom:title", ns) is None:
            continue
        authors = []
        for author in entry.findall("atom:author", ns):
            name_el = author.find("atom:name", ns)
            if name_el is not None and name_el.text:
                authors.append({"name": name_el.text})
        doi_el = entry.find("atom:doi", ns)
        if doi_el is None:
            # arXiv puts DOI in arxiv:doi sometimes
            doi_el = entry.find("{http://arxiv.org/schemas/atom}doi", ns)

        entries.append(
            {
                "id": _text(entry, "atom:id"),
                "title": _text(entry, "atom:title"),
                "summary": _text(entry, "atom:summary"),
                "published": _text(entry, "atom:published"),
                "authors": authors,
                "doi": doi_el.text.strip() if doi_el is not None and doi_el.text else None,
            }
        )
    return entries


async def search_by_title(
    title: str,
    limit: int = 5,
    client: httpx.AsyncClient | None = None,
) -> list[ResolvedCandidate]:
    should_close = client is None
    client = client or httpx.AsyncClient(timeout=config.REQUEST_TIMEOUT)
    try:
        response = await client.get(
            BASE_URL,
            params={
                "search_query": f"ti:{title}",
                "start": 0,
                "max_results": limit,
                "sortBy": "relevance",
                "sortOrder": "descending",
            },
        )
        response.raise_for_status()
        entries = _parse_atom(response.text)
        candidates = [_to_candidate(entry, title) for entry in entries]
        candidates.sort(key=lambda c: c.similarity_ratio, reverse=True)
        return candidates
    finally:
        if should_close:
            await client.aclose()
