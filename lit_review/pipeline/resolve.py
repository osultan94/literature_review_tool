"""Stage 1: paper resolution (title -> canonical paper)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import httpx
import structlog

from lit_review import config, utils
from lit_review.db import get_connection
from lit_review.models import Paper, PaperOrigin, ResolvedCandidate, SeedStatus
from lit_review.sources import search_all

logger = structlog.get_logger()


def _venue_tier(venue: str | None) -> int | None:
    if not venue:
        return None
    venue_lower = venue.lower()
    for substring, tier in config.DEFAULT_VENUE_TIERS.items():
        if substring in venue_lower:
            return tier
    return 1


def _paper_from_candidate(candidate: ResolvedCandidate, origin: PaperOrigin) -> Paper:
    return Paper(
        canonical_title=candidate.title,
        doi=candidate.doi,
        primary_source_name=candidate.source_name,
        primary_source_id=candidate.source_paper_id,
        abstract=candidate.abstract,
        venue=candidate.venue,
        venue_tier=_venue_tier(candidate.venue),
        pub_year=candidate.pub_year,
        citation_count=candidate.citation_count,
        citation_count_fetched_at=utils.utc_now(),
        authors=candidate.authors,
        origin=origin,
        discovery_round=0,
    )


def _find_existing_paper(conn: sqlite3.Connection, candidate: ResolvedCandidate) -> int | None:
    if candidate.doi:
        row = conn.execute(
            "SELECT id FROM papers WHERE doi = ? COLLATE NOCASE",
            (candidate.doi,),
        ).fetchone()
        if row:
            return int(row["id"])
    return None


async def resolve_seeds(
    db_path: Path | str | None = None,
    client: httpx.AsyncClient | None = None,
) -> dict[str, int]:
    """Resolve all extracted seeds to papers across academic APIs."""
    should_close = client is None
    client = client or httpx.AsyncClient(timeout=config.REQUEST_TIMEOUT)

    with get_connection(db_path) as conn:
        seed_rows = conn.execute(
            "SELECT id, extracted_title FROM seeds WHERE status = ?",
            (SeedStatus.EXTRACTED.value,),
        ).fetchall()

        resolved = 0
        ambiguous = 0
        failed = 0

        for seed_row in seed_rows:
            seed_id = seed_row["id"]
            title = seed_row["extracted_title"]
            if not title:
                continue

            candidates = await search_all(title, client=client)
            if not candidates:
                conn.execute(
                    "UPDATE seeds SET status = ? WHERE id = ?",
                    (SeedStatus.FAILED.value, seed_id),
                )
                failed += 1
                conn.commit()
                continue

            best = candidates[0]
            if best.similarity_ratio >= config.TITLE_MATCH_AMBIGUOUS_RATIO:
                status = SeedStatus.RESOLVED
            elif best.similarity_ratio >= config.TITLE_MATCH_MIN_RATIO:
                status = SeedStatus.AMBIGUOUS
            else:
                status = SeedStatus.FAILED

            if status == SeedStatus.FAILED:
                conn.execute(
                    "UPDATE seeds SET status = ? WHERE id = ?",
                    (status.value, seed_id),
                )
                failed += 1
                conn.commit()
                continue

            existing_id = _find_existing_paper(conn, best)
            paper_id: int
            if existing_id:
                paper_id = existing_id
            else:
                paper = _paper_from_candidate(best, PaperOrigin.SEED)
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
                        paper.discovery_round,
                    ),
                )
                new_id = cursor.lastrowid
                assert new_id is not None
                paper_id = new_id
                conn.execute(
                    """
                    INSERT INTO paper_sources (
                        paper_id, source_name, source_paper_id, raw_json, fetched_at
                    )
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        paper_id,
                        best.source_name,
                        best.source_paper_id,
                        utils.to_json_blob(best.model_dump()),
                        utils.utc_now(),
                    ),
                )

            conn.execute(
                "UPDATE seeds SET status = ?, resolved_paper_id = ? WHERE id = ?",
                (status.value, paper_id, seed_id),
            )
            conn.commit()

            if status == SeedStatus.RESOLVED:
                resolved += 1
            else:
                ambiguous += 1

            logger.info(
                "seed_resolved",
                seed_id=seed_id,
                paper_id=paper_id,
                title=title,
                best_match=best.title,
                similarity=best.similarity_ratio,
                status=status.value,
            )

    if should_close:
        await client.aclose()

    return {"resolved": resolved, "ambiguous": ambiguous, "failed": failed}
