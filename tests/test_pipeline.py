"""End-to-end pipeline tests with mocked external services."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx

from lit_review.db import get_active_criteria, get_connection
from lit_review.models import SeedStatus
from lit_review.pipeline.dedupe import deduplicate
from lit_review.pipeline.export import export_comprehensive, export_final_ranked
from lit_review.pipeline.harvest import harvest_metadata
from lit_review.pipeline.ingest import ingest_seeds
from lit_review.pipeline.resolve import resolve_seeds
from lit_review.pipeline.saturation import check_saturation
from lit_review.pipeline.score import compute_scores
from lit_review.pipeline.screen import screen_papers
from lit_review.pipeline.snowball import run_snowball_round


async def test_ingest_extracts_titles(tmp_db: Path, seed_csv: Path, fake_extractor) -> None:
    result = await ingest_seeds(seed_csv, db_path=tmp_db, llm_client=fake_extractor)
    assert result["inserted"] == 2

    with get_connection(tmp_db) as conn:
        seeds = conn.execute("SELECT * FROM seeds").fetchall()
        assert len(seeds) == 2
        assert seeds[0]["status"] == SeedStatus.EXTRACTED.value
        assert "Retrieval-Augmented Generation" in seeds[0]["extracted_title"]


async def test_resolve_creates_papers(
    tmp_db: Path, seed_csv: Path, fake_extractor, s2_search_response
) -> None:
    await ingest_seeds(seed_csv, db_path=tmp_db, llm_client=fake_extractor)

    def handler(request: httpx.Request) -> httpx.Response:
        if "semanticscholar.org" in request.url.host:
            return httpx.Response(200, json=s2_search_response)
        if "openalex.org" in request.url.host:
            return httpx.Response(200, json={"results": []})
        if "crossref.org" in request.url.host:
            return httpx.Response(200, json={"message": {"items": []}})
        if "arxiv.org" in request.url.host:
            return httpx.Response(200, text="<feed xmlns='http://www.w3.org/2005/Atom'></feed>")
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        result = await resolve_seeds(db_path=tmp_db, client=client)

    assert result["resolved"] == 1
    with get_connection(tmp_db) as conn:
        papers = conn.execute("SELECT * FROM papers").fetchall()
        assert len(papers) == 1
        assert papers[0]["canonical_title"] == s2_search_response["data"][0]["title"]


async def test_screen_and_score(
    tmp_db: Path, seed_csv: Path, fake_extractor, fake_screener, s2_search_response
) -> None:
    await ingest_seeds(seed_csv, db_path=tmp_db, llm_client=fake_extractor)

    def handler(request: httpx.Request) -> httpx.Response:
        if "semanticscholar.org" in request.url.host:
            return httpx.Response(200, json=s2_search_response)
        return httpx.Response(200, json={"results": []})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        await resolve_seeds(db_path=tmp_db, client=client)
        await harvest_metadata(db_path=tmp_db, client=client)

    await screen_papers(db_path=tmp_db, llm_client=fake_screener)

    with get_connection(tmp_db) as conn:
        decisions = conn.execute("SELECT * FROM screening_decisions").fetchall()
        assert len(decisions) == 1
        assert decisions[0]["llm_verdict"] == "include"

    compute_scores(db_path=tmp_db)

    with get_connection(tmp_db) as conn:
        scores = conn.execute("SELECT * FROM paper_scores").fetchall()
        assert len(scores) == 1
        assert scores[0]["final_score"] > 0


def test_deduplicate_merges_by_doi(tmp_db: Path) -> None:
    with get_connection(tmp_db) as conn:
        conn.execute(
            "INSERT INTO papers (canonical_title, doi, authors, origin) VALUES (?, ?, ?, ?)",
            ("Paper A", "10.1234/a", json.dumps(["Alice"]), "seed"),
        )
        conn.execute(
            "INSERT INTO papers (canonical_title, doi, authors, origin) VALUES (?, ?, ?, ?)",
            ("Paper A duplicate", "10.1234/a", json.dumps(["Alice"]), "seed"),
        )
        conn.commit()

    result = deduplicate(db_path=tmp_db)
    assert result["merged"] == 1

    with get_connection(tmp_db) as conn:
        count = conn.execute("SELECT COUNT(*) FROM papers").fetchone()[0]
        assert count == 1


def test_export_comprehensive(
    tmp_db: Path, seed_csv: Path, fake_extractor, s2_search_response
) -> None:
    async def _run():
        await ingest_seeds(seed_csv, db_path=tmp_db, llm_client=fake_extractor)

        def handler(request: httpx.Request) -> httpx.Response:
            if "semanticscholar.org" in request.url.host:
                return httpx.Response(200, json=s2_search_response)
            return httpx.Response(200, json={"results": []})

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            await resolve_seeds(db_path=tmp_db, client=client)

    asyncio.run(_run())

    output = tmp_db.parent / "comprehensive.csv"
    path = export_comprehensive(output_path=output, db_path=tmp_db)
    assert path.exists()
    text = path.read_text()
    assert "paper_id" in text
    assert "Retrieval-Augmented Generation" in text


def test_active_criteria_seeded(tmp_db: Path) -> None:
    with get_connection(tmp_db) as conn:
        criteria = get_active_criteria(conn)
        assert criteria is not None
        assert criteria.version == 1
        assert "retrieval-augmented" in criteria.criteria_text.lower()


async def test_snowball_adds_new_papers(tmp_db: Path, s2_search_response) -> None:
    # Seed one resolved paper to snowball from
    with get_connection(tmp_db) as conn:
        conn.execute(
            """
            INSERT INTO papers (
                canonical_title, primary_source_name, primary_source_id,
                origin, discovery_round
            ) VALUES (?, ?, ?, ?, ?)
            """,
            ("Seed Paper", "semantic_scholar", "s2_123", "seed", 0),
        )
        conn.execute(
            """
            INSERT INTO screening_decisions (
                paper_id, criteria_version, llm_verdict, llm_raw_response, model_name, model_params
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (1, 1, "include", "{}", "fake", "{}"),
        )
        conn.commit()

    ref_item = {
        "paperId": "s2_999",
        "title": "A Cited Paper About RAG",
        "abstract": "This paper extends RAG methods.",
        "venue": "ACL",
        "year": 2021,
        "citationCount": 42,
        "externalIds": {"DOI": "10.1234/cited"},
        "authors": [{"name": "B. Smith"}],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if "references" in request.url.path:
            return httpx.Response(200, json={"data": [{"citedPaper": ref_item}]})
        if "citations" in request.url.path:
            return httpx.Response(200, json={"data": []})
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        result = await run_snowball_round(db_path=tmp_db, client=client)

    assert result["new_papers"] == 1
    assert result["round_number"] == 1

    with get_connection(tmp_db) as conn:
        papers = conn.execute("SELECT * FROM papers WHERE origin != 'seed'").fetchall()
        assert len(papers) == 1
        assert papers[0]["canonical_title"] == ref_item["title"]

        log = conn.execute("SELECT * FROM harvest_log").fetchall()
        assert len(log) == 1
        assert log[0]["new_unique_count"] == 1


def test_saturation_detected(tmp_db: Path) -> None:
    with get_connection(tmp_db) as conn:
        for i in range(1, 3):
            conn.execute(
                """
                INSERT INTO harvest_log (round_number, stage, results_count, new_unique_count)
                VALUES (?, ?, ?, ?)
                """,
                (i, "snowball_both", 100, 1),
            )
        conn.commit()

    result = check_saturation(db_path=tmp_db, threshold=0.05, consecutive_rounds=2)
    assert result["saturated"] is True
    assert len(result["rounds"]) == 2


def test_export_final_ranked(tmp_db: Path) -> None:
    with get_connection(tmp_db) as conn:
        conn.execute(
            """
            INSERT INTO papers (canonical_title, doi, authors, origin, discovery_round)
            VALUES (?, ?, ?, ?, ?)
            """,
            ("Included Paper", "10.1234/inc", json.dumps(["Alice"]), "seed", 0),
        )
        conn.execute(
            """
            INSERT INTO screening_decisions (
                paper_id, criteria_version, llm_verdict, llm_raw_response, model_name, model_params
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (1, 1, "include", "{}", "fake", "{}"),
        )
        conn.execute(
            """
            INSERT INTO paper_scores (paper_id, weight_version, final_score, component_breakdown)
            VALUES (?, ?, ?, ?)
            """,
            (1, 1, 0.85, "{}"),
        )
        conn.commit()

    output = tmp_db.parent / "final_ranked.csv"
    path = export_final_ranked(output_path=output, db_path=tmp_db)
    assert path.exists()
    text = path.read_text()
    assert "Included Paper" in text
    assert "rank" in text
