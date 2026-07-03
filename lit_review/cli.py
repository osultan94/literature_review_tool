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
from lit_review.pipeline.export import export_comprehensive
from lit_review.pipeline.harvest import harvest_metadata
from lit_review.pipeline.ingest import ingest_seeds
from lit_review.pipeline.resolve import resolve_seeds
from lit_review.pipeline.score import compute_scores
from lit_review.pipeline.screen import screen_papers

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


@cli.command("run-all")
@click.argument("input_csv", type=click.Path(exists=True, path_type=Path))
@click.option("--output", type=click.Path(path_type=Path))
@click.pass_context
@run_async
async def run_all(ctx: click.Context, input_csv: Path, output: Path | None) -> None:
    """Run full pipeline: ingest, resolve, harvest, dedupe, screen, score, export."""
    init_db(ctx.obj["db_path"])
    click.echo("Ingesting seeds...")
    await ingest_seeds(input_csv, db_path=ctx.obj["db_path"])
    click.echo("Resolving titles...")
    async with httpx.AsyncClient(timeout=config.REQUEST_TIMEOUT) as client:
        await resolve_seeds(db_path=ctx.obj["db_path"], client=client)
        click.echo("Harvesting metadata...")
        await harvest_metadata(db_path=ctx.obj["db_path"], client=client)
    click.echo("Deduplicating...")
    deduplicate(db_path=ctx.obj["db_path"])
    click.echo("Screening abstracts...")
    await screen_papers(db_path=ctx.obj["db_path"])
    click.echo("Computing scores...")
    compute_scores(db_path=ctx.obj["db_path"])
    click.echo("Exporting...")
    path = export_comprehensive(output_path=output, db_path=ctx.obj["db_path"])
    click.echo(f"Done. Exported {path}")


if __name__ == "__main__":
    cli()
