# Literature Review Pipeline

A full stage-by-stage walkthrough of how the literature-review pipeline works, from the raw seed CSV to the final ranked output.

## High-level idea

The tool treats a literature review as a reproducible, stateful workflow. SQLite is the central state machine. Each stage reads the database, performs work (API calls, LLM calls, scoring, etc.), and writes results back. Criteria and scoring weights are **versioned**—changing them creates new rows rather than overwriting old ones, so prior decisions remain auditable.

---

## Stage 0: Seed ingestion & title extraction

**File:** `lit_review/pipeline/ingest.py`  
**Entry point:** `ingest_seeds(csv_path, db_path, llm_client)`

1. Reads a single-column CSV of messy, free-text references.
2. Inserts each row into the `seeds` table with status `pending`.
3. For each seed, sends the raw text to the local LLM (default Ollama `qwen3:8b`) with `title_extraction_prompt()` and a strict JSON schema (`EXTRACTION_SCHEMA`).
4. The LLM returns a clean `extracted_title` plus a `confidence` score.
5. Updates the seed row:
   - status = `extracted` on success
   - status = `failed` if the LLM call fails or returns bad JSON

There is also `reextract_seed()` to rerun extraction on one seed.

---

## Stage 1: Paper resolution

**File:** `lit_review/pipeline/resolve.py`  
**Entry point:** `resolve_seeds(db_path)`

1. Queries all seeds with status `extracted`.
2. For each extracted title, calls `sources.search_all()` in parallel.
   - OpenAlex, CrossRef, and arXiv are always searched.
   - Semantic Scholar is searched only if `USE_SEMANTIC_SCHOLAR=True`.
3. Each source returns `ResolvedCandidate` objects containing title, authors, DOI, year, venue, and a title-similarity ratio.
4. Candidates are merged and sorted by similarity.
5. The best candidate is classified using thresholds from `config.py`:
   - `similarity >= 0.95` → `resolved`
   - `0.85 <= similarity < 0.95` → `ambiguous`
   - `< 0.85` → `failed`
6. If a matching DOI already exists in the `papers` table, the seed is linked to it. Otherwise a new `Paper` row is created and the raw source response is stored in `paper_sources`.

---

## Stage 2: Metadata harvesting & reconciliation

**File:** `lit_review/pipeline/harvest.py`  
**Entry point:** `harvest_metadata(db_path)`

1. Iterates over every paper in the `papers` table.
2. Fetches richer metadata from multiple sources:
   - Semantic Scholar (if that was the primary resolved source)
   - CrossRef by DOI
   - OpenAlex by DOI
   - arXiv by title as a cross-check/fallback
3. Stores each raw API response in `paper_sources`.
4. Runs `_reconcile()`:
   - Normalizes each source’s payload into a common structure.
   - For each field (title, authors, year, venue, abstract, DOI, citation count, etc.), picks the value according to `config.SOURCE_PRIORITY`.
   - Updates the `papers` row with the reconciled, canonical metadata.

---

## Stage 3: Deduplication

**File:** `lit_review/pipeline/dedupe.py`  
**Entry point:** `deduplicate(db_path, auto_merge_fuzzy=False)`

1. **Exact DOI pass:** any two papers with the same DOI are automatically merged via `_merge_papers()`.
2. **Fuzzy pass:** compares remaining papers by:
   - Title similarity via Levenshtein ratio (`>= 0.92` by default)
   - Author overlap via Jaccard-like similarity (`>= 0.6` by default)
3. If `auto_merge_fuzzy=True` and the pair is very strong (`title similarity >= 0.98` + author overlap above threshold), it auto-merges.
4. Otherwise the pair is flagged as a candidate duplicate for human review in the UI.

Merging keeps the oldest paper as the canonical row and retires the duplicate.

---

## Stage 4: Snowballing (optional expansion)

**File:** `lit_review/pipeline/snowball.py`  
**Entry point:** `run_snowball_round(db_path, source_paper_ids=None)`

1. Selects source papers:
   - If `source_paper_ids` is provided, uses those.
   - Otherwise uses papers whose latest screening decision is `include` or `uncertain`.
2. For each source paper, `_resolve_snowball_source()` obtains a usable Semantic Scholar or OpenAlex ID. Papers that only have CrossRef/arXiv IDs are resolved through OpenAlex first.
3. Fetches:
   - **Backward references** (papers this paper cites)
   - **Forward citations** (papers that cite this paper)
4. Converts each returned item into a `Paper` object.
5. Skips papers already in the database by DOI or fuzzy title match.
6. Inserts new papers with `origin = snowball-backward` or `snowball-forward` and `discovery_round = current round`.
7. Logs the round to `harvest_log` (total results, new unique papers, source counts).

These new papers then flow back through harvest, dedupe, screen, and score on the next iteration.

---

## Stage 5: LLM abstract screening

**File:** `lit_review/pipeline/screen.py`  
**Entry point:** `screen_papers(db_path, llm_client)`

1. Loads the **active** criteria version from the `criteria` table.
2. Selects papers with non-empty abstracts that do not yet have a decision for that criteria version.
3. For each paper, calls the LLM with `screening_prompt(abstract, criteria_text)` constrained by `SCREENING_SCHEMA`.
4. The LLM returns a verdict and a short reason:
   - `include`
   - `exclude`
   - `uncertain`
5. Stores the result in `screening_decisions` keyed by `(paper_id, criteria_version_id)`.

Because criteria are versioned, changing the criteria text creates new decisions without deleting old ones.

---

## Stage 6: Scoring & weighting

**File:** `lit_review/pipeline/score.py`  
**Entry point:** `compute_scores(db_path)`

1. Loads the **active** weight config from `weight_config`.
2. For each paper, computes raw component scores:
   - **LLM verdict:** `include = 1.0`, `uncertain = 0.5`, `exclude = 0.0`
   - **Citations per year:** `log1p(citations / years_since_publication)`
   - **Recency:** linear 0–1 scale from the cutoff year (default 2020) to the current year
   - **Venue tier:** tier 1 → 0, tier 2 → 0.5, tier 3 → 1 (tier mapping is configurable)
3. Min-max normalizes each component across the current batch.
4. Combines components into a `final_score` using the configured weights.
5. Stores the full breakdown in `paper_scores`, keyed by `(paper_id, weight_version_id)`.

Default weights are 0.40 verdict / 0.25 citations-per-year / 0.20 recency / 0.15 venue tier.

---

## Stage 7: Saturation checking

**File:** `lit_review/pipeline/saturation.py`  
**Entry point:** `check_saturation(db_path)`

1. Reads the most recent `harvest_log` entries (snowball rounds).
2. Computes `new_unique_count / results_count` for each round.
3. Returns `saturated=True` when that ratio stays below a threshold (default 5%) for a configurable number of consecutive rounds (default 2).

This tells the user when snowballing is no longer yielding enough new papers to continue.

---

## Stage 8: Export

**File:** `lit_review/pipeline/export.py`  
**Entry points:** `export_comprehensive()` and `export_final_ranked()`

1. `export_comprehensive()` writes `data/comprehensive.csv` containing every paper, its reconciled metadata, active screening decision (with human override if set), and full score breakdown.
2. `export_final_ranked()` writes `data/final_ranked.csv` containing only papers where the effective verdict (`human_override` if present, otherwise LLM verdict) is `include` or `uncertain`, sorted by `final_score DESC`.

---

## How the pieces fit together

### Data flow

```
seeds.csv
  → seeds table (raw_text)
  → LLM title extraction
  → sources.search_all()
  → papers + paper_sources
  → harvest_metadata()
  → deduplicate()
  → screen_papers()
  → compute_scores()
  → export_*(comprehensive.csv, final_ranked.csv)
```

Snowballing injects new rows back into `papers`, causing the pipeline to re-run harvest → dedupe → screen → score on the expanded set.

### Central state: SQLite

Key tables:

- `seeds` — raw input and extraction status
- `papers` — canonical deduplicated papers
- `paper_sources` — raw API responses per source
- `criteria` — versioned inclusion/exclusion text
- `screening_decisions` — verdicts per paper per criteria version
- `weight_config` — versioned scoring weights
- `paper_scores` — scores per paper per weight version
- `harvest_log` — snowball-round statistics

### Human-in-the-loop surfaces

- CLI (`lit_review/cli.py`) has commands for every stage plus `run-all`.
- FastAPI UI (`lit_review/ui/app.py`) lets you:
  - Upload seeds
  - Trigger stages
  - Override LLM verdicts
  - Manage criteria/weights versions
  - Review duplicate candidates
  - Download CSV exports

### Reproducibility safeguards

- Criteria and weights are versioned; nothing is overwritten.
- Raw API responses are stored in `paper_sources`.
- Every score stores its full component breakdown as JSON.
- LLM calls use temperature 0 and structured JSON schemas for deterministic parsing.
