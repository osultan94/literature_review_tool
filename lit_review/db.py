"""SQLite database schema and connection helpers."""

from __future__ import annotations

import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path

from lit_review import config, utils
from lit_review.models import CriteriaVersion, Paper, Seed, SeedStatus, WeightConfig


def _adapt_datetime(value: datetime) -> str:
    return value.isoformat()


def _convert_datetime(value: bytes) -> datetime:
    return datetime.fromisoformat(value.decode())


def _adapt_date(value: date) -> str:
    return value.isoformat()


def _convert_date(value: bytes) -> date:
    return date.fromisoformat(value.decode())


sqlite3.register_adapter(datetime, _adapt_datetime)
sqlite3.register_converter("TIMESTAMP", _convert_datetime)
sqlite3.register_adapter(date, _adapt_date)
sqlite3.register_converter("DATE", _convert_date)

SCHEMA = """
CREATE TABLE IF NOT EXISTS seeds (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    raw_text TEXT NOT NULL,
    extracted_title TEXT,
    extraction_confidence REAL,
    status TEXT NOT NULL DEFAULT 'pending',
    resolved_paper_id INTEGER REFERENCES papers(id),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS papers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    canonical_title TEXT NOT NULL,
    doi TEXT,
    primary_source_name TEXT,
    primary_source_id TEXT,
    abstract TEXT,
    venue TEXT,
    venue_tier INTEGER,
    pub_date DATE,
    pub_year INTEGER,
    citation_count INTEGER,
    citation_count_fetched_at TIMESTAMP,
    authors TEXT,  -- JSON list
    origin TEXT NOT NULL DEFAULT 'seed',
    origin_paper_id INTEGER,
    discovery_round INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_papers_doi ON papers(doi);
CREATE INDEX IF NOT EXISTS idx_papers_origin ON papers(origin);

CREATE TABLE IF NOT EXISTS paper_sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    paper_id INTEGER NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
    source_name TEXT NOT NULL,
    source_paper_id TEXT NOT NULL,
    raw_json TEXT NOT NULL,  -- JSON blob
    fetched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(paper_id, source_name)
);

CREATE TABLE IF NOT EXISTS criteria (
    version INTEGER PRIMARY KEY AUTOINCREMENT,
    criteria_text TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    active BOOLEAN NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS screening_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    paper_id INTEGER NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
    criteria_version INTEGER NOT NULL REFERENCES criteria(version),
    llm_verdict TEXT NOT NULL,
    llm_reason TEXT,
    llm_raw_response TEXT NOT NULL,
    model_name TEXT NOT NULL,
    model_params TEXT NOT NULL,  -- JSON
    human_override TEXT,
    human_note TEXT,
    decided_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(paper_id, criteria_version)
);

CREATE INDEX IF NOT EXISTS idx_decisions_paper ON screening_decisions(paper_id);

CREATE TABLE IF NOT EXISTS weight_config (
    version INTEGER PRIMARY KEY AUTOINCREMENT,
    component_weights TEXT NOT NULL,  -- JSON
    normalization_method TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    active BOOLEAN NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS harvest_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    round_number INTEGER NOT NULL,
    stage TEXT NOT NULL,
    source_paper_id INTEGER,
    query TEXT,
    results_count INTEGER NOT NULL,
    new_unique_count INTEGER NOT NULL,
    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_harvest_round ON harvest_log(round_number);

CREATE TABLE IF NOT EXISTS paper_scores (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    paper_id INTEGER NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
    weight_version INTEGER NOT NULL REFERENCES weight_config(version),
    llm_verdict_score REAL,
    citation_score REAL,
    recency_score REAL,
    venue_tier_score REAL,
    final_score REAL NOT NULL,
    component_breakdown TEXT NOT NULL,  -- JSON
    computed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(paper_id, weight_version)
);

CREATE INDEX IF NOT EXISTS idx_scores_paper ON paper_scores(paper_id);
"""


def get_connection(db_path: Path | str | None = None) -> sqlite3.Connection:
    """Open a SQLite connection with sensible defaults."""
    path = str(db_path or config.DEFAULT_DB_PATH)
    conn = sqlite3.connect(path, detect_types=sqlite3.PARSE_DECLTYPES)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(db_path: Path | str | None = None) -> None:
    """Create all tables and seed default criteria/weight config."""
    import json

    with get_connection(db_path) as conn:
        conn.executescript(SCHEMA)

        # Seed default criteria if none exists
        cursor = conn.execute("SELECT COUNT(*) FROM criteria")
        if cursor.fetchone()[0] == 0:
            conn.execute(
                "INSERT INTO criteria (criteria_text, active) VALUES (?, 1)",
                (config.DEFAULT_CRITERIA_TEXT,),
            )

        # Seed default weight config if none exists
        cursor = conn.execute("SELECT COUNT(*) FROM weight_config")
        if cursor.fetchone()[0] == 0:
            conn.execute(
                "INSERT INTO weight_config (component_weights, normalization_method, active) "
                "VALUES (?, ?, 1)",
                (json.dumps(config.DEFAULT_WEIGHT_CONFIG), config.DEFAULT_NORMALIZATION_METHOD),
            )

        conn.commit()


@contextmanager
def transaction(
    db_path: Path | str | None = None,
) -> Generator[sqlite3.Connection, None, None]:
    """Context manager that yields a connection and commits on success."""
    conn = get_connection(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _parse_authors(value: str | None) -> list[str]:
    authors = utils.from_json_blob(value)
    if isinstance(authors, list):
        return [str(a) for a in authors]
    return []


def row_to_seed(row: sqlite3.Row) -> Seed:
    return Seed(
        id=row["id"],
        raw_text=row["raw_text"],
        extracted_title=row["extracted_title"],
        extraction_confidence=row["extraction_confidence"],
        status=SeedStatus(row["status"]),
        resolved_paper_id=row["resolved_paper_id"],
        created_at=row["created_at"],
    )


def row_to_paper(row: sqlite3.Row) -> Paper:
    return Paper(
        id=row["id"],
        canonical_title=row["canonical_title"],
        doi=row["doi"],
        primary_source_name=row["primary_source_name"],
        primary_source_id=row["primary_source_id"],
        abstract=row["abstract"],
        venue=row["venue"],
        venue_tier=row["venue_tier"],
        pub_date=row["pub_date"],
        pub_year=row["pub_year"],
        citation_count=row["citation_count"],
        citation_count_fetched_at=row["citation_count_fetched_at"],
        authors=_parse_authors(row["authors"]),
        origin=row["origin"],
        origin_paper_id=row["origin_paper_id"],
        discovery_round=row["discovery_round"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def get_active_criteria(conn: sqlite3.Connection) -> CriteriaVersion | None:
    row = conn.execute(
        "SELECT * FROM criteria WHERE active = 1 ORDER BY version DESC LIMIT 1"
    ).fetchone()
    if not row:
        return None
    return CriteriaVersion(
        version=row["version"],
        criteria_text=row["criteria_text"],
        created_at=row["created_at"],
        active=bool(row["active"]),
    )


def get_active_weight_config(conn: sqlite3.Connection) -> WeightConfig | None:
    row = conn.execute(
        "SELECT * FROM weight_config WHERE active = 1 ORDER BY version DESC LIMIT 1"
    ).fetchone()
    if not row:
        return None
    return WeightConfig(
        version=row["version"],
        component_weights=utils.from_json_blob(row["component_weights"]),
        normalization_method=row["normalization_method"],
        created_at=row["created_at"],
        active=bool(row["active"]),
    )
