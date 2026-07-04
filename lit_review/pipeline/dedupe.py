"""Stage 3: deduplication of papers."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import structlog

from lit_review import config, utils
from lit_review.db import get_connection, row_to_paper

logger = structlog.get_logger()


def _merge_papers(conn: sqlite3.Connection, keep_id: int, drop_id: int) -> None:
    """Move all references from drop_id to keep_id and delete drop_id."""
    conn.execute(
        "UPDATE seeds SET resolved_paper_id = ? WHERE resolved_paper_id = ?", (keep_id, drop_id)
    )
    # paper_sources has a UNIQUE(paper_id, source_name) constraint. If both papers
    # have a source with the same name, drop the duplicate from the paper being
    # merged away rather than failing the UPDATE.
    conn.execute(
        """
        DELETE FROM paper_sources
        WHERE paper_id = ? AND source_name IN (
            SELECT source_name FROM paper_sources WHERE paper_id = ?
        )
        """,
        (drop_id, keep_id),
    )
    conn.execute(
        "UPDATE paper_sources SET paper_id = ? WHERE paper_id = ?", (keep_id, drop_id)
    )
    conn.execute(
        """
        DELETE FROM screening_decisions
        WHERE paper_id = ? AND criteria_version IN (
            SELECT criteria_version FROM screening_decisions WHERE paper_id = ?
        )
        """,
        (keep_id, drop_id),
    )
    conn.execute(
        "UPDATE screening_decisions SET paper_id = ? WHERE paper_id = ?", (keep_id, drop_id)
    )
    conn.execute(
        "UPDATE papers SET origin_paper_id = ? WHERE origin_paper_id = ?", (keep_id, drop_id)
    )
    conn.execute("DELETE FROM papers WHERE id = ?", (drop_id,))


def deduplicate(
    db_path: Path | str | None = None,
    auto_merge_fuzzy: bool = False,
) -> dict[str, Any]:
    """Deduplicate papers by DOI and fuzzy title/author match.

    DOI matches are always auto-merged. Fuzzy matches are flagged by default;
    set auto_merge_fuzzy=True to merge very high-confidence pairs.
    """
    with get_connection(db_path) as conn:
        rows = conn.execute("SELECT * FROM papers ORDER BY id").fetchall()
        papers = [row_to_paper(row) for row in rows]
        for paper in papers:
            assert paper.id is not None

        merged_count = 0
        flagged_pairs: list[dict[str, Any]] = []
        keep_ids = {p.id for p in papers}

        # Pass 1: exact DOI match
        doi_to_id: dict[str, int] = {}
        for paper in papers:
            paper_id = paper.id
            assert paper_id is not None
            if not paper.doi or paper_id not in keep_ids:
                continue
            doi = paper.doi.lower().strip()
            if doi in doi_to_id:
                keep_id = doi_to_id[doi]
                if paper_id != keep_id and paper_id in keep_ids:
                    _merge_papers(conn, keep_id, paper_id)
                    keep_ids.discard(paper_id)
                    merged_count += 1
                    logger.info("merged_doi_dupes", keep_id=keep_id, drop_id=paper_id, doi=doi)
            else:
                doi_to_id[doi] = paper_id

        # Refresh list after DOI merges
        rows = conn.execute("SELECT * FROM papers ORDER BY id").fetchall()
        papers = [row_to_paper(row) for row in rows]
        for paper in papers:
            assert paper.id is not None

        # Pass 2: fuzzy title + author overlap
        n = len(papers)
        for i in range(n):
            for j in range(i + 1, n):
                a, b = papers[i], papers[j]
                a_id = a.id
                b_id = b.id
                assert a_id is not None and b_id is not None
                if a_id not in keep_ids or b_id not in keep_ids:
                    continue
                title_ratio = utils.title_similarity(a.canonical_title, b.canonical_title)
                author_ratio = utils.author_similarity(a.authors, b.authors)
                if title_ratio < config.DEDUPE_TITLE_MIN_RATIO:
                    continue

                if title_ratio >= 0.98 and author_ratio >= config.DEDUPE_AUTHOR_MIN_RATIO:
                    if auto_merge_fuzzy:
                        keep_id, drop_id = (a_id, b_id) if a_id < b_id else (b_id, a_id)
                        _merge_papers(conn, keep_id, drop_id)
                        keep_ids.discard(drop_id)
                        merged_count += 1
                        logger.info(
                            "merged_fuzzy_dupes",
                            keep_id=keep_id,
                            drop_id=drop_id,
                            title_ratio=title_ratio,
                            author_ratio=author_ratio,
                        )
                    else:
                        flagged_pairs.append(
                            {
                                "paper_a_id": a_id,
                                "paper_b_id": b_id,
                                "title_a": a.canonical_title,
                                "title_b": b.canonical_title,
                                "title_ratio": title_ratio,
                                "author_ratio": author_ratio,
                            }
                        )

        conn.commit()

    return {"merged": merged_count, "flagged_pairs": flagged_pairs}
