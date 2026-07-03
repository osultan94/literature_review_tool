"""Smoke tests for the FastAPI web UI."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from lit_review.ui.app import app


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    # Point DB to temp path so tests stay isolated.
    import lit_review.config as cfg

    cfg.DEFAULT_DB_PATH = tmp_path / "ui.db"
    from lit_review.db import init_db

    init_db(cfg.DEFAULT_DB_PATH)
    return TestClient(app)


def test_index_html(client: TestClient) -> None:
    res = client.get("/")
    assert res.status_code == 200
    assert "Literature Review Automation" in res.text


def test_dashboard_endpoint(client: TestClient) -> None:
    res = client.get("/api/dashboard")
    assert res.status_code == 200
    data = res.json()
    assert "seeds" in data
    assert "papers" in data
    assert "saturation" in data


def test_papers_endpoint(client: TestClient) -> None:
    res = client.get("/api/papers")
    assert res.status_code == 200
    assert isinstance(res.json(), list)


def test_criteria_endpoint(client: TestClient) -> None:
    res = client.get("/api/criteria")
    assert res.status_code == 200
    data = res.json()
    assert data["active"] == 1


def test_weights_endpoint(client: TestClient) -> None:
    res = client.get("/api/weights")
    assert res.status_code == 200
    data = res.json()
    assert data["active"] == 1


def test_seeds_endpoint(client: TestClient) -> None:
    res = client.get("/api/seeds")
    assert res.status_code == 200
    assert isinstance(res.json(), list)


def test_create_criteria(client: TestClient) -> None:
    res = client.post("/api/criteria", data={"text": "Include only surveys."})
    assert res.status_code == 200
    assert res.json()["version"] == 2

    res = client.get("/api/criteria")
    data = res.json()
    assert data["active"] == 2
