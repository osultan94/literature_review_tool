"""Ollama LLM client with structured JSON output."""

from __future__ import annotations

import json
from typing import Any, cast

import httpx
import structlog

from lit_review import config

logger = structlog.get_logger()


class LLMError(Exception):
    """Raised when an LLM call fails permanently."""


class OllamaClient:
    """Thin client around the Ollama generate API."""

    def __init__(
        self,
        host: str | None = None,
        model: str | None = None,
        temperature: float | None = None,
        timeout: float = 120.0,
    ) -> None:
        self.host = (host or config.OLLAMA_HOST).rstrip("/")
        self.model = model or config.OLLAMA_MODEL
        self.temperature = temperature if temperature is not None else config.OLLAMA_TEMPERATURE
        self.timeout = timeout
        self._client: httpx.AsyncClient | None = None

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.timeout)
        return self._client

    async def generate(
        self,
        prompt: str,
        schema: dict[str, Any] | None = None,
        temperature: float | None = None,
    ) -> dict[str, Any]:
        """Generate and parse structured JSON from Ollama.

        Args:
            prompt: The prompt text.
            schema: Optional JSON schema to constrain generation.
            temperature: Override default temperature.

        Returns:
            Parsed JSON dict.

        Raises:
            LLMError: If the response cannot be parsed or the API fails.
        """
        temp = temperature if temperature is not None else self.temperature
        payload: dict[str, Any] = {
            "model": self.model,
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": temp},
        }
        if schema is not None:
            payload["format"] = schema
        else:
            payload["format"] = "json"

        try:
            response = await self.client.post(
                f"{self.host}/api/generate",
                json=payload,
            )
            response.raise_for_status()
            data = response.json()
        except httpx.HTTPError as exc:
            logger.error("ollama_http_error", error=str(exc), model=self.model)
            raise LLMError(f"Ollama HTTP error: {exc}") from exc

        raw_response = data.get("response", "")
        if not raw_response:
            raise LLMError("Ollama returned an empty response.")

        try:
            parsed = json.loads(raw_response)
        except json.JSONDecodeError as exc:
            logger.error("ollama_json_parse_error", raw_response=raw_response, error=str(exc))
            raise LLMError(f"Failed to parse LLM response as JSON: {raw_response}") from exc

        logger.info(
            "ollama_generate_success",
            model=self.model,
            temperature=temp,
            response_keys=list(parsed.keys()),
        )
        return cast(dict[str, Any], parsed)

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def __aenter__(self) -> OllamaClient:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: Any,
    ) -> None:
        await self.close()
