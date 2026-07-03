"""Pydantic models for entities and API responses."""

from __future__ import annotations

from datetime import date, datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class SeedStatus(str, Enum):
    PENDING = "pending"
    EXTRACTED = "extracted"
    RESOLVED = "resolved"
    AMBIGUOUS = "ambiguous"
    FAILED = "failed"


class Verdict(str, Enum):
    INCLUDE = "include"
    EXCLUDE = "exclude"
    UNCERTAIN = "uncertain"


class PaperOrigin(str, Enum):
    SEED = "seed"
    SNOWBALL_BACKWARD = "snowball-backward"
    SNOWBALL_FORWARD = "snowball-forward"
    MANUAL = "manual"


class HarvestStage(str, Enum):
    SEED_RESOLUTION = "seed_resolution"
    SNOWBALL_BACKWARD = "snowball_backward"
    SNOWBALL_FORWARD = "snowball_forward"
    MANUAL = "manual"


class Seed(BaseModel):
    id: int | None = None
    raw_text: str
    extracted_title: str | None = None
    extraction_confidence: float | None = None
    status: SeedStatus = SeedStatus.PENDING
    resolved_paper_id: int | None = None
    created_at: datetime | None = None


class Paper(BaseModel):
    id: int | None = None
    canonical_title: str
    doi: str | None = None
    primary_source_name: str | None = None
    primary_source_id: str | None = None
    abstract: str | None = None
    venue: str | None = None
    venue_tier: int | None = None
    pub_date: date | None = None
    pub_year: int | None = None
    citation_count: int | None = None
    citation_count_fetched_at: datetime | None = None
    authors: list[str] = Field(default_factory=list)
    origin: PaperOrigin = PaperOrigin.SEED
    origin_paper_id: int | None = None
    discovery_round: int = 0
    created_at: datetime | None = None
    updated_at: datetime | None = None


class PaperSource(BaseModel):
    id: int | None = None
    paper_id: int
    source_name: str
    source_paper_id: str
    raw_json: dict[str, Any]
    fetched_at: datetime


class CriteriaVersion(BaseModel):
    version: int
    criteria_text: str
    created_at: datetime
    active: bool


class ScreeningDecision(BaseModel):
    id: int | None = None
    paper_id: int
    criteria_version: int
    llm_verdict: Verdict
    llm_reason: str
    llm_raw_response: str
    model_name: str
    model_params: dict[str, Any]
    human_override: Verdict | None = None
    human_note: str | None = None
    decided_at: datetime | None = None


class WeightConfig(BaseModel):
    version: int
    component_weights: dict[str, float]
    normalization_method: str
    created_at: datetime
    active: bool


class HarvestLogEntry(BaseModel):
    id: int | None = None
    round_number: int
    stage: HarvestStage
    source_paper_id: int | None = None
    query: str | None = None
    results_count: int
    new_unique_count: int
    timestamp: datetime


class ResolvedCandidate(BaseModel):
    source_name: str
    source_paper_id: str
    title: str
    doi: str | None = None
    authors: list[str] = Field(default_factory=list)
    pub_year: int | None = None
    abstract: str | None = None
    venue: str | None = None
    citation_count: int | None = None
    similarity_ratio: float
