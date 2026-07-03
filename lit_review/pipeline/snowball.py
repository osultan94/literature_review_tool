"""Stage 4: backward/forward citation snowballing."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import httpx
import structlog

from lit_review import config, utils
from lit_review.db import get_connection
from lit_review.models import Paper, PaperOrigin, Verdict
from lit_review.sources import openalex, semantic_scholar

logger = structlog.get_logger()


def _candidate_from_s2_item(item: dict[str, Any], direction: PaperOrigin) -> Paper | None:
    """Convert a Semantic Scholar reference/citation item into a Paper."""
    paper_id = item.get("paperId")
    title = item.get("title")
    if not paper_id or not title:
        return None
    authors = [a.get("name", "") for a in item.get("authors", []) if a.get("name")]
    return Paper(
        canonical_title=title,
        doi=(item.get("externalIds") or {}).get("DOI"),
        primary_source_name="semantic_scholar",
        primary_source_id=str(paper_id),
        abstract=item.get("abstract"),
        venue=item.get("venue"),
        venue_tier=_venue_tier(item.get("venue")),
        pub_year=utils.parse_year(item.get("year")),
        citation_count=item.get("citationCount"),
        citation_count_fetched_at=utils.utc_now(),
        authors=authors,
        origin=direction,
        discovery_round=0,  # set by caller
    )


def _candidate_from_openalex_work(work: dict[str, Any], direction: PaperOrigin) -> Paper | None:
    """Convert an OpenAlex work into a Paper."""
    work_id = work.get("id")
    title = work.get("display_name")
    if not work_id or not title:
        return None
    authors = []
    for authorship in work.get("authorships", []):
        author = authorship.get("author") or {}
        if author.get("display_name"):
            authors.append(author["display_name"])
    return Paper(
        canonical_title=title,
        doi=(work.get("ids") or {}).get("doi") or work.get("doi"),
        primary_source_name="openalex",
        primary_source_id=work_id,
        abstract=work.get("abstract"),
        venue=(work.get("primary_location") or {}).get("source", {}).get("display_name"),
        venue_tier=_venue_tier(
            (work.get("primary_location") or {}).get("source", {}).get("display_name")
        ),
        pub_year=utils.parse_year(work.get("publication_year")),
        citation_count=work.get("cited_by_count"),
        citation_count_fetched_at=utils.utc_now(),
        authors=authors,
        origin=direction,
        discovery_round=0,
    )


def _venue_tier(venue: str | None) -> int | None:
    if not venue:
        return None
    venue_lower = venue.lower()
    for substring, tier in config.DEFAULT_VENUE_TIERS.items():
        if substring in venue_lower:
            return tier
    return 1


def _find_existing_paper_id(conn: sqlite3.Connection, paper: Paper) -> int | None:
    """Check for an existing paper by DOI or fuzzy title match."""
    if paper.doi:
        row = conn.execute(
            "SELECT id FROM papers WHERE doi = ? COLLATE NOCASE",
            (paper.doi,),
        ).fetchone()
        if row:
            return int(row["id"])
    # Fuzzy title check against all existing papers
    rows = conn.execute("SELECT id, canonical_title FROM papers").fetchall()
    for row in rows:
        title_sim = utils.title_similarity(row["canonical_title"], paper.canonical_title)
        if title_sim >= config.DEDUPE_TITLE_MIN_RATIO:
            return int(row["id"])
    return None


def _insert_paper(conn: sqlite3.Connection, paper: Paper, round_number: int) -> int:
    """Insert a new paper and its raw source record, returning the new id."""
    cursor = conn.execute(
        """
        INSERT INTO papers (
            canonical_title, doi, primary_source_name, primary_source_id,
            abstract, venue, venue_tier, pub_year, citation_count,
            citation_count_fetched_at, authors, origin, origin_paper_id,
            discovery_round
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            paper.canonical_title,
            paper.doi,
            paper.primary_source_name,
            paper.primary_source_id,
            paper.abstract,
            paper.venue,
            paper.venue_tier,
            paper.pub_year,
            paper.citation_count,
            paper.citation_count_fetched_at,
            utils.to_json_blob(paper.authors),
            paper.origin.value,
            paper.origin_paper_id,
            round_number,
        ),
    )
    paper_id = cursor.lastrowid
    assert paper_id is not None
    conn.execute(
        """
        INSERT INTO paper_sources (paper_id, source_name, source_paper_id, raw_json, fetched_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            paper_id,
            paper.primary_source_name,
            paper.primary_source_id,
            utils.to_json_blob(paper.model_dump()),
            utils.utc_now(),
        ),
    )
    return int(paper_id)


async def run_snowball_round(
    db_path: Path | str | None = None,
    source_paper_ids: list[int] | None = None,
    direction: str = "both",
    limit: int = 100,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """Run one snowballing round from the given source paper IDs.

    Args:
        db_path: SQLite database path.
        source_paper_ids: Papers to expand from. If None, uses papers with
            include/uncertain verdicts under the active criteria.
        direction: "backward", "forward", or "both".
        limit: Max references/citations to fetch per source per direction.
        client: Optional shared HTTP client.
    """
    should_close = client is None
    client = client or httpx.AsyncClient(timeout=config.REQUEST_TIMEOUT)

    with get_connection(db_path) as conn:
        if source_paper_ids is None:
            rows = conn.execute(
                """
                SELECT DISTINCT p.id
                FROM papers p
                JOIN screening_decisions sd ON sd.paper_id = p.id
                WHERE sd.llm_verdict IN (?, ?)
                """,
                (Verdict.INCLUDE.value, Verdict.UNCERTAIN.value),
            ).fetchall()
            source_paper_ids = [int(row["id"]) for row in rows]

        if not source_paper_ids:
            return {"source_count": 0, "new_papers": 0, "round_number": 0}

        # Determine the next round number
        round_row = conn.execute(
            "SELECT COALESCE(MAX(round_number), 0) + 1 FROM harvest_log"
        ).fetchone()
        round_number = int(round_row[0]) if round_row else 1

        new_papers_count = 0
        total_results = 0

        for source_id in source_paper_ids:
            source_row = conn.execute(
                "SELECT primary_source_name, primary_source_id FROM papers WHERE id = ?",
                (source_id,),
            ).fetchone()
            if not source_row or not source_row["primary_source_id"]:
                continue
            source_name = source_row["primary_source_name"]
            source_native_id = source_row["primary_source_id"]

            fetched_items: list[tuple[Paper, str]] = []

            # Backward snowballing
            if direction in ("backward", "both"):
                items: list[dict[str, Any]] = []
                try:
                    if source_name == "semantic_scholar" and config.USE_SEMANTIC_SCHOLAR:
                        items = await semantic_scholar.fetch_references(
                            source_native_id, client=client, limit=limit
                        )
                    elif source_name == "openalex":
                        items = await openalex.fetch_references(
                            source_native_id, client=client, limit=limit
                        )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "snowball_backward_failed", source_id=source_id, error=str(exc)
                    )

                total_results += len(items)
                for item in items:
                    if source_name == "semantic_scholar":
                        paper = _candidate_from_s2_item(item, PaperOrigin.SNOWBALL_BACKWARD)
                    else:
                        paper = _candidate_from_openalex_work(item, PaperOrigin.SNOWBALL_BACKWARD)
                    if paper:
                        paper.origin_paper_id = source_id
                        fetched_items.append((paper, "backward"))

            # Forward snowballing
            if direction in ("forward", "both"):
                items = []
                try:
                    if source_name == "semantic_scholar" and config.USE_SEMANTIC_SCHOLAR:
                        items = await semantic_scholar.fetch_citations(
                            source_native_id, client=client, limit=limit
                        )
                    elif source_name == "openalex":
                        items = await openalex.fetch_citations(
                            source_native_id, client=client, limit=limit
                        )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "snowball_forward_failed", source_id=source_id, error=str(exc)
                    )

                total_results += len(items)
                for item in items:
                    if source_name == "semantic_scholar":
                        paper = _candidate_from_s2_item(item, PaperOrigin.SNOWBALL_FORWARD)
                    else:
                        paper = _candidate_from_openalex_work(item, PaperOrigin.SNOWBALL_FORWARD)
                    if paper:
                        paper.origin_paper_id = source_id
                        fetched_items.append((paper, "forward"))

            for paper, _dir in fetched_items:
                existing_id = _find_existing_paper_id(conn, paper)
                if existing_id:
                    continue
                _insert_paper(conn, paper, round_number)
                new_papers_count += 1

        conn.execute(
            """
            INSERT INTO harvest_log (
                round_number, stage, results_count, new_unique_count, timestamp
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                round_number,
                "snowball_both" if direction == "both" else f"snowball_{direction}",
                total_results,
                new_papers_count,
                utils.utc_now(),
            ),
        )
        conn.commit()

    if should_close:
        await client.aclose()

    logger.info(
        "snowball_round_complete",
        round_number=round_number,
        source_count=len(source_paper_ids),
        new_papers=new_papers_count,
        total_results=total_results,
    )

    return {
        "round_number": round_number,
        "source_count": len(source_paper_ids),
        "new_papers": new_papers_count,
        "total_results": total_results,
    }
