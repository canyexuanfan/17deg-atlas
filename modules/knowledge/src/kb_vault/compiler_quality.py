from __future__ import annotations

import re
from typing import Iterable

from .core import KBError


_QUESTION_MARK_RUN = re.compile(r"[?？]{3,}")


def validate_generated_text(
    value: str,
    *,
    label: str,
    minimum_alnum: int,
) -> str:
    """Reject obviously damaged or empty Agent-generated knowledge before persistence."""
    cleaned = value.strip()
    if not cleaned:
        raise KBError(f"{label} is required")
    if "\ufffd" in cleaned or _QUESTION_MARK_RUN.search(cleaned):
        raise KBError(f"{label} contains damaged or replacement characters")
    if any(ord(character) < 32 and character not in "\n\r\t" for character in cleaned):
        raise KBError(f"{label} contains unsupported control characters")
    if sum(character.isalnum() for character in cleaned) < minimum_alnum:
        raise KBError(f"{label} does not contain enough meaningful text")
    return cleaned


def normalize_topic_names(values: Iterable[str]) -> list[str]:
    """Normalize and de-duplicate proposed topics without silently repairing corruption."""
    topics: list[str] = []
    seen: set[str] = set()
    for value in values:
        cleaned = " ".join(value.split())
        if not cleaned:
            continue
        validate_generated_text(
            cleaned,
            label="workspace Wiki topic",
            minimum_alnum=2,
        )
        if len(cleaned) > 80:
            raise KBError("workspace Wiki topic must not exceed 80 characters")
        key = cleaned.casefold()
        if key in seen:
            continue
        seen.add(key)
        topics.append(cleaned)
    return topics


def validate_candidate_payload(
    *,
    summary: str,
    card_question: str,
    card_answer: str,
    topic_names: Iterable[str],
) -> tuple[str, str, str, list[str]]:
    summary_text = validate_generated_text(
        summary,
        label="workspace Wiki summary",
        minimum_alnum=12,
    )
    question = validate_generated_text(
        card_question,
        label="workspace Wiki card question",
        minimum_alnum=4,
    )
    answer = validate_generated_text(
        card_answer,
        label="workspace Wiki card answer",
        minimum_alnum=8,
    )
    topics = normalize_topic_names(topic_names)
    if not topics:
        raise KBError("workspace Wiki import requires at least one meaningful topic")
    return summary_text, question, answer, topics
