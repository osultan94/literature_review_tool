"""Stage 6: weight computation and scoring."""

from __future__ import annotations

import json
import math
from pathlib import Path

import structlog

from lit_review import utils
from lit_review.db import get_active_weight_config, get_connection, row_to_paper
from lit_review.models import Verdict

logger = structlog.get_logger()

CURRENT_YEAR = utils.utc_now().year


def _llm_verdict_score(verdict: str | None) -> float:
    if verdict == Verdict.INCLUDE.value:
        return 1.0
    if verdict == Verdict.UNCERTAIN.value:
        return 0.5
    return 0.0


def _citation_score(citation_count: int | None, pub_year: int | None) -> float:
    if not citation_count or not pub_year or pub_year >= CURRENT_YEAR:
        return 0.0
    years_since = max(CURRENT_YEAR - pub_year, 1)
    cpy = citation_count / years_since
    return math.log1p(cpy)


def _recency_score(pub_year: int | None, cutoff_year: int = 2020) -> float:
    if not pub_year:
        return 0.0
    if pub_year < cutoff_year:
        return 0.0
    return min(1.0, (pub_year - cutoff_year) / max(CURRENT_YEAR - cutoff_year, 1))


def _venue_tier_score(tier: int | None) -> float:
    if tier is None:
        return 0.0
    return (tier - 1) / 2.0  # 1->0, 2->0.5, 3->1


def _min_max_normalize(values: list[float]) -> list[float]:
    min_val = min(values)
    max_val = max(values)
    if max_val == min_val:
        return [0.5] * len(values)
    return [(v - min_val) / (max_val - min_val) for v in values]


def compute_scores(
    db_path: Path | str | None = None,
) -> dict[str, int]:
    """Compute per-paper scores using the active weight configuration."""
    with get_connection(db_path) as conn:
        weight_config = get_active_weight_config(conn)
        if weight_config is None:
            raise ValueError("No active weight configuration found.")

        weights = weight_config.component_weights

        rows = conn.execute(
            """
            SELECT p.*, sd.llm_verdict
            FROM papers p
            LEFT JOIN screening_decisions sd
                ON sd.paper_id = p.id
            WHERE sd.id IS NOT NULL OR p.abstract IS NULL
            """
        ).fetchall()
        papers = [(row_to_paper(row), row["llm_verdict"]) for row in rows]

        if not papers:
            return {"scored": 0}

        # Raw component scores per paper
        raw_scores: list[dict[str, float]] = []
        for paper, verdict in papers:
            raw_scores.append(
                {
                    "llm_verdict": _llm_verdict_score(verdict),
                    "citations_per_year": _citation_score(paper.citation_count, paper.pub_year),
                    "recency": _recency_score(paper.pub_year),
                    "venue_tier": _venue_tier_score(paper.venue_tier),
                }
            )

        # Normalize each component independently (min-max across the batch)
        normalized_scores: list[dict[str, float]] = []
        for idx in range(len(raw_scores)):
            normalized_scores.append(
                {
                    key: _min_max_normalize([r[key] for r in raw_scores])[idx]
                    for key in raw_scores[0]
                }
            )

        scored = 0
        for (paper, _verdict), raw, norm in zip(
            papers, raw_scores, normalized_scores, strict=True
        ):
            final = sum(weights.get(key, 0.0) * norm[key] for key in norm)
            breakdown = {"normalized": norm, "raw": raw}

            conn.execute(
                """
                INSERT INTO paper_scores (
                    paper_id, weight_version, llm_verdict_score, citation_score,
                    recency_score, venue_tier_score, final_score, component_breakdown
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(paper_id, weight_version) DO UPDATE SET
                    llm_verdict_score=excluded.llm_verdict_score,
                    citation_score=excluded.citation_score,
                    recency_score=excluded.recency_score,
                    venue_tier_score=excluded.venue_tier_score,
                    final_score=excluded.final_score,
                    component_breakdown=excluded.component_breakdown,
                    computed_at=excluded.computed_at
                """,
                (
                    paper.id,
                    weight_config.version,
                    norm["llm_verdict"],
                    norm["citations_per_year"],
                    norm["recency"],
                    norm["venue_tier"],
                    final,
                    json.dumps(breakdown),
                ),
            )
            scored += 1

        conn.commit()

    logger.info("scores_computed", scored=scored, weight_version=weight_config.version)
    return {"scored": scored}
