"""Stage 8: CSV artifact generation."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

from lit_review import config
from lit_review.db import get_connection


def _decode_json(value: str | None) -> Any:
    if value is None:
        return {}
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return {}


def export_comprehensive(
    output_path: Path | str | None = None,
    db_path: Path | str | None = None,
) -> Path:
    """Export a comprehensive audit CSV of all papers and their decisions."""
    output_path = Path(output_path or config.DATA_DIR / "comprehensive.csv")
    output_path.parent.mkdir(parents=True, exist_ok=True)

    fieldnames = [
        "paper_id",
        "canonical_title",
        "doi",
        "venue",
        "venue_tier",
        "pub_year",
        "citation_count",
        "authors",
        "origin",
        "discovery_round",
        "criteria_version",
        "llm_verdict",
        "llm_reason",
        "model_name",
        "human_override",
        "human_note",
        "weight_version",
        "final_score",
        "llm_verdict_score",
        "citation_score",
        "recency_score",
        "venue_tier_score",
        "component_breakdown",
        "sources",
    ]

    with get_connection(db_path) as conn:
        rows = conn.execute(
            """
            SELECT
                p.id AS paper_id,
                p.canonical_title,
                p.doi,
                p.venue,
                p.venue_tier,
                p.pub_year,
                p.citation_count,
                p.authors,
                p.origin,
                p.discovery_round,
                sd.criteria_version,
                sd.llm_verdict,
                sd.llm_reason,
                sd.model_name,
                sd.human_override,
                sd.human_note,
                ps.weight_version,
                ps.final_score,
                ps.llm_verdict_score,
                ps.citation_score,
                ps.recency_score,
                ps.venue_tier_score,
                ps.component_breakdown,
                GROUP_CONCAT(DISTINCT psrc.source_name) AS sources
            FROM papers p
            LEFT JOIN screening_decisions sd ON sd.paper_id = p.id
            LEFT JOIN paper_scores ps ON ps.paper_id = p.id
            LEFT JOIN paper_sources psrc ON psrc.paper_id = p.id
            GROUP BY p.id
            ORDER BY COALESCE(ps.final_score, 0) DESC, p.canonical_title ASC
            """
        ).fetchall()

    with output_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            row_dict = dict(row)
            row_dict["authors"] = "; ".join(_decode_json(row_dict.get("authors")))
            breakdown = _decode_json(row_dict.get("component_breakdown"))
            row_dict["component_breakdown"] = json.dumps(breakdown)
            writer.writerow(row_dict)

    return output_path


def export_final_ranked(
    output_path: Path | str | None = None,
    db_path: Path | str | None = None,
) -> Path:
    """Export the final ranked shortlist (include verdicts / overrides only)."""
    output_path = Path(output_path or config.DATA_DIR / "final_ranked.csv")
    output_path.parent.mkdir(parents=True, exist_ok=True)

    fieldnames = [
        "rank",
        "paper_id",
        "canonical_title",
        "doi",
        "venue",
        "venue_tier",
        "pub_year",
        "citation_count",
        "authors",
        "origin",
        "discovery_round",
        "llm_verdict",
        "llm_reason",
        "human_override",
        "final_score",
        "component_breakdown",
        "sources",
    ]

    with get_connection(db_path) as conn:
        rows = conn.execute(
            """
            SELECT
                p.id AS paper_id,
                p.canonical_title,
                p.doi,
                p.venue,
                p.venue_tier,
                p.pub_year,
                p.citation_count,
                p.authors,
                p.origin,
                p.discovery_round,
                sd.llm_verdict,
                sd.llm_reason,
                sd.human_override,
                ps.final_score,
                ps.component_breakdown,
                GROUP_CONCAT(DISTINCT psrc.source_name) AS sources
            FROM papers p
            JOIN screening_decisions sd ON sd.paper_id = p.id
            LEFT JOIN paper_scores ps ON ps.paper_id = p.id
            LEFT JOIN paper_sources psrc ON psrc.paper_id = p.id
            WHERE COALESCE(sd.human_override, sd.llm_verdict) IN (?, ?)
            GROUP BY p.id
            ORDER BY COALESCE(ps.final_score, 0) DESC, p.canonical_title ASC
            """,
            ("include", "uncertain"),
        ).fetchall()

    with output_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for rank, row in enumerate(rows, start=1):
            row_dict = dict(row)
            row_dict["rank"] = rank
            row_dict["authors"] = "; ".join(_decode_json(row_dict.get("authors")))
            breakdown = _decode_json(row_dict.get("component_breakdown"))
            row_dict["component_breakdown"] = json.dumps(breakdown)
            writer.writerow(row_dict)

    return output_path
