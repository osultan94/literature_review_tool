"""Stage 5: LLM abstract screening against versioned criteria."""

from __future__ import annotations

import json
from pathlib import Path

import structlog

from lit_review import utils
from lit_review.db import get_active_criteria, get_connection, row_to_paper
from lit_review.llm.client import LLMError, OllamaClient
from lit_review.llm.prompts import screening_prompt
from lit_review.llm.schemas import SCREENING_SCHEMA
from lit_review.models import ScreeningDecision, Verdict

logger = structlog.get_logger()


async def screen_papers(
    db_path: Path | str | None = None,
    llm_client: OllamaClient | None = None,
) -> dict[str, int]:
    """Screen all unscreened papers with an abstract using the active criteria."""
    should_close = llm_client is None
    llm_client = llm_client or OllamaClient()

    with get_connection(db_path) as conn:
        criteria = get_active_criteria(conn)
        if criteria is None:
            raise ValueError("No active criteria version found.")

        rows = conn.execute(
            """
            SELECT p.* FROM papers p
            LEFT JOIN screening_decisions sd
                ON sd.paper_id = p.id AND sd.criteria_version = ?
            WHERE p.abstract IS NOT NULL AND p.abstract != ''
              AND sd.id IS NULL
            """,
            (criteria.version,),
        ).fetchall()
        papers = [row_to_paper(row) for row in rows]

        screened = 0
        skipped = 0
        for paper in papers:
            if not paper.abstract:
                skipped += 1
                continue

            try:
                parsed = await llm_client.generate(
                    screening_prompt(paper.abstract, criteria.criteria_text),
                    schema=SCREENING_SCHEMA,
                    temperature=0.0,
                )
            except LLMError as exc:
                logger.error("screening_failed", paper_id=paper.id, error=str(exc))
                skipped += 1
                continue

            try:
                verdict = Verdict(parsed["verdict"])
            except (KeyError, ValueError):
                verdict = Verdict.UNCERTAIN

            assert paper.id is not None
            decision = ScreeningDecision(
                paper_id=paper.id,
                criteria_version=criteria.version,
                llm_verdict=verdict,
                llm_reason=str(parsed.get("reason", "")),
                llm_raw_response=str(parsed),
                model_name=llm_client.model,
                model_params={"temperature": llm_client.temperature},
                decided_at=utils.utc_now(),
            )

            conn.execute(
                """
                INSERT INTO screening_decisions (
                    paper_id, criteria_version, llm_verdict, llm_reason,
                    llm_raw_response, model_name, model_params, decided_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(paper_id, criteria_version) DO UPDATE SET
                    llm_verdict=excluded.llm_verdict,
                    llm_reason=excluded.llm_reason,
                    llm_raw_response=excluded.llm_raw_response,
                    model_name=excluded.model_name,
                    model_params=excluded.model_params,
                    decided_at=excluded.decided_at
                """,
                (
                    decision.paper_id,
                    decision.criteria_version,
                    decision.llm_verdict.value,
                    decision.llm_reason,
                    decision.llm_raw_response,
                    decision.model_name,
                    json.dumps(decision.model_params),
                    decision.decided_at,
                ),
            )
            conn.commit()
            screened += 1
            logger.info(
                "paper_screened",
                paper_id=paper.id,
                verdict=decision.llm_verdict.value,
                criteria_version=criteria.version,
            )

    if should_close:
        await llm_client.close()

    return {"screened": screened, "skipped": skipped}
