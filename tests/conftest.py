"""Pytest fixtures."""

from __future__ import annotations

from pathlib import Path

import pytest

from lit_review.db import init_db


@pytest.fixture
def tmp_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "test.db"
    init_db(db_path)
    return db_path


@pytest.fixture
def seed_csv(tmp_path: Path) -> Path:
    csv_path = tmp_path / "seeds.csv"
    csv_path.write_text(
        "Retrieval-Augmented Generation for Knowledge-Intensive NLP Tasks\n"
        "Lewis, P., et al. (2020). RAG. NeurIPS.\n"
    )
    return csv_path


class FakeOllamaClient:
    """Fake Ollama client that returns scripted responses."""

    def __init__(self, responses: list[dict] | None = None) -> None:
        self.responses = list(responses or [])
        self.calls: list[dict] = []
        self.model = "fake-model"
        self.temperature = 0.0

    async def generate(self, prompt: str, schema=None, temperature=None) -> dict:  # noqa: ANN001
        self.calls.append({"prompt": prompt, "schema": schema, "temperature": temperature})
        if self.responses:
            return self.responses.pop(0)
        return {"title": "", "confidence": 0.0}

    async def close(self) -> None:
        pass


@pytest.fixture
def fake_extractor() -> FakeOllamaClient:
    return FakeOllamaClient(
        [
            {
                "title": "Retrieval-Augmented Generation for Knowledge-Intensive NLP Tasks",
                "confidence": 0.95,
            },
            {"title": "RAG: Retrieval-Augmented Generation", "confidence": 0.8},
        ]
    )


@pytest.fixture
def fake_screener() -> FakeOllamaClient:
    return FakeOllamaClient(
        [
            {
                "verdict": "include",
                "matched_criterion": "RAG or IR+generation",
                "reason": "Directly addresses retrieval-augmented generation.",
            },
            {
                "verdict": "exclude",
                "matched_criterion": "vision/speech",
                "reason": "About computer vision.",
            },
        ]
    )


class FakeResponse:
    def __init__(self, json_data: dict, status: int = 200) -> None:
        self._json = json_data
        self.status_code = status

    def json(self) -> dict:
        return self._json

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise Exception(f"HTTP {self.status_code}")


@pytest.fixture
def s2_search_response() -> dict:
    return {
        "data": [
            {
                "paperId": "s2_123",
                "title": "Retrieval-Augmented Generation for Knowledge-Intensive NLP Tasks",
                "abstract": "We combine parametric and non-parametric memory...",
                "venue": "NeurIPS",
                "year": 2020,
                "citationCount": 5000,
                "externalIds": {"DOI": "10.1234/rag"},
                "authors": [{"name": "P. Lewis"}, {"name": "E. Perez"}],
            }
        ]
    }
