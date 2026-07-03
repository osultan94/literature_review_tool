"""Stage 7: saturation check across harvest/snowball rounds."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from lit_review.db import get_connection

DEFAULT_SATURATION_THRESHOLD = 0.05
DEFAULT_CONSECUTIVE_ROUNDS = 2


def check_saturation(
    db_path: Path | str | None = None,
    threshold: float = DEFAULT_SATURATION_THRESHOLD,
    consecutive_rounds: int = DEFAULT_CONSECUTIVE_ROUNDS,
) -> dict[str, Any]:
    """Check whether snowballing/harvesting has saturated.

    Saturation is reached when the ratio of new unique papers to total
    results falls below `threshold` for `consecutive_rounds` in a row.
    """
    with get_connection(db_path) as conn:
        rows = conn.execute(
            """
            SELECT round_number, stage, results_count, new_unique_count, timestamp
            FROM harvest_log
            ORDER BY round_number DESC
            LIMIT ?
            """,
            (max(consecutive_rounds, 1),),
        ).fetchall()

    rounds = [
        {
            "round_number": row["round_number"],
            "stage": row["stage"],
            "results_count": row["results_count"],
            "new_unique_count": row["new_unique_count"],
            "novelty_ratio": (
                row["new_unique_count"] / row["results_count"]
                if row["results_count"]
                else 0.0
            ),
            "timestamp": row["timestamp"],
        }
        for row in reversed(rows)
    ]

    saturated = False
    if len(rounds) >= consecutive_rounds:
        recent = rounds[-consecutive_rounds:]
        saturated = all(r["novelty_ratio"] < threshold for r in recent)

    return {
        "saturated": saturated,
        "threshold": threshold,
        "consecutive_rounds": consecutive_rounds,
        "rounds": rounds,
    }
