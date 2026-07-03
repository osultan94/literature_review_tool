"""Application configuration and source-priority rules."""

from __future__ import annotations

import os
from pathlib import Path

# Paths
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
DEFAULT_DB_PATH = DATA_DIR / "lit_review.db"

# Ensure data directory exists
DATA_DIR.mkdir(exist_ok=True)

# API keys / polite-pool emails
SEMANTIC_SCHOLAR_API_KEY = os.getenv("SEMANTIC_SCHOLAR_API_KEY", "")
OPENALEX_EMAIL = os.getenv("OPENALEX_EMAIL", "")
CROSSREF_EMAIL = os.getenv("CROSSREF_EMAIL", "")

# Source toggles
USE_SEMANTIC_SCHOLAR = os.getenv("USE_SEMANTIC_SCHOLAR", "true").lower() in ("1", "true", "yes")

# Ollama
OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen3:8b")
OLLAMA_TEMPERATURE = float(os.getenv("OLLAMA_TEMPERATURE", "0.0"))

# Concurrency / resilience
MAX_WORKERS = int(os.getenv("MAX_WORKERS", "4"))
REQUEST_TIMEOUT = float(os.getenv("REQUEST_TIMEOUT", "30.0"))
API_RATE_LIMIT_RPS = float(os.getenv("API_RATE_LIMIT_RPS", "10.0"))

# Resolution thresholds
TITLE_MATCH_MIN_RATIO = float(os.getenv("TITLE_MATCH_MIN_RATIO", "0.85"))
TITLE_MATCH_AMBIGUOUS_RATIO = float(os.getenv("TITLE_MATCH_AMBIGUOUS_RATIO", "0.95"))

# Deduplication thresholds
DEDUPE_TITLE_MIN_RATIO = float(os.getenv("DEDUPE_TITLE_MIN_RATIO", "0.90"))
DEDUPE_AUTHOR_MIN_RATIO = float(os.getenv("DEDUPE_AUTHOR_MIN_RATIO", "0.70"))

# LLM extraction
EXTRACTION_MAX_RETRIES = int(os.getenv("EXTRACTION_MAX_RETRIES", "3"))

# Venue tier mapping (name substring -> tier). Editable via UI later.
DEFAULT_VENUE_TIERS: dict[str, int] = {
    # Top tier
    "neurips": 3,
    "icml": 3,
    "iclr": 3,
    "acl": 3,
    "emnlp": 3,
    "naacl": 3,
    "sigir": 3,
    "www": 3,
    "kdd": 3,
    # Solid venues
    "coling": 2,
    "ecir": 2,
    "cikm": 2,
    "wsdm": 2,
    "aaai": 2,
    "ijcai": 2,
    # Preprint / unknown
    "arxiv": 1,
    "biorxiv": 1,
    "medrxiv": 1,
    "workshop": 1,
}

# Which source wins for which field when metadata conflicts.
# Lower index = higher priority.
SOURCE_PRIORITY: dict[str, list[str]] = {
    "doi": ["crossref", "semantic_scholar", "openalex", "arxiv"],
    "title": ["crossref", "semantic_scholar", "openalex", "arxiv"],
    "abstract": ["semantic_scholar", "openalex", "arxiv", "crossref"],
    "venue": ["crossref", "semantic_scholar", "openalex", "arxiv"],
    "pub_date": ["crossref", "semantic_scholar", "openalex", "arxiv"],
    "citation_count": ["semantic_scholar", "openalex"],
    "authors": ["crossref", "semantic_scholar", "openalex", "arxiv"],
}

DEFAULT_CRITERIA_TEXT = os.getenv(
    "DEFAULT_CRITERIA_TEXT",
    (
        "Include papers that:\n"
        "1. Address retrieval-augmented generation (RAG) or "
        "information retrieval combined with generation.\n"
        "2. Were published in 2020 or later.\n"
        "3. Present a method, model, or empirical analysis (not purely a survey).\n"
        "Exclude papers that:\n"
        "A. Are only about computer vision, speech, or other non-text domains.\n"
        "B. Are short workshop abstracts or posters without technical detail."
    ),
)

DEFAULT_WEIGHT_CONFIG: dict[str, float] = {
    "llm_verdict": 0.40,
    "citations_per_year": 0.25,
    "recency": 0.20,
    "venue_tier": 0.15,
}

DEFAULT_NORMALIZATION_METHOD = "min_max"
