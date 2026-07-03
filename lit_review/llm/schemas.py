"""JSON schemas for structured LLM outputs."""

from __future__ import annotations

EXTRACTION_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
    },
    "required": ["title", "confidence"],
    "additionalProperties": False,
}


SCREENING_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["include", "exclude", "uncertain"]},
        "matched_criterion": {"type": "string"},
        "reason": {"type": "string"},
    },
    "required": ["verdict", "matched_criterion", "reason"],
    "additionalProperties": False,
}
