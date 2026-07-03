"""LLM prompts."""

from __future__ import annotations


def title_extraction_prompt(raw_text: str) -> str:
    return (
        "Extract the paper title from the following reference text. "
        "Respond only with a JSON object containing 'title' and 'confidence'.\n\n"
        "Reference text:\n"
        f"{raw_text}\n\n"
        "Return strict JSON: {\"title\": \"...\", \"confidence\": 0.95}"
    )


def screening_prompt(abstract: str, criteria_text: str) -> str:
    return (
        "You are screening academic papers for a systematic literature review.\n\n"
        "Inclusion/Exclusion Criteria:\n"
        f"{criteria_text}\n\n"
        "Abstract:\n"
        f"{abstract}\n\n"
        "Respond only with a JSON object containing:\n"
        "- 'verdict': one of 'include', 'exclude', or 'uncertain'\n"
        "- 'matched_criterion': the specific criterion that most strongly supports the verdict\n"
        "- 'reason': a one-sentence justification\n\n"
        "If the abstract is missing, too short, or does not provide enough information, "
        "choose 'uncertain'."
    )
