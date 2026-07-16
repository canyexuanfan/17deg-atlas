from __future__ import annotations

import copy
from typing import Any, Mapping


OBJECT_KINDS = (
    "source_profile",
    "subscription",
    "capture_event",
    "raw",
    "wiki",
    "release",
    "run",
    "feedback",
)
MEDIA_TYPES = (
    "article",
    "screenshot",
    "site",
    "data",
    "book",
    "transcript",
    "idea",
    "conversation",
    "file",
)
CAPTURE_PURPOSES = (
    "trend",
    "deep_learning",
    "case",
    "demand",
    "feedback",
    "reference",
)
WIKI_KINDS = ("source_summary", "atomic_card", "topic_page")
CARD_KINDS = ("concept", "claim", "question", "pattern", "practice", "case", "decision")
CAPTURE_STATES = ("captured", "queued", "accepted", "rejected", "duplicate", "failed")
COMPILE_STATES = ("uncompiled", "compiling", "compiled", "needs_recompile", "failed")
REVIEW_STATES = ("candidate", "reviewed", "verified", "rejected")
RELATION_TYPES = (
    "derived_from",
    "authored_by",
    "edited_by",
    "coauthored_with",
    "ai_assisted_by",
    "publishes",
    "selected_for",
    "supports",
    "contradicts",
    "example_of",
    "causes",
    "applies_to",
    "updates",
    "supersedes",
)
RELATION_REVIEW_STATES = ("candidate", "reviewed", "verified")
RELATION_CREATORS = ("human", "agent", "importer")
ORIGIN_KINDS = ("self", "external", "mixed", "unknown")
AUTHORSHIP_STATUSES = (
    "self_authored",
    "edited",
    "coauthored",
    "ai_assisted",
    "external",
    "unknown",
)
INTENDED_ROLES = (
    "memory",
    "evidence",
    "knowledge",
    "creation",
    "cognition",
    "capability",
    "work",
    "product",
    "relationship",
    "governance",
    "publication",
    "unknown",
)
ELIGIBILITY_STATES = ("denied", "candidate", "approved")
TRAINING_PERMISSIONS = ("denied", "undecided", "allowed")
CLARIFICATION_STATES = ("not_needed", "required", "answered")


def governance_requirements(domain_kind: str) -> dict[str, Any]:
    """Return the shared semantic contract with domain-specific confirmation authority."""

    profiles = {
        "personal": {
            "canonical_authority": "subject:self",
            "taxonomy_authority": "subject:self",
            "required_controls": ["explicit-confirmation", "recoverable-history"],
        },
        "enterprise": {
            "canonical_authority": "role:knowledge-owner",
            "taxonomy_authority": "role:taxonomy-steward",
            "required_controls": ["role-separation", "approval", "audit"],
        },
        "public-national": {
            "canonical_authority": "lawful-designated-authority",
            "taxonomy_authority": "lawful-records-authority",
            "required_controls": ["lawful-purpose", "multi-party-approval", "oversight", "appeal"],
        },
    }
    try:
        return copy.deepcopy(profiles[domain_kind])
    except KeyError as exc:
        raise ValueError("unsupported domain governance profile") from exc


def new_semantic_fields(
    *,
    object_kind: str,
    media_type: str | None = None,
    capture_purpose: str | None = None,
    wiki_kind: str | None = None,
    card_kind: str | None = None,
    topic_ids: list[str] | None = None,
    relations: list[Mapping[str, Any]] | None = None,
    capture_state: str | None = None,
    compile_state: str | None = None,
    review_state: str = "candidate",
    origin_kind: str = "unknown",
    authorship_status: str = "unknown",
    contributors: list[str] | None = None,
    source_refs: list[str] | None = None,
    interaction_refs: list[str] | None = None,
    intended_role: str | None = None,
    corpus_eligibility: str = "denied",
    style_eligibility: str = "denied",
    training_permission: str = "denied",
    clarification_status: str | None = None,
    clarification_refs: list[str] | None = None,
) -> dict[str, Any]:
    default_roles = {
        "source_profile": "evidence",
        "subscription": "evidence",
        "capture_event": "evidence",
        "raw": "unknown",
        "wiki": "knowledge",
        "release": "publication",
        "run": "work",
        "feedback": "knowledge",
    }
    resolved_role = intended_role or default_roles.get(object_kind, "unknown")
    resolved_clarification = clarification_status
    if resolved_clarification is None:
        responsibility_unknown = object_kind in ("raw", "wiki", "release") and (
            authorship_status == "unknown"
            or resolved_role == "unknown"
            or corpus_eligibility == "candidate"
            or style_eligibility == "candidate"
            or training_permission == "undecided"
        )
        resolved_clarification = "required" if responsibility_unknown else "not_needed"
    fields = {
        "media_type": media_type,
        "capture_purpose": capture_purpose,
        "wiki_kind": wiki_kind,
        "card_kind": card_kind,
        "topic_ids": list(topic_ids or []),
        "relations": [dict(item) for item in (relations or [])],
        "capture_state": capture_state,
        "compile_state": compile_state,
        "review_state": review_state,
        "origin_kind": origin_kind,
        "authorship_status": authorship_status,
        "contributors": list(contributors or []),
        "source_refs": list(source_refs or []),
        "interaction_refs": list(interaction_refs or []),
        "intended_role": resolved_role,
        "corpus_eligibility": corpus_eligibility,
        "style_eligibility": style_eligibility,
        "training_permission": training_permission,
        "clarification_status": resolved_clarification,
        "clarification_refs": list(clarification_refs or []),
    }
    issues = validate_semantic_fields({"object_kind": object_kind, **fields})
    if issues:
        raise ValueError("; ".join(issues))
    return fields


def materialize_semantic_fields(envelope: Mapping[str, Any]) -> dict[str, Any]:
    """Return a KB2-compatible view without rewriting a v1/v2 object."""

    result = copy.deepcopy(dict(envelope))
    kind = str(result.get("object_kind", ""))
    result.setdefault("media_type", None)
    result.setdefault("capture_purpose", None)
    result.setdefault("wiki_kind", None)
    result.setdefault("card_kind", None)
    result.setdefault("topic_ids", [])
    result.setdefault("relations", [])
    result.setdefault("capture_state", None)
    result.setdefault("compile_state", "uncompiled" if kind == "raw" else None)
    if "review_state" not in result:
        maturity = str(result.get("maturity", "seed"))
        result["review_state"] = "verified" if maturity == "verified" else (
            "reviewed" if maturity == "reviewed" else "candidate"
        )
    result.setdefault("origin_kind", "unknown")
    result.setdefault("authorship_status", "unknown")
    result.setdefault("contributors", [])
    result.setdefault("source_refs", [])
    result.setdefault("interaction_refs", [])
    result.setdefault("intended_role", "unknown")
    result.setdefault("corpus_eligibility", "denied")
    result.setdefault("style_eligibility", "denied")
    result.setdefault("training_permission", "denied")
    result.setdefault("clarification_status", "required" if kind in ("raw", "wiki", "release") else "not_needed")
    result.setdefault("clarification_refs", [])
    return result


def clarification_questions(envelope: Mapping[str, Any]) -> list[str]:
    questions: list[str] = []
    if envelope.get("authorship_status") == "unknown":
        questions.append(
            "Is this content self-authored, edited from another source, coauthored, AI-assisted, or external?"
        )
    if envelope.get("intended_role") == "unknown":
        questions.append(
            "Should this be treated as memory, reference knowledge, a personal creation, cognition, capability, work, product, relationship, governance, or a publication?"
        )
    if envelope.get("media_type") == "conversation" and not envelope.get("interaction_refs"):
        questions.append(
            "Which conversation and message or segment identifiers support this knowledge extract?"
        )
    if envelope.get("rights") == "unknown":
        questions.append(
            "What rights are available for storing, adapting, publishing, commercial use, or redistribution?"
        )
    if envelope.get("corpus_eligibility") == "candidate" or envelope.get("style_eligibility") == "candidate":
        questions.append(
            "May this work be included in a personal or style corpus?"
        )
    if envelope.get("training_permission") == "undecided":
        questions.append("May this work be used for model training?")
    return questions


def validate_semantic_fields(envelope: Mapping[str, Any]) -> list[str]:
    issues: list[str] = []
    kind = envelope.get("object_kind")
    if kind not in OBJECT_KINDS:
        issues.append("object_kind is invalid")

    nullable_enums = (
        ("media_type", MEDIA_TYPES),
        ("capture_purpose", CAPTURE_PURPOSES),
        ("wiki_kind", WIKI_KINDS),
        ("card_kind", CARD_KINDS),
        ("capture_state", CAPTURE_STATES),
        ("compile_state", COMPILE_STATES),
    )
    for field, choices in nullable_enums:
        value = envelope.get(field)
        if value is not None and value not in choices:
            issues.append(f"{field} is invalid")

    if envelope.get("review_state") not in REVIEW_STATES:
        issues.append("review_state is invalid")
    if envelope.get("origin_kind") not in ORIGIN_KINDS:
        issues.append("origin_kind is invalid")
    if envelope.get("authorship_status") not in AUTHORSHIP_STATUSES:
        issues.append("authorship_status is invalid")
    if envelope.get("intended_role") not in INTENDED_ROLES:
        issues.append("intended_role is invalid")
    if envelope.get("corpus_eligibility") not in ELIGIBILITY_STATES:
        issues.append("corpus_eligibility is invalid")
    if envelope.get("style_eligibility") not in ELIGIBILITY_STATES:
        issues.append("style_eligibility is invalid")
    if envelope.get("training_permission") not in TRAINING_PERMISSIONS:
        issues.append("training_permission is invalid")
    if envelope.get("clarification_status") not in CLARIFICATION_STATES:
        issues.append("clarification_status is invalid")
    for field in ("contributors", "source_refs", "interaction_refs", "clarification_refs"):
        values = envelope.get(field)
        if not isinstance(values, list) or any(not isinstance(item, str) or not item for item in (values or [])):
            issues.append(f"{field} must be a string array")
        elif len(values) != len(set(values)):
            issues.append(f"{field} must not contain duplicates")
    if envelope.get("clarification_status") == "answered" and (
        envelope.get("authorship_status") == "unknown" or envelope.get("intended_role") == "unknown"
    ):
        issues.append("answered clarification requires known authorship_status and intended_role")
    if envelope.get("clarification_status") == "required" and envelope.get("review_state") != "candidate":
        issues.append("unresolved clarification must remain a candidate")
    if (
        envelope.get("schema_version") == 4
        and kind == "raw"
        and envelope.get("media_type") == "conversation"
        and envelope.get("intended_role") in ("evidence", "knowledge")
        and not envelope.get("interaction_refs")
    ):
        issues.append("conversation knowledge raw requires interaction_refs")
    if (
        envelope.get("corpus_eligibility") == "approved"
        or envelope.get("style_eligibility") == "approved"
        or envelope.get("training_permission") == "allowed"
    ) and envelope.get("authorship_status") in ("unknown", "external"):
        issues.append("corpus, style, or training approval requires confirmed non-external authorship")
    topic_ids = envelope.get("topic_ids")
    if not isinstance(topic_ids, list) or any(
        not isinstance(item, str) or not item.startswith("topic_") for item in (topic_ids or [])
    ):
        issues.append("topic_ids must contain stable topic_ ids")
    elif len(topic_ids) != len(set(topic_ids)):
        issues.append("topic_ids must not contain duplicates")

    relations = envelope.get("relations")
    if not isinstance(relations, list):
        issues.append("relations must be an array")
    else:
        relation_ids: list[str] = []
        for index, relation in enumerate(relations):
            prefix = f"relations[{index}]"
            if not isinstance(relation, Mapping):
                issues.append(f"{prefix} must be an object")
                continue
            relation_id = relation.get("relation_id")
            if not isinstance(relation_id, str) or not relation_id.startswith("rel_"):
                issues.append(f"{prefix}.relation_id is invalid")
            else:
                relation_ids.append(relation_id)
            if relation.get("type") not in RELATION_TYPES:
                issues.append(f"{prefix}.type is invalid")
            if not isinstance(relation.get("target_id"), str) or not relation.get("target_id"):
                issues.append(f"{prefix}.target_id is required")
            if not isinstance(relation.get("statement"), str) or not relation.get("statement"):
                issues.append(f"{prefix}.statement is required")
            evidence_ids = relation.get("evidence_ids")
            if not isinstance(evidence_ids, list) or any(
                not isinstance(item, str) or not item for item in (evidence_ids or [])
            ):
                issues.append(f"{prefix}.evidence_ids must be a string array")
            if relation.get("review_state") not in RELATION_REVIEW_STATES:
                issues.append(f"{prefix}.review_state is invalid")
            if relation.get("created_by") not in RELATION_CREATORS:
                issues.append(f"{prefix}.created_by is invalid")
        if len(relation_ids) != len(set(relation_ids)):
            issues.append("relation ids must not contain duplicates")

    wiki_kind = envelope.get("wiki_kind")
    card_kind = envelope.get("card_kind")
    if kind != "wiki" and (wiki_kind is not None or card_kind is not None):
        issues.append("wiki_kind and card_kind only apply to wiki objects")
    if card_kind is not None and wiki_kind != "atomic_card":
        issues.append("card_kind requires wiki_kind atomic_card")
    if wiki_kind == "atomic_card" and card_kind is None:
        issues.append("atomic_card requires card_kind")
    if kind == "capture_event" and envelope.get("capture_state") is None:
        issues.append("capture_event requires capture_state")
    if kind == "raw" and envelope.get("compile_state") is None:
        issues.append("raw requires compile_state")
    return issues
