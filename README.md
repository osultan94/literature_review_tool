# Literature Review Automation Tool

A local, human-in-the-loop pipeline for systematic literature reviews. It takes a seed CSV of paper references, resolves them through academic APIs (Semantic Scholar, OpenAlex, arXiv, CrossRef), screens abstracts with a local LLM (Qwen3:8b via Ollama), scores each paper with configurable, auditable weights, and exports a ranked shortlist plus a full audit trail.

Built for thesis-grade reproducibility: every API call, LLM prompt, criteria version, and weight configuration is logged and versioned.

---

## Features

- **Seed ingestion** — extracts clean paper titles from messy CSV rows using a local LLM.
- **Paper resolution** — matches titles to canonical records across Semantic Scholar, OpenAlex, arXiv, and CrossRef.
- **Metadata harvesting & reconciliation** — merges multi-source metadata with explicit source-priority rules.
- **Deduplication** — DOI-first matching with fuzzy title/author fallback.
- **Snowballing** — backward/forward citation expansion with saturation tracking.
- **LLM abstract screening** — inclusion/exclusion/uncertain verdicts against versioned criteria.
- **Weight computation** — transparent scoring from citations, recency, venue tier, and LLM verdict.
- **Interactive UI** — dashboard, paper grid, criteria/weight editors, seed management, and CSV export.
- **Audit trail** — SQLite database plus `comprehensive.csv` records every decision and its lineage.

---

## Quick Start

### Prerequisites

- Python 3.10+
- [Ollama](https://ollama.com/) running locally with `qwen3:8b` pulled
- API keys (free tiers are usually sufficient):
  - [Semantic Scholar](https://www.semanticscholar.org/product/api)
  - [OpenAlex](https://docs.openalex.org/)
  - [CrossRef](https://www.crossref.org/documentation/) (polite pool recommended)
  - arXiv has no key requirement

### Installation

```bash
git clone <repo-url>
cd literature_review_tool
python -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### Run the pipeline

```bash
# 1. Place your seed references in data/seeds.csv
# 2. Start the UI
python -m lit_review.ui

# Or run individual stages from the CLI
python -m lit_review.cli ingest --input data/seeds.csv
python -m lit_review.cli resolve
python -m lit_review.cli harvest
python -m lit_review.cli screen
python -m lit_review.cli score
python -m lit_review.cli export
```

---

## Project Structure

```
literature_review_tool/
├── README.md                         # This file
├── lit_review_system_design.md       # Detailed system design
├── requirements.txt                  # Python dependencies
├── lit_review/                       # Main package
│   ├── __init__.py
│   ├── cli.py                        # CLI entry points for each pipeline stage
│   ├── config.py                     # API keys, source priority rules, defaults
│   ├── db.py                         # SQLite schema & connection helpers
│   ├── models.py                     # Pydantic/dataclass models
│   ├── pipeline/
│   │   ├── ingest.py                 # Stage 0: seed ingestion & title extraction
│   │   ├── resolve.py                # Stage 1: paper resolution
│   │   ├── harvest.py                # Stage 2: metadata harvesting & reconciliation
│   │   ├── dedupe.py                 # Stage 3: deduplication
│   │   ├── snowball.py               # Stage 4: backward/forward snowballing
│   │   ├── screen.py                 # Stage 5: LLM abstract screening
│   │   ├── score.py                  # Stage 6: weight computation
│   │   ├── saturation.py             # Stage 7: saturation check
│   │   └── export.py                 # Stage 8: CSV artifact generation
│   ├── llm/
│   │   ├── client.py                 # Ollama client wrapper
│   │   ├── prompts.py                # Task A & Task B prompts
│   │   └── schemas.py                # Structured output schemas
│   ├── sources/
│   │   ├── semantic_scholar.py
│   │   ├── openalex.py
│   │   ├── arxiv.py
│   │   └── crossref.py
│   ├── ui/                           # Web UI (FastAPI + simple frontend)
│   │   ├── app.py
│   │   ├── static/
│   │   └── templates/
│   └── utils.py
├── data/                             # User data (gitignored)
│   ├── seeds.csv
│   ├── lit_review.db
│   ├── comprehensive.csv
│   └── final_ranked.csv
└── tests/
```

---

## Pipeline Stages

| Stage | Description | Trigger |
|-------|-------------|---------|
| 0 | Seed ingestion & title extraction | CLI / UI |
| 1 | Paper resolution (title → canonical ID) | CLI / UI |
| 2 | Metadata harvesting & reconciliation | CLI / UI |
| 3 | Deduplication | CLI / UI |
| 4 | Snowballing (citation expansion) | CLI / UI |
| 5 | LLM abstract screening | CLI / UI |
| 6 | Weight computation | CLI / UI |
| 7 | Saturation check | Automatic after each round |
| 8 | Artifact generation (CSV export) | CLI / UI |

---

## Configuration

Edit `lit_review/config.py` or use environment variables:

```bash
export SEMANTIC_SCHOLAR_API_KEY="..."
export OPENALEX_EMAIL="..."
export CROSSREF_EMAIL="..."
export OLLAMA_HOST="http://localhost:11434"
export OLLAMA_MODEL="qwen3:8b"
```

---

## Development

```bash
# Run tests
pytest

# Format code
ruff format .
ruff check --fix .

# Type check
mypy lit_review
```

---

## Design Notes

See [`lit_review_system_design.md`](./lit_review_system_design.md) for the full architecture, data model, LLM task design, weighting strategy, reproducibility requirements, and suggested build order.

---

## License

MIT
