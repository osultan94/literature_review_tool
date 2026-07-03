"""Tests for optional/disabled data sources."""

from __future__ import annotations

import httpx
import pytest

from lit_review import config
from lit_review.sources import search_all


@pytest.mark.asyncio
async def test_search_all_skips_semantic_scholar_when_disabled(monkeypatch) -> None:
    """When USE_SEMANTIC_SCHOLAR is False, S2 is not queried even if it would fail."""
    monkeypatch.setattr(config, "USE_SEMANTIC_SCHOLAR", False)

    calls = {"s2": False, "openalex": False}

    async def failing_s2_search(*args, **kwargs):  # noqa: ANN002, ANN003
        calls["s2"] = True
        raise RuntimeError("S2 should not be called")

    import lit_review.sources.semantic_scholar as s2_mod

    monkeypatch.setattr(s2_mod, "search_by_title", failing_s2_search)

    async def empty_openalex_search(*args, **kwargs):  # noqa: ANN002, ANN003
        calls["openalex"] = True
        return []

    import lit_review.sources.openalex as oa_mod

    monkeypatch.setattr(oa_mod, "search_by_title", empty_openalex_search)

    async with httpx.AsyncClient() as client:
        await search_all("some title", client=client)

    assert calls["s2"] is False
    assert calls["openalex"] is True
