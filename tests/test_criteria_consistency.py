"""Tests for criteria versioning and screening consistency."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from lit_review.db import get_active_criteria, get_connection
from lit_review.pipeline.export import export_comprehensive
from lit_review.pipeline.harvest import harvest_metadata
from lit_review.pipeline.resolve import resolve_seeds
from lit_review.pipeline.score import compute_scores
from lit_review.pipeline.screen import screen_papers


@pytest.fixture
def s2_search_response() -> dict[str, Any]:
    return {
        "data": [
            {
                "paperId": "s2_123",
                "title": "Retrieval-Augmented Generation for Knowledge-Intensive NLP Tasks",
                "abstract": "We combine parametric and non-parametric memory...",
                "venue": "NeurIPS",
                "year": 2020,
                "citationCount": 5000,
                "externalIds": {"DOI": "10.1234/rag"},
                "authors": [{"name": "P. Lewis"}],
            }
        ]
    }


@pytest.fixture
def fake_extractor() -> Any:
    class _Fake:
        model = "fake"
        temperature = 0.0

        async def generate(
            self,
            prompt: str,
            schema: dict[str, Any] | None = None,
            temperature: float | None = None,
        ) -> dict[str, Any]:
            return {
                "title": "Retrieval-Augmented Generation for Knowledge-Intensive NLP Tasks",
                "confidence": 0.95,
            }

        async def close(self) -> None:
            pass

    return _Fake()


@pytest.fixture
def fake_screener() -> Any:
    class _Fake:
        model = "fake"
        temperature = 0.0
        _responses: list[dict[str, str]] = [
            {"verdict": "include", "matched_criterion": "RAG", "reason": "RAG paper"},
            {"verdict": "exclude", "matched_criterion": "survey", "reason": "Not a survey"},
        ]

        async def generate(
            self,
            prompt: str,
            schema: dict[str, Any] | None = None,
            temperature: float | None = None,
        ) -> dict[str, str]:
            return self._responses.pop(0)

        async def close(self) -> None:
            pass

    return _Fake()


async def test_criteria_change_resets_screening_state(
    tmp_db: Path,
    s2_search_response: dict[str, Any],
    fake_extractor: Any,
    fake_screener: Any,
) -> None:
    from lit_review.pipeline.ingest import ingest_seeds

    # Seed one paper
    seed_csv = tmp_db.parent / "seeds.csv"
    seed_csv.write_text("Retrieval-Augmented Generation for Knowledge-Intensive NLP Tasks\n")
    await ingest_seeds(seed_csv, db_path=tmp_db, llm_client=fake_extractor)

    def handler(request: httpx.Request) -> httpx.Response:
        if "semanticscholar.org" in request.url.host:
            return httpx.Response(200, json=s2_search_response)
        return httpx.Response(200, json={"results": []})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        await resolve_seeds(db_path=tmp_db, client=client)
        await harvest_metadata(db_path=tmp_db, client=client)

    # Initial active criteria is v1
    with get_connection(tmp_db) as conn:
        criteria = get_active_criteria(conn)
        assert criteria is not None
        assert criteria.version == 1

    # Screen under v1 -> include
    await screen_papers(db_path=tmp_db, llm_client=fake_screener)
    with get_connection(tmp_db) as conn:
        row = conn.execute(
            "SELECT llm_verdict FROM screening_decisions WHERE criteria_version = 1"
        ).fetchone()
        assert row is not None
        assert row["llm_verdict"] == "include"

    # Create v2 criteria
    with get_connection(tmp_db) as conn:
        conn.execute("UPDATE criteria SET active = 0")
        conn.execute(
            "INSERT INTO criteria (criteria_text, active) VALUES (?, 1)",
            ("Exclude everything that is not a survey.",),
        )
        conn.commit()
        criteria = get_active_criteria(conn)
        assert criteria is not None
        assert criteria.version == 2

    # Paper should be unscreened under v2
    with get_connection(tmp_db) as conn:
        row = conn.execute(
            "SELECT id FROM screening_decisions WHERE paper_id = 1 AND criteria_version = 2"
        ).fetchone()
        assert row is None

    # Screen under v2 -> exclude
    await screen_papers(db_path=tmp_db, llm_client=fake_screener)
    with get_connection(tmp_db) as conn:
        row = conn.execute(
            "SELECT llm_verdict FROM screening_decisions WHERE criteria_version = 2"
        ).fetchone()
        assert row is not None
        assert row["llm_verdict"] == "exclude"

    # Score and export should reflect v2 exclude
    compute_scores(db_path=tmp_db)
    path = export_comprehensive(db_path=tmp_db)
    text = path.read_text()
    assert "exclude" in text
