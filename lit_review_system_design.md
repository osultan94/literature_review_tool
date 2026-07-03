# Literature Review Automation — System Design

## 1. Goals & Scope

A local, human-in-the-loop pipeline that takes a seed CSV of paper references, resolves and expands them via academic APIs, screens them against your inclusion/exclusion criteria using a local LLM (Qwen3:8b via Ollama), computes a defensible relevance weight per paper, and exposes everything through a UI where you can tune criteria/weights and override individual decisions. Two CSV artifacts come out the other end: a full audit trail of everything fetched, and a final ranked shortlist.

The system has to serve two masters: it needs to be fast/automatable enough to be worth building, but every decision it makes needs to be traceable, since this will likely underpin your thesis's related-work methodology. Reproducibility and auditability are first-class requirements, not an afterthought.

---

## 2. High-Level Pipeline

```
Seed CSV
   │
   ▼
[0] Seed Ingestion & Title Extraction (LLM)
   │
   ▼
[1] Paper Resolution (title → canonical paper ID)
   │
   ▼
[2] Metadata Harvesting & Reconciliation (multi-source)
   │
   ▼
[3] Deduplication
   │
   ▼
[4] Snowballing (backward/forward citation expansion) ──┐
   │                                                      │ (loops back into [2]/[3])
   ▼                                                      │
[5] LLM Abstract Screening (inclusion/exclusion) ◄────────┘
   │
   ▼
[6] Weight Computation
   │
   ▼
[7] Saturation Check → (stop, or feed new seeds back to [4])
   │
   ▼
[8] Artifact Generation → comprehensive.csv + final_ranked.csv
```

All stages read/write a central SQLite database. The UI sits on top of the same database and can trigger any stage manually or edit its inputs (criteria, weights, seed list) between runs.

---

## 3. Data Model

Everything hinges on getting this schema right early, since every later stage (weighting, UI, audit trail) depends on it. Rough conceptual tables, not final DDL:

### 3.1 `seeds`
One row per line in your original CSV.
| field | purpose |
|---|---|
| id | PK |
| raw_text | original CSV row, untouched |
| extracted_title | LLM's title extraction output |
| extraction_confidence | LLM self-reported or heuristic confidence |
| status | pending / resolved / ambiguous / failed |
| resolved_paper_id | FK → papers, once matched |

### 3.2 `papers`
The canonical record per unique paper (post-dedup).
| field | purpose |
|---|---|
| id | PK, internal |
| title | canonical/normalized title |
| doi | for dedup matching |
| primary_source_id | which API's record was treated as authoritative for conflicting fields |
| abstract | for LLM screening |
| venue | conference/journal name |
| venue_tier | your manual/derived tier mapping (see §5) |
| pub_date | |
| citation_count | |
| citation_count_fetched_at | since this number drifts — snapshot it |
| authors | |
| origin | seed / snowball-backward / snowball-forward |
| origin_paper_id | if from snowballing, which paper it came from |
| discovery_round | which harvest/snowball iteration surfaced it (for saturation tracking) |

### 3.3 `paper_sources` (raw, pre-reconciliation)
One row per (paper, API source) — keeps S2's version and OpenAlex's version separately before you decide which wins. Critical for debugging metadata conflicts later.
| field | purpose |
|---|---|
| paper_id | FK |
| source_name | semantic_scholar / openalex / arxiv / crossref |
| source_paper_id | that API's native ID |
| raw_json | full raw response, for audit |
| fetched_at | |

### 3.4 `criteria` (versioned)
Never overwrite — always insert a new version so every past decision remains attributable to the criteria version active when it was made.
| field | purpose |
|---|---|
| version | incrementing int |
| criteria_text | the actual inclusion/exclusion rules, as given to the LLM |
| created_at | |
| active | only one version active at a time |

### 3.5 `screening_decisions`
| field | purpose |
|---|---|
| paper_id | FK |
| criteria_version | FK → criteria, which version was used |
| llm_verdict | include / exclude / uncertain |
| llm_reason | one-line justification, criterion cited |
| llm_raw_response | full response for audit |
| model_name | qwen3:8b (or whatever you swap in later) |
| model_params | temperature, etc. |
| human_override | null / include / exclude |
| human_note | your reasoning if you override |
| decided_at | |

### 3.6 `weight_config` (versioned, same pattern as criteria)
| field | purpose |
|---|---|
| version | |
| component_weights | JSON: {citation_weight, recency_weight, venue_weight, llm_verdict_weight, ...} |
| normalization_method | see §5 |
| active | |

### 3.7 `harvest_log`
One row per API call/search round — this is what makes saturation tracking and PRISMA-style flow counts possible, and it's what you'd cite in a methodology section as evidence the search was systematic.
| field | purpose |
|---|---|
| round_number | |
| stage | seed_resolution / snowball_backward / snowball_forward / manual |
| source_paper_id | if snowballing, the seed of this round |
| query | if a search, the actual query string |
| results_count | |
| new_unique_count | papers not already in `papers` table |
| timestamp | |

---

## 4. Pipeline Stages, in Detail

### Stage 0 — Seed Ingestion & Title Extraction
Read the seed CSV line by line into `seeds`. Each raw line goes to the LLM with a tightly scoped prompt: *"extract only the paper title from this text, respond in strict JSON."* Since seed CSV rows might be full citations, messy pastes, or just a title, this needs to handle variable input quality. Flag low-confidence extractions (e.g., LLM returns something suspiciously long, or your own heuristic — like title having no matches later — retroactively flags it) for manual review rather than silently trusting the local 8B model here.

### Stage 1 — Paper Resolution
Take `extracted_title`, query Semantic Scholar / OpenAlex by title match. This is a risk point: title-matching against a search index means you have to actively guard against picking a similarly-titled but wrong paper. Practical approach: take the top match, but also check title similarity (e.g., token overlap or edit distance) against a threshold — below threshold, mark `ambiguous` and surface for manual pick in the UI rather than auto-resolving. Store all candidate matches, not just the winner, so you can review misses later.

### Stage 2 — Metadata Harvesting & Reconciliation
For each resolved paper, hit multiple sources (Semantic Scholar primary, OpenAlex as cross-check/fallback, CrossRef for DOI-level bibliographic data, arXiv for preprint-specific fields). Store each source's raw response in `paper_sources`. Reconciliation logic decides which source "wins" per field when they disagree — e.g., prefer Semantic Scholar for citation counts (updated frequently), CrossRef for canonical venue/DOI, OpenAlex as fallback for anything missing. This mapping should be an explicit, documented rule set, not ad hoc — it's part of what makes the pipeline defensible as a method.

### Stage 3 — Deduplication
DOI match first (cheapest, most reliable). Fall back to fuzzy title + author-overlap matching for papers lacking a DOI (common for preprints/workshop papers). Anything below a similarity threshold but suspiciously close should be flagged for manual merge/reject rather than auto-merged — false merges are worse than false duplicates here since they'd silently drop a paper.

### Stage 4 — Snowballing
Given any paper's ID, both Semantic Scholar and OpenAlex expose direct reference/citation lists — no extra scraping needed. Backward snowballing = pull what a paper cites; forward = pull what cites it. New papers found this way re-enter Stage 2 with `origin = snowball-backward/forward` and `origin_paper_id` set, and get tagged with the current `discovery_round`. You'll typically seed this from your strongest 5–10 papers first, then decide each round whether to snowball further based on the saturation check.

### Stage 5 — LLM Abstract Screening
For every paper with a fetched abstract, run it against the *active* criteria version through Qwen3:8b, asking for a structured verdict (include/exclude/uncertain) plus a short reason tied to a specific criterion. This is the core "tedious work removal" step. Given local 8B model reliability is meaningfully below a frontier model's, I'd build in:
- Strict JSON schema enforcement (Ollama supports constrained/grammar-based generation — worth using rather than hoping the model behaves)
- Temperature 0 for reproducibility
- A mandatory "uncertain" option rather than forcing binary — forcing binary on a smaller model tends to produce confident-sounding wrong answers
- Metadata pre-filtering *before* the LLM call where possible (e.g., a paper outside your date range doesn't need an LLM call at all) — cheaper and removes a class of LLM error entirely

### Stage 6 — Weight Computation
Combine metadata signals + LLM verdict into a single score per paper. Full design in §5.

### Stage 7 — Saturation Check
After each harvest/snowball round, compute `new_unique_count / results_count` from `harvest_log`. When this ratio drops below a threshold (you defined ~5% earlier) for consecutive rounds, surface a "saturation reached" signal in the UI rather than auto-stopping — you stay in the loop on the actual stop decision.

### Stage 8 — Artifact Generation
- **comprehensive.csv**: every paper ever fetched, all metadata columns, all LLM decision fields, human override fields, weight breakdown. This is your audit trail.
- **final_ranked.csv**: papers with `human_override=include` (or `llm_verdict=include` where unreviewed), sorted by final weight descending.

---

## 5. Weighting & Scoring Design

This needs the most care, since an opaque scoring formula is hard to defend in a thesis methodology section. Recommended approach: **weighted sum of normalized components**, with every component's contribution stored per-paper (not just the final number), so you can always answer "why did paper X rank higher than paper Y."

Suggested components:

| component | notes |
|---|---|
| **LLM verdict** | include=1, uncertain=0.5, exclude=0 (or excluded entirely from the ranked set) |
| **Citation count** | raw counts are skewed — log-scale it, and consider citations-per-year to avoid unfairly penalizing recent papers |
| **Recency** | decayed score, e.g., linear or exponential falloff from your cutoff year |
| **Venue tier** | you'll need a manually maintained lookup table (top-tier venues you already know in RAG/IR: NeurIPS, ACL, EMNLP, SIGIR, etc. vs. workshop/unknown) — this is inherently a judgment call, so keep it editable and transparent rather than pretending it's objective |
| **Criteria-match strength** | if your criteria have sub-checks (e.g., "addresses retrieval AND generation" vs. just one), weight papers matching more sub-criteria higher |

**Normalization**: per-batch min-max normalization is simplest but shifts if you rerun with a different paper set; a fixed reference scale (e.g., citations-per-year capped at some ceiling) is more stable across runs — worth deciding upfront which you want since it affects reproducibility.

Weights themselves should be a config the UI exposes as sliders/inputs, versioned like criteria, so changing a weight and re-scoring doesn't silently overwrite the history of what a paper's rank *was* under the previous config.

---

## 6. LLM Task Design (Qwen3:8b via Ollama)

Two distinct, narrow tasks — don't ask one prompt to do both extraction and screening:

**Task A — Title extraction (Stage 0)**: input = raw seed CSV line, output = strict JSON `{title, confidence}`. Keep the prompt minimal; this is a simple extraction task well within an 8B model's comfort zone.

**Task B — Abstract screening (Stage 5)**: input = abstract + active criteria text, output = strict JSON `{verdict, matched_criterion, reason}`. This is the harder task — semantic judgment against multi-part criteria is where smaller local models are more error-prone than something like GPT-4-class or Claude. Mitigations:
- Constrain the criteria to simple, checklist-style boolean conditions rather than nuanced qualitative judgments — the more the task looks like structured matching rather than reasoning, the better an 8B model performs.
- Sample-audit a random 10–15% of both include and exclude verdicts yourself early on, to calibrate whether Qwen3:8b's error rate is acceptable for this specific criteria set before trusting it at scale.
- Keep "uncertain" cheap to trigger — the LLM should default to uncertain rather than guess when a criterion is ambiguous relative to the abstract.

---

## 7. UI Requirements

Given you want to manage weights, criteria, and review decisions interactively, this needs to be a real data-management UI, not just a script runner. Screens:

1. **Dashboard** — saturation curve over rounds, counts by status (included/excluded/uncertain/pending), last run timestamps.
2. **Papers table** — sortable/filterable grid: title, venue, year, citations, LLM verdict, weight, override control. This is the main workspace — needs to handle a few hundred to low thousands of rows responsively.
3. **Criteria editor** — view/edit active criteria, see version history, "activate this version" (never destructive edits).
4. **Weight config editor** — sliders/inputs per component, live preview of how re-scoring would shift the top-N ranking before committing.
5. **Seed management** — upload/view seed CSV, see extraction/resolution status per row, manually fix ambiguous resolutions.
6. **Decision audit view** — the sample-review workflow: surface a random subset of LLM verdicts for you to confirm/override, tracks your agreement rate with the model over time.
7. **Run controls** — trigger harvest / snowball round / re-screen / re-score, see progress, since these involve external API calls and shouldn't block the UI.
8. **Export** — generate the two CSVs on demand.

---

## 8. Tech Stack Notes

Given your existing stack (Python, SQLite, threaded workers from the Hindawi scraper, prior Gemini/OpenRouter LLM pipeline experience):

- **Backend/data**: Python + SQLite, consistent with what you've already built.
- **LLM serving**: Ollama running Qwen3:8b locally, called over its local HTTP API — same pattern as your OpenAI-compatible OpenRouter pipeline, just pointed at localhost.
- **API clients**: Semantic Scholar API, OpenAlex API, arXiv API, CrossRef API — all free, no scraping needed.
- **UI**: this is the one place I'd steer you away from tkinter despite your familiarity with it from the Hindawi project — tkinter is workable for a form-and-buttons app, but a filterable/sortable/editable table with live re-scoring previews is much more naturally a web UI. A lightweight local web app (FastAPI backend + a simple table-grid frontend, or something like Streamlit/Gradio if you want to minimize frontend work at the cost of some UI flexibility) will save you real time here. Worth weighing Streamlit (fast to build, less flexible) against a small custom FastAPI+HTML setup (more control over the grid interactions you'll want for weight-tuning) before committing.
- **Concurrency**: your threaded-download pattern from the Hindawi scraper applies directly to Stage 2/4's API harvesting — rate-limited concurrent fetches with backoff.

---

## 9. Reproducibility & Logging

Since this pipeline's output likely feeds your thesis's related-work methodology, treat logging as a deliverable, not just debugging aid:
- Every API call logged with query, timestamp, and raw response.
- Every LLM call logged with exact prompt, model name, parameters, and raw response — not just the parsed verdict.
- Criteria and weights versioned, never overwritten.
- Every paper's final status traceable to: which criteria version screened it, which weight version scored it, whether a human overrode it and why.

This turns the tool itself into something you can describe in a methods section ("papers were identified via X, screened against criteria version Y using model Z, with human audit of N% of decisions") — which is exactly the kind of reproducibility PRISMA-style rigor is trying to achieve, just automated.

---

## 10. Open Design Decisions to Resolve Before Building

- **Metadata conflict resolution rule** — decide the source-priority order per field now, rather than mid-build.
- **Title-match confidence threshold** — how aggressive should auto-resolution be vs. deferring to you.
- **Citation count staleness** — snapshot once, or periodically refresh? Affects whether weights drift over time for the same paper.
- **Venue tier list** — this is inherently manual/subjective; decide whether it's a hard filter or just a weight component (I'd lean weight component, since a hard filter risks excluding good workshop papers).
- **8B model reliability ceiling** — worth running a small pilot (20–30 papers) with manual verification before trusting Task B at scale; if error rate is too high, options are prompt refinement, switching to a larger local model, or hybrid (8B for clear cases, escalate uncertain ones to a stronger model).
- **No-match fallback** — some seed papers won't resolve via any API (self-published, non-indexed) — decide whether these get manually entered with hand-typed metadata or dropped.

---

## 11. Suggested Build Order

1. Schema + seed ingestion + Task A (title extraction) + paper resolution
2. Metadata harvesting + reconciliation + dedup
3. Task B (LLM screening) + criteria versioning
4. Weight computation engine
5. Snowballing + saturation tracking
6. CSV artifact export
7. UI (can start in parallel with 3–4 once schema is stable, since it's mostly a viewer/editor over the DB)

This ordering front-loads the parts with the most design risk (title resolution accuracy, LLM screening reliability) so you find out early if either needs rework, before the UI and weighting layers are built on top of them.
