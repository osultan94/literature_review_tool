"""Stage 0: seed ingestion and title extraction."""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

import structlog

from lit_review.db import get_connection
from lit_review.llm.client import LLMError, OllamaClient
from lit_review.llm.prompts import title_extraction_prompt
from lit_review.llm.schemas import EXTRACTION_SCHEMA
from lit_review.models import SeedStatus

logger = structlog.get_logger()


async def ingest_seeds(
    csv_path: Path | str,
    db_path: Path | str | None = None,
    llm_client: OllamaClient | None = None,
) -> dict[str, int]:
    """Read a seed CSV, insert raw rows, and extract titles with an LLM.

    The CSV is expected to have a single column of raw references, one per row.
    If the file has a header row, each header cell is also ingested as a raw row.
    """
    csv_path = Path(csv_path)
    if not csv_path.exists():
        raise FileNotFoundError(f"Seed CSV not found: {csv_path}")

    should_close = llm_client is None
    llm_client = llm_client or OllamaClient()

    rows: list[list[str]] = []
    with csv_path.open("r", encoding="utf-8", errors="replace") as f:
        reader = csv.reader(f)
        for row in reader:
            rows.append(row)

    inserted = 0
    with get_connection(db_path) as conn:
        for row in rows:
            raw_text = " ".join(cell.strip() for cell in row if cell.strip())
            if not raw_text:
                continue
            cursor = conn.execute(
                "INSERT INTO seeds (raw_text, status) VALUES (?, ?)",
                (raw_text, SeedStatus.PENDING.value),
            )
            inserted += 1
            seed_id = cursor.lastrowid
            conn.commit()

            try:
                parsed = await llm_client.generate(
                    title_extraction_prompt(raw_text),
                    schema=EXTRACTION_SCHEMA,
                    temperature=0.0,
                )
                extracted_title = str(parsed.get("title", "")).strip()
                confidence = float(parsed.get("confidence", 0.0))
                status = SeedStatus.EXTRACTED if extracted_title else SeedStatus.FAILED
                conn.execute(
                    "UPDATE seeds SET extracted_title=?, extraction_confidence=?, "
                    "status=? WHERE id=?",
                    (extracted_title, confidence, status.value, seed_id),
                )
                logger.info(
                    "seed_extracted",
                    seed_id=seed_id,
                    title=extracted_title,
                    confidence=confidence,
                )
            except LLMError as exc:
                conn.execute(
                    "UPDATE seeds SET status=? WHERE id=?",
                    (SeedStatus.FAILED.value, seed_id),
                )
                logger.error("seed_extraction_failed", seed_id=seed_id, error=str(exc))
            conn.commit()

    if should_close:
        await llm_client.close()

    return {"inserted": inserted}


async def reextract_seed(
    seed_id: int,
    raw_text: str,
    db_path: Path | str | None = None,
    llm_client: OllamaClient | None = None,
) -> dict[str, Any]:
    """Re-run title extraction for a single seed (useful from the UI)."""
    should_close = llm_client is None
    llm_client = llm_client or OllamaClient()
    try:
        parsed = await llm_client.generate(
            title_extraction_prompt(raw_text),
            schema=EXTRACTION_SCHEMA,
            temperature=0.0,
        )
        extracted_title = str(parsed.get("title", "")).strip()
        confidence = float(parsed.get("confidence", 0.0))
        status = SeedStatus.EXTRACTED if extracted_title else SeedStatus.FAILED
        with get_connection(db_path) as conn:
            conn.execute(
                "UPDATE seeds SET extracted_title=?, extraction_confidence=?, status=? WHERE id=?",
                (extracted_title, confidence, status.value, seed_id),
            )
            conn.commit()
        return {
            "seed_id": seed_id,
            "extracted_title": extracted_title,
            "confidence": confidence,
            "status": status.value,
        }
    finally:
        if should_close:
            await llm_client.close()
