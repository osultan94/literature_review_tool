"""Stage 2: metadata harvesting and reconciliation across academic APIs."""

from __future__ import annotations

from collections.abc import Coroutine
from pathlib import Path
from typing import Any

import httpx
import structlog

from lit_review import config, utils
from lit_review.db import get_connection, row_to_paper
from lit_review.models import Paper
from lit_review.sources import arxiv, crossref, openalex, semantic_scholar

logger = structlog.get_logger()


def _s2_normalize(raw: dict[str, Any]) -> dict[str, Any]:
    authors = [a.get("name", "") for a in raw.get("authors", []) if a.get("name")]
    return {
        "title": raw.get("title"),
        "doi": (raw.get("externalIds") or {}).get("DOI"),
        "abstract": raw.get("abstract"),
        "venue": raw.get("venue"),
        "pub_year": utils.parse_year(raw.get("year")),
        "citation_count": raw.get("citationCount"),
        "authors": authors,
    }


def _openalex_normalize(raw: dict[str, Any]) -> dict[str, Any]:
    authors = []
    for authorship in raw.get("authorships", []):
        author = authorship.get("author") or {}
        if author.get("display_name"):
            authors.append(author["display_name"])
    return {
        "title": raw.get("display_name"),
        "doi": (raw.get("ids") or {}).get("doi") or raw.get("doi"),
        "abstract": raw.get("abstract"),
        "venue": (raw.get("primary_location") or {}).get("source", {}).get("display_name"),
        "pub_year": utils.parse_year(raw.get("publication_year")),
        "citation_count": raw.get("cited_by_count"),
        "authors": authors,
    }


def _crossref_normalize(raw: dict[str, Any]) -> dict[str, Any]:
    authors = []
    for author in raw.get("author", []):
        name = f"{author.get('given', '')} {author.get('family', '')}".strip()
        if name:
            authors.append(name)
    title = ""
    if raw.get("title"):
        title = raw["title"][0]
    elif raw.get("original-title"):
        title = raw["original-title"][0]
    return {
        "title": title,
        "doi": raw.get("DOI"),
        "abstract": raw.get("abstract"),
        "venue": (raw.get("container-title") or [None])[0],
        "pub_year": utils.parse_year(
            raw.get("published-print") or raw.get("published-online") or raw.get("published")
        ),
        "citation_count": raw.get("is-referenced-by-count"),
        "authors": authors,
    }


def _arxiv_normalize(raw: dict[str, Any]) -> dict[str, Any]:
    return {
        "title": raw.get("title"),
        "doi": raw.get("doi"),
        "abstract": raw.get("summary"),
        "venue": "arXiv",
        "pub_year": utils.parse_year(raw.get("published", "")),
        "citation_count": None,
        "authors": [a.get("name", "") for a in raw.get("authors", []) if a.get("name")],
    }


_NORMALIZERS = {
    "semantic_scholar": _s2_normalize,
    "openalex": _openalex_normalize,
    "crossref": _crossref_normalize,
    "arxiv": _arxiv_normalize,
}


def _reconcile(sources: list[dict[str, Any]]) -> dict[str, Any]:
    """Pick the highest-priority non-empty value per field."""
    normalized = [
        {"source_name": s["source_name"], **_NORMALIZERS[s["source_name"]](s["raw"])}
        for s in sources
        if s["source_name"] in _NORMALIZERS
    ]
    reconciled: dict[str, Any] = {}
    for field, priority in config.SOURCE_PRIORITY.items():
        for source_name in priority:
            for record in normalized:
                if record["source_name"] == source_name:
                    value = record.get(field)
                    if value is not None and value != "":
                        reconciled[field] = value
                        break
            if field in reconciled:
                break
    # Determine primary source name from where the winning title came.
    if "title" in reconciled:
        for record in normalized:
            if record.get("title") == reconciled["title"]:
                reconciled["primary_source_name"] = record["source_name"]
                break
    return reconciled


async def _harvest_paper(
    paper: Paper,
    client: httpx.AsyncClient,
) -> list[dict[str, Any]]:
    """Fetch raw metadata for a paper from all sources."""
    sources: list[dict[str, Any]] = []

    async def _add_source(
        name: str, coro: Coroutine[Any, Any, dict[str, Any] | None]
    ) -> None:
        try:
            result = await coro
        except Exception as exc:  # noqa: BLE001
            logger.warning("harvest_source_failed", paper_id=paper.id, source=name, error=str(exc))
            return
        if result is None:
            return
        sources.append({"source_name": name, "raw": result})

    if (
        config.USE_SEMANTIC_SCHOLAR
        and paper.primary_source_name == "semantic_scholar"
        and paper.primary_source_id
    ):
        await _add_source(
            "semantic_scholar", semantic_scholar.fetch_paper(paper.primary_source_id, client=client)
        )
    if paper.doi:
        await _add_source("crossref", crossref.fetch_work(paper.doi, client=client))
        await _add_source("openalex", openalex.fetch_work(f"doi:{paper.doi}", client=client))

    # Always query arXiv by title as a cross-check / fallback.
    try:
        arxiv_candidates = await arxiv.search_by_title(
            paper.canonical_title, limit=1, client=client
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("harvest_source_failed", paper_id=paper.id, source="arxiv", error=str(exc))
        arxiv_candidates = []
    if arxiv_candidates and arxiv_candidates[0].similarity_ratio >= config.TITLE_MATCH_MIN_RATIO:
        sources.append(
            {
                "source_name": "arxiv",
                "raw": arxiv_candidates[0].model_dump(),
            }
        )

    return sources


async def harvest_metadata(
    db_path: Path | str | None = None,
    client: httpx.AsyncClient | None = None,
) -> dict[str, int]:
    """Harvest and reconcile metadata for all resolved papers."""
    should_close = client is None
    client = client or httpx.AsyncClient(timeout=config.REQUEST_TIMEOUT)

    with get_connection(db_path) as conn:
        rows = conn.execute("SELECT * FROM papers").fetchall()
        papers = [row_to_paper(row) for row in rows]

        harvested = 0
        for paper in papers:
            sources = await _harvest_paper(paper, client)
            if not sources:
                continue

            for source in sources:
                conn.execute(
                    """
                    INSERT INTO paper_sources (
                        paper_id, source_name, source_paper_id, raw_json, fetched_at
                    )
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(paper_id, source_name) DO UPDATE SET
                        raw_json=excluded.raw_json,
                        fetched_at=excluded.fetched_at
                    """,
                    (
                        paper.id,
                        source["source_name"],
                        _extract_source_id(source),
                        utils.to_json_blob(source["raw"]),
                        utils.utc_now(),
                    ),
                )

            reconciled = _reconcile(sources)
            conn.execute(
                """
                UPDATE papers SET
                    canonical_title = COALESCE(?, canonical_title),
                    doi = COALESCE(?, doi),
                    abstract = COALESCE(?, abstract),
                    venue = COALESCE(?, venue),
                    venue_tier = COALESCE(?, venue_tier),
                    pub_year = COALESCE(?, pub_year),
                    citation_count = COALESCE(?, citation_count),
                    citation_count_fetched_at = COALESCE(?, citation_count_fetched_at),
                    authors = COALESCE(?, authors),
                    primary_source_name = COALESCE(?, primary_source_name),
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    reconciled.get("title"),
                    reconciled.get("doi"),
                    reconciled.get("abstract"),
                    reconciled.get("venue"),
                    _venue_tier(reconciled.get("venue")),
                    reconciled.get("pub_year"),
                    reconciled.get("citation_count"),
                    utils.utc_now() if reconciled.get("citation_count") is not None else None,
                    (
                        utils.to_json_blob(reconciled.get("authors"))
                        if reconciled.get("authors")
                        else None
                    ),
                    reconciled.get("primary_source_name"),
                    utils.utc_now(),
                    paper.id,
                ),
            )
            conn.commit()
            harvested += 1
            logger.info("paper_harvested", paper_id=paper.id, sources=len(sources))

    if should_close:
        await client.aclose()

    return {"harvested": harvested}


def _extract_source_id(source: dict[str, Any]) -> str:
    raw = source["raw"]
    name = source["source_name"]
    if name == "semantic_scholar":
        return str(raw.get("paperId", ""))
    if name == "openalex":
        return str(raw.get("id", ""))
    if name == "crossref":
        return str(raw.get("DOI", ""))
    if name == "arxiv":
        return str(raw.get("id", ""))
    return ""


def _venue_tier(venue: str | None) -> int | None:
    if not venue:
        return None
    venue_lower = venue.lower()
    for substring, tier in config.DEFAULT_VENUE_TIERS.items():
        if substring in venue_lower:
            return tier
    return 1
