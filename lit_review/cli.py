"""Command-line interface for the literature review pipeline."""

from __future__ import annotations

import asyncio
import functools
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any, ParamSpec, TypeVar

import click
import httpx

from lit_review import config
from lit_review.db import init_db
from lit_review.pipeline.dedupe import deduplicate
from lit_review.pipeline.export import export_comprehensive, export_final_ranked
from lit_review.pipeline.harvest import harvest_metadata
from lit_review.pipeline.ingest import ingest_seeds
from lit_review.pipeline.resolve import resolve_seeds
from lit_review.pipeline.saturation import check_saturation
from lit_review.pipeline.score import compute_scores
from lit_review.pipeline.screen import screen_papers
from lit_review.pipeline.snowball import run_snowball_round

P = ParamSpec("P")
R = TypeVar("R")


def run_async(
    coro: Callable[P, Coroutine[Any, Any, R]],
) -> Callable[P, R]:
    """Run an async coroutine from a synchronous Click command."""

    @functools.wraps(coro)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        return asyncio.run(coro(*args, **kwargs))

    return wrapper


@click.group()
@click.option("--db-path", type=click.Path(), default=str(config.DEFAULT_DB_PATH))
@click.pass_context
def cli(ctx: click.Context, db_path: str) -> None:
    """Literature review automation CLI."""
    ctx.ensure_object(dict)
    ctx.obj["db_path"] = Path(db_path)


@cli.command("init-db")
@click.pass_context
def init_db_cmd(ctx: click.Context) -> None:
    """Initialize the SQLite database."""
    init_db(ctx.obj["db_path"])
    click.echo(f"Database initialized at {ctx.obj['db_path']}")


@cli.command()
@click.argument("input_csv", type=click.Path(exists=True, path_type=Path))
@click.pass_context
@run_async
async def ingest(ctx: click.Context, input_csv: Path) -> None:
    """Ingest a seed CSV and extract titles."""
    result = await ingest_seeds(input_csv, db_path=ctx.obj["db_path"])
    click.echo(f"Ingested {result['inserted']} seeds.")


@cli.command()
@click.pass_context
@run_async
async def resolve(ctx: click.Context) -> None:
    """Resolve extracted titles to canonical papers."""
    async with httpx.AsyncClient(timeout=config.REQUEST_TIMEOUT) as client:
        result = await resolve_seeds(db_path=ctx.obj["db_path"], client=client)
    summary = (
        f"Resolved: {result['resolved']}, "
        f"ambiguous: {result['ambiguous']}, failed: {result['failed']}"
    )
    click.echo(summary)


@cli.command()
@click.pass_context
@run_async
async def harvest(ctx: click.Context) -> None:
    """Harvest and reconcile metadata for resolved papers."""
    async with httpx.AsyncClient(timeout=config.REQUEST_TIMEOUT) as client:
        result = await harvest_metadata(db_path=ctx.obj["db_path"], client=client)
    click.echo(f"Harvested {result['harvested']} papers.")


@cli.command()
@click.option("--auto-merge/--no-auto-merge", default=False)
@click.pass_context
def dedupe(ctx: click.Context, auto_merge: bool) -> None:
    """Deduplicate papers by DOI and fuzzy title match."""
    result = deduplicate(db_path=ctx.obj["db_path"], auto_merge_fuzzy=auto_merge)
    click.echo(
        f"Merged {result['merged']} duplicates; flagged {len(result['flagged_pairs'])} pairs."
    )


@cli.command()
@click.pass_context
@run_async
async def screen(ctx: click.Context) -> None:
    """Screen abstracts against active criteria using the local LLM."""
    result = await screen_papers(db_path=ctx.obj["db_path"])
    click.echo(f"Screened {result['screened']} papers; skipped {result['skipped']}.")


@cli.command()
@click.pass_context
def score(ctx: click.Context) -> None:
    """Compute weights and final scores."""
    result = compute_scores(db_path=ctx.obj["db_path"])
    click.echo(f"Scored {result['scored']} papers.")


@cli.command()
@click.option("--output", type=click.Path(path_type=Path))
@click.pass_context
def export(ctx: click.Context, output: Path | None) -> None:
    """Export comprehensive.csv audit trail."""
    path = export_comprehensive(output_path=output, db_path=ctx.obj["db_path"])
    click.echo(f"Exported {path}")


@cli.command()
@click.option("--paper-ids", help="Comma-separated source paper IDs (default: included/uncertain)")
@click.option("--direction", type=click.Choice(["backward", "forward", "both"]), default="both")
@click.option("--limit", default=100, help="Max references/citations per source")
@click.pass_context
@run_async
async def snowball(
    ctx: click.Context, paper_ids: str | None, direction: str, limit: int
) -> None:
    """Run one snowballing round from selected papers."""
    ids = [int(x) for x in paper_ids.split(",")] if paper_ids else None
    async with httpx.AsyncClient(timeout=config.REQUEST_TIMEOUT) as client:
        result = await run_snowball_round(
            db_path=ctx.obj["db_path"],
            source_paper_ids=ids,
            direction=direction,
            limit=limit,
            client=client,
        )
    click.echo(
        f"Round {result['round_number']}: {result['new_papers']} new papers "
        f"from {result['source_count']} sources ({result['total_results']} results)."
    )


@cli.command("check-saturation")
@click.option("--threshold", default=0.05, help="Novelty ratio threshold")
@click.option("--rounds", default=2, help="Consecutive rounds below threshold")
@click.pass_context
def saturation(ctx: click.Context, threshold: float, rounds: int) -> None:
    """Check whether snowballing/harvesting has saturated."""
    result = check_saturation(
        db_path=ctx.obj["db_path"], threshold=threshold, consecutive_rounds=rounds
    )
    status = "SATURATED" if result["saturated"] else "not saturated"
    click.echo(f"Saturation: {status}")
    for r in result["rounds"]:
        click.echo(
            f"  Round {r['round_number']} ({r['stage']}): "
            f"{r['new_unique_count']}/{r['results_count']} new "
            f"({r['novelty_ratio']:.2%})"
        )


@cli.command("export-ranked")
@click.option("--output", type=click.Path(path_type=Path))
@click.pass_context
def export_ranked(ctx: click.Context, output: Path | None) -> None:
    """Export final_ranked.csv shortlist."""
    path = export_final_ranked(output_path=output, db_path=ctx.obj["db_path"])
    click.echo(f"Exported {path}")


@cli.command("run-all")
@click.argument("input_csv", type=click.Path(exists=True, path_type=Path))
@click.option("--snowball-rounds", default=0, help="Number of snowball rounds to run")
@click.option("--output-dir", type=click.Path(path_type=Path))
@click.pass_context
@run_async
async def run_all(
    ctx: click.Context, input_csv: Path, snowball_rounds: int, output_dir: Path | None
) -> None:
    """Run full pipeline: ingest, resolve, harvest, dedupe, screen, score, snowball, export."""
    db_path = ctx.obj["db_path"]
    init_db(db_path)
    click.echo("Ingesting seeds...")
    await ingest_seeds(input_csv, db_path=db_path)
    click.echo("Resolving titles...")
    async with httpx.AsyncClient(timeout=config.REQUEST_TIMEOUT) as client:
        await resolve_seeds(db_path=db_path, client=client)
        click.echo("Harvesting metadata...")
        await harvest_metadata(db_path=db_path, client=client)
    click.echo("Deduplicating...")
    deduplicate(db_path=db_path)
    click.echo("Screening abstracts...")
    await screen_papers(db_path=db_path)
    click.echo("Computing scores...")
    compute_scores(db_path=db_path)

    for i in range(snowball_rounds):
        click.echo(f"Snowball round {i + 1}...")
        async with httpx.AsyncClient(timeout=config.REQUEST_TIMEOUT) as client:
            result = await run_snowball_round(db_path=db_path, client=client)
            if result["new_papers"] == 0:
                click.echo("No new papers found; stopping snowballing.")
                break
            await harvest_metadata(db_path=db_path, client=client)
        deduplicate(db_path=db_path)
        await screen_papers(db_path=db_path)
        compute_scores(db_path=db_path)

    click.echo("Exporting...")
    if output_dir:
        output_dir.mkdir(parents=True, exist_ok=True)
        comp_path = export_comprehensive(
            output_path=output_dir / "comprehensive.csv", db_path=db_path
        )
        ranked_path = export_final_ranked(
            output_path=output_dir / "final_ranked.csv", db_path=db_path
        )
    else:
        comp_path = export_comprehensive(db_path=db_path)
        ranked_path = export_final_ranked(db_path=db_path)
    click.echo(f"Done. {comp_path}, {ranked_path}")


if __name__ == "__main__":
    cli()
