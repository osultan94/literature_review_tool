"""FastAPI web UI for the literature review pipeline."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
from fastapi import BackgroundTasks, FastAPI, Form, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from lit_review import config
from lit_review.db import (
    get_active_criteria,
    get_active_weight_config,
    get_connection,
    init_db,
    row_to_seed,
)
from lit_review.models import SeedStatus, Verdict
from lit_review.pipeline.dedupe import deduplicate
from lit_review.pipeline.export import export_comprehensive, export_final_ranked
from lit_review.pipeline.harvest import harvest_metadata
from lit_review.pipeline.ingest import ingest_seeds, reextract_seed
from lit_review.pipeline.resolve import resolve_seeds
from lit_review.pipeline.saturation import check_saturation
from lit_review.pipeline.score import compute_scores
from lit_review.pipeline.screen import screen_papers
from lit_review.pipeline.snowball import run_snowball_round

app = FastAPI(title="Literature Review Automation")
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")

init_db()

print(
    f"Sources: Semantic Scholar {'on' if config.USE_SEMANTIC_SCHOLAR else 'off'}, "
    f"OpenAlex on (email={'yes' if config.OPENALEX_EMAIL else 'no'}), "
    f"CrossRef on (email={'yes' if config.CROSSREF_EMAIL else 'no'})"
)


def _get_db_path() -> Path:
    return Path(config.DEFAULT_DB_PATH)


@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "index.html")


@app.get("/api/dashboard")
async def dashboard() -> dict[str, Any]:
    db_path = _get_db_path()
    with get_connection(db_path) as conn:
        criteria = get_active_criteria(conn)
        criteria_version = criteria.version if criteria else 0
        seed_total = conn.execute("SELECT COUNT(*) FROM seeds").fetchone()[0]
        seed_extracted = conn.execute(
            "SELECT COUNT(*) FROM seeds WHERE status = ?", (SeedStatus.EXTRACTED.value,)
        ).fetchone()[0]
        seed_resolved = conn.execute(
            "SELECT COUNT(*) FROM seeds WHERE status = ?", (SeedStatus.RESOLVED.value,)
        ).fetchone()[0]
        paper_total = conn.execute("SELECT COUNT(*) FROM papers").fetchone()[0]
        include = conn.execute(
            "SELECT COUNT(*) FROM screening_decisions "
            "WHERE criteria_version = ? AND llm_verdict = ?",
            (criteria_version, Verdict.INCLUDE.value),
        ).fetchone()[0]
        exclude = conn.execute(
            "SELECT COUNT(*) FROM screening_decisions "
            "WHERE criteria_version = ? AND llm_verdict = ?",
            (criteria_version, Verdict.EXCLUDE.value),
        ).fetchone()[0]
        uncertain = conn.execute(
            "SELECT COUNT(*) FROM screening_decisions "
            "WHERE criteria_version = ? AND llm_verdict = ?",
            (criteria_version, Verdict.UNCERTAIN.value),
        ).fetchone()[0]
        screened = conn.execute(
            "SELECT COUNT(DISTINCT paper_id) FROM screening_decisions WHERE criteria_version = ?",
            (criteria_version,),
        ).fetchone()[0]
        unscreened = paper_total - screened
        weight = get_active_weight_config(conn)

    saturation = check_saturation(db_path=db_path)

    return {
        "seeds": {"total": seed_total, "extracted": seed_extracted, "resolved": seed_resolved},
        "papers": {
            "total": paper_total,
            "include": include,
            "exclude": exclude,
            "uncertain": uncertain,
            "unscreened": unscreened,
        },
        "criteria_version": criteria.version if criteria else None,
        "weight_version": weight.version if weight else None,
        "saturation": saturation,
    }


@app.get("/api/papers")
async def list_papers(
    q: str = "",
    verdict: str | None = None,
    sort: str = "score",
    order: str = "desc",
    limit: int = 500,
) -> list[dict[str, Any]]:
    db_path = _get_db_path()
    sort_column = {
        "score": "COALESCE(ps.final_score, 0)",
        "title": "p.canonical_title",
        "year": "COALESCE(p.pub_year, 0)",
        "citations": "COALESCE(p.citation_count, 0)",
    }.get(sort, "COALESCE(ps.final_score, 0)")
    order_sql = "DESC" if order.lower() == "desc" else "ASC"

    with get_connection(db_path) as conn:
        criteria = get_active_criteria(conn)
        criteria_version = criteria.version if criteria else 0
        weight = get_active_weight_config(conn)
        weight_version = weight.version if weight else 0

        params: list[Any] = [criteria_version, weight_version]
        where_clauses = ["1=1"]
        if q:
            where_clauses.append("(p.canonical_title LIKE ? OR p.abstract LIKE ?)")
            params.extend([f"%{q}%", f"%{q}%"])
        if verdict == "unscreened":
            where_clauses.append("sd.id IS NULL")
        elif verdict:
            where_clauses.append("sd.llm_verdict = ?")
            params.append(verdict)

        sql = f"""
            SELECT
                p.id, p.canonical_title, p.doi, p.venue, p.venue_tier,
                p.pub_year, p.citation_count, p.authors, p.origin, p.discovery_round,
                sd.llm_verdict, sd.llm_reason, sd.human_override, sd.human_note,
                ps.final_score, ps.component_breakdown
            FROM papers p
            LEFT JOIN screening_decisions sd
                ON sd.paper_id = p.id AND sd.criteria_version = ?
            LEFT JOIN paper_scores ps
                ON ps.paper_id = p.id AND ps.weight_version = ?
            WHERE {' AND '.join(where_clauses)}
            ORDER BY {sort_column} {order_sql}, p.canonical_title ASC
            LIMIT ?
        """
        params.append(limit)

        rows = conn.execute(sql, params).fetchall()

    results = []
    for row in rows:
        d = dict(row)
        d["authors"] = _decode_json(d.get("authors"))
        d["component_breakdown"] = _decode_json(d.get("component_breakdown"))
        results.append(d)
    return results


@app.post("/api/papers/{paper_id}/override")
async def override_paper(
    paper_id: int, override: str = Form(...), note: str = Form("")
) -> dict[str, str]:
    db_path = _get_db_path()
    if override not in {v.value for v in Verdict}:
        return {"error": "invalid override"}
    with get_connection(db_path) as conn:
        criteria = get_active_criteria(conn)
        criteria_version = criteria.version if criteria else 0
        conn.execute(
            """
            INSERT INTO screening_decisions (
                paper_id, criteria_version, llm_verdict, llm_reason,
                llm_raw_response, model_name, model_params, human_override, human_note
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(paper_id, criteria_version) DO UPDATE SET
                human_override = excluded.human_override,
                human_note = excluded.human_note
            """,
            (
                paper_id,
                criteria_version,
                override,
                "human override",
                "{}",
                "manual",
                "{}",
                override,
                note or None,
            ),
        )
        conn.commit()
    return {"status": "ok"}


@app.get("/api/criteria")
async def list_criteria() -> dict[str, Any]:
    db_path = _get_db_path()
    with get_connection(db_path) as conn:
        active = get_active_criteria(conn)
        rows = conn.execute("SELECT * FROM criteria ORDER BY version DESC").fetchall()
    return {
        "active": active.version if active else None,
        "versions": [dict(row) for row in rows],
    }


@app.post("/api/criteria")
async def create_criteria(text: str = Form(...)) -> dict[str, int]:
    db_path = _get_db_path()
    with get_connection(db_path) as conn:
        conn.execute("UPDATE criteria SET active = 0")
        cursor = conn.execute(
            "INSERT INTO criteria (criteria_text, active) VALUES (?, 1)",
            (text,),
        )
        conn.commit()
    return {"version": cursor.lastrowid or 0}


@app.post("/api/criteria/{version}/activate")
async def activate_criteria(version: int) -> dict[str, str]:
    db_path = _get_db_path()
    with get_connection(db_path) as conn:
        conn.execute("UPDATE criteria SET active = 0")
        conn.execute("UPDATE criteria SET active = 1 WHERE version = ?", (version,))
        conn.commit()
    return {"status": "ok"}


@app.get("/api/weights")
async def list_weights() -> dict[str, Any]:
    db_path = _get_db_path()
    with get_connection(db_path) as conn:
        active = get_active_weight_config(conn)
        rows = conn.execute("SELECT * FROM weight_config ORDER BY version DESC").fetchall()
    versions = []
    for row in rows:
        d = dict(row)
        import json

        d["component_weights"] = json.loads(d["component_weights"])
        versions.append(d)
    return {
        "active": active.version if active else None,
        "versions": versions,
    }


@app.post("/api/weights")
async def create_weights(
    llm_verdict: float = Form(...),
    citations_per_year: float = Form(...),
    recency: float = Form(...),
    venue_tier: float = Form(...),
    normalization: str = Form("min_max"),
) -> dict[str, int]:
    import json

    db_path = _get_db_path()
    weights = {
        "llm_verdict": llm_verdict,
        "citations_per_year": citations_per_year,
        "recency": recency,
        "venue_tier": venue_tier,
    }
    with get_connection(db_path) as conn:
        conn.execute("UPDATE weight_config SET active = 0")
        cursor = conn.execute(
            "INSERT INTO weight_config (component_weights, normalization_method, active) "
            "VALUES (?, ?, 1)",
            (json.dumps(weights), normalization),
        )
        conn.commit()
    compute_scores(db_path=db_path)
    return {"version": cursor.lastrowid or 0}


@app.post("/api/weights/{version}/activate")
async def activate_weights(version: int) -> dict[str, str]:
    db_path = _get_db_path()
    with get_connection(db_path) as conn:
        conn.execute("UPDATE weight_config SET active = 0")
        conn.execute("UPDATE weight_config SET active = 1 WHERE version = ?", (version,))
        conn.commit()
    compute_scores(db_path=db_path)
    return {"status": "ok"}


@app.get("/api/seeds")
async def list_seeds() -> list[dict[str, Any]]:
    db_path = _get_db_path()
    with get_connection(db_path) as conn:
        rows = conn.execute("SELECT * FROM seeds ORDER BY id").fetchall()
    return [dict(row_to_seed(row).model_dump()) for row in rows]


@app.post("/api/seeds/upload")
async def upload_seeds(file: UploadFile) -> dict[str, int]:
    db_path = _get_db_path()
    content = await file.read()
    csv_path = db_path.parent / "uploaded_seeds.csv"
    csv_path.write_bytes(content)
    result = await ingest_seeds(csv_path, db_path=db_path)
    return {"inserted": result["inserted"]}


@app.post("/api/seeds/{seed_id}/reextract")
async def reextract(seed_id: int) -> dict[str, Any]:
    db_path = _get_db_path()
    with get_connection(db_path) as conn:
        row = conn.execute("SELECT raw_text FROM seeds WHERE id = ?", (seed_id,)).fetchone()
        if not row:
            return {"error": "seed not found"}
        raw_text = row["raw_text"]
    result = await reextract_seed(seed_id, raw_text, db_path=db_path)
    return result


@app.post("/api/run/{stage}")
async def run_stage(stage: str, background_tasks: BackgroundTasks) -> dict[str, str]:
    db_path = _get_db_path()
    valid_stages = {
        "resolve",
        "harvest",
        "dedupe",
        "screen",
        "score",
        "snowball",
        "export",
    }
    if stage not in valid_stages:
        return {"error": f"invalid stage: {stage}"}

    # Run synchronously for simplicity; the UI shows a spinner.
    if stage == "resolve":
        async with httpx.AsyncClient(timeout=config.REQUEST_TIMEOUT) as client:
            await resolve_seeds(db_path=db_path, client=client)
    elif stage == "harvest":
        async with httpx.AsyncClient(timeout=config.REQUEST_TIMEOUT) as client:
            await harvest_metadata(db_path=db_path, client=client)
    elif stage == "dedupe":
        deduplicate(db_path=db_path)
    elif stage == "screen":
        await screen_papers(db_path=db_path)
    elif stage == "score":
        compute_scores(db_path=db_path)
    elif stage == "snowball":
        async with httpx.AsyncClient(timeout=config.REQUEST_TIMEOUT) as client:
            await run_snowball_round(db_path=db_path, client=client)
        deduplicate(db_path=db_path)
        await run_stage("harvest", background_tasks)
        await run_stage("screen", background_tasks)
        await run_stage("score", background_tasks)
    elif stage == "export":
        export_comprehensive(db_path=db_path)
        export_final_ranked(db_path=db_path)

    return {"status": "ok", "stage": stage}


@app.get("/api/export/comprehensive")
async def download_comprehensive() -> FileResponse:
    db_path = _get_db_path()
    path = export_comprehensive(db_path=db_path)
    return FileResponse(path, filename="comprehensive.csv", media_type="text/csv")


@app.get("/api/export/ranked")
async def download_ranked() -> FileResponse:
    db_path = _get_db_path()
    path = export_final_ranked(db_path=db_path)
    return FileResponse(path, filename="final_ranked.csv", media_type="text/csv")


def _decode_json(value: str | None) -> Any:
    import json

    if value is None:
        return {}
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return {}


def main() -> None:
    import os

    import uvicorn

    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run(app, host="127.0.0.1", port=port)


if __name__ == "__main__":
    main()
