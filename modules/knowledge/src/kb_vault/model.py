from __future__ import annotations

import copy
from typing import Any, Mapping


CLASSIFICATION_LEVELS = ("public", "basic", "advanced", "core")
CLASSIFICATION_RANK = {level: rank for rank, level in enumerate(CLASSIFICATION_LEVELS)}
STORAGE_BACKENDS = ("local-git", "github", "object-store", "controlled-service")
ENCRYPTION_PROFILES = (
    "plaintext",
    "age-basic-v1",
    "age-advanced-v1",
    "age-core-v1",
    "managed",
)
DISTRIBUTION_CHANNELS = (
    "local-only",
    "public-plaintext",
    "public-ciphertext",
    "controlled-channel",
)


def classification_level_for_tier(tier: str) -> str:
    if tier == "archive":
        return "public"
    if tier not in CLASSIFICATION_LEVELS:
        raise ValueError(f"unsupported compatibility tier: {tier}")
    return tier


def compatibility_tier_for(*, classification_level: str, lifecycle: str) -> str:
    if classification_level not in CLASSIFICATION_LEVELS:
        raise ValueError(f"unsupported classification level: {classification_level}")
    if classification_level == "public" and lifecycle == "archived":
        return "archive"
    return classification_level


def encryption_profile_for(classification_level: str) -> str:
    profiles = {
        "public": "plaintext",
        "basic": "age-basic-v1",
        "advanced": "age-advanced-v1",
        "core": "age-core-v1",
    }
    try:
        return profiles[classification_level]
    except KeyError as exc:
        raise ValueError(
            f"unsupported classification level: {classification_level}"
        ) from exc


def highest_classification_level(sources: list[Mapping[str, Any]]) -> str:
    """Return the most restrictive source level for derived knowledge."""

    if not sources:
        raise ValueError("at least one source object is required")
    levels: list[str] = []
    for source in sources:
        classification = source.get("classification")
        if isinstance(classification, Mapping):
            level = str(classification.get("level", ""))
        else:
            level = classification_level_for_tier(str(source.get("tier", "")))
        if level not in CLASSIFICATION_LEVELS:
            raise ValueError(f"unsupported classification level: {level}")
        levels.append(level)
    return max(levels, key=CLASSIFICATION_RANK.__getitem__)


def default_classification(
    *, tier: str, timestamp: str, authority: str, reason: str
) -> dict[str, Any]:
    return {
        "level": classification_level_for_tier(tier),
        "reason": reason,
        "authority": authority,
        "classified_at": timestamp,
        "reviewed_at": None,
    }


def default_storage_binding(
    *,
    classification_level: str,
    content_ref: str,
    content_hash: str,
    backend: str = "local-git",
) -> dict[str, Any]:
    return {
        "backend": backend,
        "content_ref": content_ref,
        "encryption_profile": encryption_profile_for(classification_level),
        "key_version": "v1",
        "content_hash": content_hash,
    }


def default_distribution_decision(
    *,
    tier: str,
    catalog_visibility: str,
    human_confirmed: bool,
    timestamp: str,
    channel: str | None = None,
    audience: list[str] | None = None,
) -> dict[str, Any]:
    resolved_channel = channel or "local-only"
    resolved_audience = list(dict.fromkeys(audience or []))
    if not resolved_audience:
        resolved_audience = ["public"] if resolved_channel.startswith("public-") else ["self"]
    if resolved_channel == "controlled-channel" and resolved_audience == ["self"]:
        raise ValueError("controlled-channel requires an explicit non-self audience")
    if resolved_channel.startswith("public-") and resolved_audience != ["public"]:
        raise ValueError("public distribution requires the public audience")
    if (
        channel is None
        and tier in ("public", "archive")
        and catalog_visibility == "public"
        and human_confirmed
    ):
        resolved_channel = "public-plaintext"
    is_approved_distribution = resolved_channel != "local-only"
    return {
        "channel": resolved_channel,
        "audience": resolved_audience,
        "license_id": None,
        "approved_by": "subject:self" if is_approved_distribution and human_confirmed else None,
        "approved_at": timestamp if is_approved_distribution and human_confirmed else None,
    }


def new_orthogonal_fields(
    *,
    tier: str,
    lifecycle: str,
    content_ref: str,
    content_hash: str,
    catalog_visibility: str,
    human_confirmed: bool,
    timestamp: str,
    backend: str = "local-git",
    distribution_channel: str | None = None,
    distribution_audience: list[str] | None = None,
) -> dict[str, Any]:
    effective_lifecycle = "archived" if tier == "archive" else lifecycle
    classification = default_classification(
        tier=tier,
        timestamp=timestamp,
        authority="subject:self",
        reason="created-from-compatibility-tier",
    )
    expected_tier = compatibility_tier_for(
        classification_level=classification["level"],
        lifecycle=effective_lifecycle,
    )
    if expected_tier != tier:
        raise ValueError(
            f"tier {tier} conflicts with lifecycle {effective_lifecycle}; use {expected_tier}"
        )
    return {
        "classification": classification,
        "storage_binding": default_storage_binding(
            classification_level=classification["level"],
            content_ref=content_ref,
            content_hash=content_hash,
            backend=backend,
        ),
        "policy_refs": [],
        "distribution_decision": default_distribution_decision(
            tier=tier,
            catalog_visibility=catalog_visibility,
            human_confirmed=human_confirmed,
            timestamp=timestamp,
            channel=distribution_channel,
            audience=distribution_audience,
        ),
    }


def materialize_orthogonal_fields(
    envelope: Mapping[str, Any], *, content_ref: str | None = None
) -> dict[str, Any]:
    """Return a dual-read view without rewriting a legacy v1 object."""

    result = copy.deepcopy(dict(envelope))
    tier = str(result.get("tier", ""))
    timestamp = str(result.get("updated_at") or result.get("created_at") or "")
    lifecycle = str(result.get("lifecycle", "active"))
    if tier == "archive":
        lifecycle = "archived"
        result["lifecycle"] = lifecycle
    classification = result.get("classification")
    if not isinstance(classification, Mapping):
        classification = default_classification(
            tier=tier,
            timestamp=timestamp,
            authority="compatibility:legacy-tier",
            reason="derived-from-v1-tier",
        )
        result["classification"] = classification
    binding = result.get("storage_binding")
    if not isinstance(binding, Mapping):
        result["storage_binding"] = default_storage_binding(
            classification_level=str(classification.get("level", "")),
            content_ref=content_ref or str(result.get("content_ref", "")),
            content_hash=str(result.get("content_hash", "")),
        )
    result.setdefault("policy_refs", [])
    decision = result.get("distribution_decision")
    if not isinstance(decision, Mapping):
        result["distribution_decision"] = default_distribution_decision(
            tier=tier,
            catalog_visibility=str(result.get("catalog_visibility", "private")),
            human_confirmed=bool(result.get("human_confirmed", False)),
            timestamp=timestamp,
        )
    return result


def validate_orthogonal_fields(envelope: Mapping[str, Any]) -> list[str]:
    issues: list[str] = []
    version = envelope.get("schema_version")
    classification = envelope.get("classification")
    storage = envelope.get("storage_binding")
    policy_refs = envelope.get("policy_refs")
    distribution = envelope.get("distribution_decision")

    if not isinstance(classification, Mapping):
        issues.append("classification must be an object")
        level = ""
    else:
        level = str(classification.get("level", ""))
        if level not in CLASSIFICATION_LEVELS:
            issues.append("classification.level is invalid")
        for field in ("reason", "authority", "classified_at"):
            if not isinstance(classification.get(field), str) or not classification.get(field):
                issues.append(f"classification.{field} is required")

    if not isinstance(storage, Mapping):
        issues.append("storage_binding must be an object")
    else:
        if storage.get("backend") not in STORAGE_BACKENDS:
            issues.append("storage_binding.backend is invalid")
        if storage.get("encryption_profile") not in ENCRYPTION_PROFILES:
            issues.append("storage_binding.encryption_profile is invalid")
        if not isinstance(storage.get("content_ref"), str) or not storage.get("content_ref"):
            issues.append("storage_binding.content_ref is required")
        if storage.get("content_hash") != envelope.get("content_hash"):
            issues.append("storage_binding.content_hash does not match object content_hash")
        if (
            level in CLASSIFICATION_LEVELS
            and storage.get("backend") in ("local-git", "github")
            and storage.get("encryption_profile") != encryption_profile_for(level)
        ):
            issues.append("storage encryption profile does not match classification")

    if not isinstance(policy_refs, list) or any(
        not isinstance(item, str) or not item for item in (policy_refs or [])
    ):
        issues.append("policy_refs must contain non-empty strings")
    elif len(policy_refs) != len(set(policy_refs)):
        issues.append("policy_refs must not contain duplicates")

    if not isinstance(distribution, Mapping):
        issues.append("distribution_decision must be an object")
    else:
        if distribution.get("channel") not in DISTRIBUTION_CHANNELS:
            issues.append("distribution_decision.channel is invalid")
        audience = distribution.get("audience")
        if not isinstance(audience, list) or any(not isinstance(item, str) for item in audience):
            issues.append("distribution_decision.audience must be a string array")

    if version in (2, 3, 4) and level in CLASSIFICATION_LEVELS:
        try:
            expected_tier = compatibility_tier_for(
                classification_level=level,
                lifecycle=str(envelope.get("lifecycle", "")),
            )
        except ValueError as exc:
            issues.append(str(exc))
        else:
            if envelope.get("tier") != expected_tier:
                issues.append("tier is not the compatibility projection of classification and lifecycle")
    return issues
