#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


from kb_vault import (  # noqa: E402
    ContentProjection,
    GitHubRemoteInbox,
    KBError,
    KnowledgeCurator,
    KnowledgeCycle,
    KnowledgeCapabilities,
    KnowledgeVault,
    TrustedRetrieval,
    compatibility_tier_for,
)
from kb_vault.bootstrap import initialize_personal_domain, resolve_knowledge_root  # noqa: E402
from kb_vault.agent import (  # noqa: E402
    github_first_plan,
    github_first_setup,
    local_plan,
    local_setup,
    save as agent_save,
    search as agent_search,
)
from kb_vault.adapters.github_contents import GitHubContentsAdapter  # noqa: E402
from kb_vault.hermes import handle_hermes_request  # noqa: E402
from kb_vault.migration import (  # noqa: E402
    migrate_instance,
    migration_plan,
    migration_repair_plan,
    prepare_migration_source,
    record_migration_candidate,
    repair_migration,
    retire_source,
    retirement_plan,
)
from kb_vault.workspace_views import audit_workspace_views, build_workspace_views  # noqa: E402


def add_identity_args(parser: argparse.ArgumentParser) -> None:
    for tier in ("basic", "advanced", "core"):
        parser.add_argument(f"--identity-{tier}", type=Path)


def add_recipient_args(parser: argparse.ArgumentParser) -> None:
    for tier in ("basic", "advanced", "core"):
        parser.add_argument(f"--recipient-{tier}")


def add_authorship_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--origin-kind", choices=("self", "external", "mixed", "unknown"), default="unknown"
    )
    parser.add_argument(
        "--authorship-status",
        choices=("self_authored", "edited", "coauthored", "ai_assisted", "external", "unknown"),
        default="unknown",
    )
    parser.add_argument("--contributor", action="append", default=[])
    parser.add_argument("--source-ref", action="append", default=[])
    parser.add_argument("--interaction-ref", action="append", default=[])
    parser.add_argument(
        "--intended-role",
        choices=(
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
        ),
    )
    parser.add_argument(
        "--corpus-eligibility", choices=("denied", "candidate", "approved"), default="denied"
    )
    parser.add_argument(
        "--style-eligibility", choices=("denied", "candidate", "approved"), default="denied"
    )
    parser.add_argument(
        "--training-permission", choices=("denied", "undecided", "allowed"), default="denied"
    )
    parser.add_argument(
        "--clarification-status", choices=("not_needed", "required", "answered")
    )
    parser.add_argument("--clarification-ref", action="append", default=[])


def identities(args: argparse.Namespace) -> dict[str, Path]:
    return {
        tier: value
        for tier in ("basic", "advanced", "core")
        if (value := getattr(args, f"identity_{tier}", None)) is not None
    }


def recipients(args: argparse.Namespace) -> dict[str, str]:
    return {
        tier: value
        for tier in ("basic", "advanced", "core")
        if (value := getattr(args, f"recipient_{tier}", None))
    }


def write_tier(args: argparse.Namespace) -> str:
    access = getattr(args, "access", None)
    lifecycle = getattr(args, "lifecycle", "active")
    if access:
        return compatibility_tier_for(
            classification_level=access,
            lifecycle=lifecycle,
        )
    tier = getattr(args, "tier", None)
    if not tier:
        raise KBError("provide --access or legacy --tier")
    return str(tier)


def content_value(args: argparse.Namespace) -> str:
    if getattr(args, "content_file", None):
        return args.content_file.read_text(encoding="utf-8")
    value = getattr(args, "content", None)
    if value is None:
        raise KBError("provide --content-file or --content")
    return value


def json_object(value: str, label: str) -> dict[str, object]:
    try:
        result = json.loads(value)
    except json.JSONDecodeError as exc:
        raise KBError(f"{label} must be valid JSON") from exc
    if not isinstance(result, dict):
        raise KBError(f"{label} must be a JSON object")
    return result


def json_array(value: str, label: str) -> list[dict[str, object]]:
    try:
        result = json.loads(value)
    except json.JSONDecodeError as exc:
        raise KBError(f"{label} must be valid JSON") from exc
    if not isinstance(result, list):
        raise KBError(f"{label} must be a JSON array")
    if any(not isinstance(item, dict) for item in result):
        raise KBError(f"{label} must contain JSON objects")
    return result


def add_github_target_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--owner", required=True)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--branch", default="main")
    parser.add_argument("--projection-root", "--atlas-root", dest="projection_root", type=Path, required=True)
    parser.add_argument("--path", required=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Agent-ready local knowledge workspace"
    )
    default_root = Path(os.environ.get("KB_INSTANCE_ROOT", Path.cwd()))
    parser.add_argument("--root", type=Path, default=default_root)
    parser.add_argument("--age-path", type=Path, default=os.environ.get("KB_AGE_PATH"))
    sub = parser.add_subparsers(dest="command", required=True)

    agent_start_plan = sub.add_parser(
        "agent-start-plan", help="prepare the first knowledge workspace"
    )
    agent_start_plan.add_argument("--target", type=Path)
    agent_start_plan.add_argument("--runtime", choices=("auto", "local", "remote"), default="auto")
    agent_start_plan.add_argument("--repository-name")
    agent_start_plan.add_argument("--visibility", choices=("private", "public"), default="private")
    agent_start_plan.add_argument("--mode", choices=("test", "production"), default="test")

    agent_start = sub.add_parser(
        "agent-start", help="connect GitHub and prepare the first knowledge workspace"
    )
    agent_start.add_argument("--target", type=Path)
    agent_start.add_argument("--runtime", choices=("auto", "local", "remote"), default="auto")
    agent_start.add_argument("--repository-name")
    agent_start.add_argument("--visibility", choices=("private", "public"), default="private")
    agent_start.add_argument("--mode", choices=("test", "production"), default="test")
    agent_start.add_argument("--no-git", action="store_true")
    agent_start.add_argument("--no-self-test", action="store_true")
    agent_start.add_argument("--no-initial-sync", action="store_true")
    agent_start.add_argument("--confirm-github-cli-install", action="store_true")
    agent_start.add_argument("--confirm-github-login", action="store_true")
    agent_start.add_argument("--confirm-age-install", action="store_true")
    agent_start.add_argument("--confirm-repository-create", action="store_true")
    agent_start.add_argument("--confirm-existing-repository", action="store_true")
    agent_start.add_argument("--confirm-initial-sync", action="store_true")
    agent_start.add_argument("--confirm-production-key-use", action="store_true")
    agent_start.add_argument("--confirm-nonempty-directory", action="store_true")

    migration_source_parser = sub.add_parser(
        "agent-migration-source", help="prepare a local source for a verified migration"
    )
    migration_source_parser.add_argument("--repository", required=True)
    migration_source_parser.add_argument("--target", type=Path, required=True)
    migration_source_parser.add_argument(
        "--confirm-existing-repository", action="store_true"
    )

    migration_plan_parser = sub.add_parser(
        "agent-migration-plan", help="plan a verified legacy knowledge migration"
    )
    migration_plan_parser.add_argument("--source", type=Path, required=True)
    migration_plan_parser.add_argument("--target", type=Path, required=True)
    add_identity_args(migration_plan_parser)

    migration_start_parser = sub.add_parser(
        "agent-migration-start", help="migrate legacy knowledge into the current layout"
    )
    migration_start_parser.add_argument("--source", type=Path, required=True)
    migration_start_parser.add_argument("--target", type=Path, required=True)
    add_identity_args(migration_start_parser)
    migration_start_parser.add_argument("--confirm-content-migration", action="store_true")
    migration_start_parser.add_argument(
        "--confirm-local-credential-transfer", action="store_true"
    )

    migration_review_parser = sub.add_parser(
        "agent-migration-review",
        help="verify one migrated source against its raw and LLM Wiki objects",
    )
    migration_review_parser.add_argument("--target", type=Path, required=True)
    migration_review_parser.add_argument("--source-path", required=True)
    migration_review_parser.add_argument("--raw-object-id", required=True)
    migration_review_parser.add_argument("--wiki-object-id", action="append", default=[])
    migration_review_parser.add_argument("--confirm-raw-only", action="store_true")
    add_identity_args(migration_review_parser)

    migration_repair_plan_parser = sub.add_parser(
        "agent-migration-repair-plan",
        help="audit an older migration receipt before it can be trusted",
    )
    migration_repair_plan_parser.add_argument("--target", type=Path, required=True)

    migration_repair_start_parser = sub.add_parser(
        "agent-migration-repair-start",
        help="move recoverable legacy copies back into semantic review",
    )
    migration_repair_start_parser.add_argument("--target", type=Path, required=True)
    migration_repair_start_parser.add_argument(
        "--confirm-migration-state-repair", action="store_true"
    )

    retirement_plan_parser = sub.add_parser(
        "agent-retirement-plan", help="plan the disposition of a verified migration source"
    )
    retirement_plan_parser.add_argument("--source", type=Path, required=True)
    retirement_plan_parser.add_argument("--target", type=Path, required=True)

    retirement_start_parser = sub.add_parser(
        "agent-retirement-start", help="preserve, archive, or delete a migrated source"
    )
    retirement_start_parser.add_argument("--source", type=Path, required=True)
    retirement_start_parser.add_argument("--target", type=Path, required=True)
    retirement_start_parser.add_argument(
        "--action", choices=("preserve", "archive", "delete"), required=True
    )
    retirement_start_parser.add_argument("--delete-local", action="store_true")
    retirement_start_parser.add_argument("--delete-remote", action="store_true")
    retirement_start_parser.add_argument("--expected-source-root", default="")
    retirement_start_parser.add_argument("--expected-repository", default="")
    retirement_start_parser.add_argument("--confirm-archive", action="store_true")
    retirement_start_parser.add_argument("--confirm-delete-local", action="store_true")
    retirement_start_parser.add_argument("--confirm-delete-remote", action="store_true")

    local_agent_plan = sub.add_parser(
        "agent-local-plan", help="prepare a safe local Agent setup plan"
    )
    local_agent_plan.add_argument("--target", type=Path)
    local_agent_plan.add_argument("--mode", choices=("test", "production"), default="test")

    local_agent_setup = sub.add_parser(
        "agent-local-setup", help="create and verify an Agent-ready local instance"
    )
    local_agent_setup.add_argument("--target", type=Path)
    local_agent_setup.add_argument("--mode", choices=("test", "production"), default="test")
    local_agent_setup.add_argument("--no-git", action="store_true")
    local_agent_setup.add_argument("--no-self-test", action="store_true")
    local_agent_setup.add_argument("--confirm-production-key-use", action="store_true")
    local_agent_setup.add_argument("--confirm-nonempty-directory", action="store_true")

    natural_save = sub.add_parser(
        "agent-save", help="save knowledge while generating internal identifiers automatically"
    )
    natural_write_scope = natural_save.add_mutually_exclusive_group(required=True)
    natural_write_scope.add_argument("--access", choices=("public", "basic", "advanced", "core"))
    natural_write_scope.add_argument(
        "--tier",
        choices=("public", "archive", "basic", "advanced", "core"),
        help="legacy compatibility input",
    )
    natural_save.add_argument(
        "--lifecycle",
        choices=("active", "superseded", "archived", "revoked"),
        default="active",
    )
    natural_save.add_argument("--kind", choices=("raw", "wiki", "release"), default="raw")
    natural_save.add_argument("--title", required=True)
    natural_save.add_argument("--summary", default="")
    natural_save_group = natural_save.add_mutually_exclusive_group(required=True)
    natural_save_group.add_argument("--content")
    natural_save_group.add_argument("--content-file", type=Path)
    natural_save.add_argument("--confirm-public", action="store_true")
    add_authorship_args(natural_save)

    natural_search = sub.add_parser(
        "agent-search", help="search public or authorized knowledge with automatic identity discovery"
    )
    natural_search.add_argument("query")
    natural_search.add_argument("--authorized", action="store_true")
    natural_search.add_argument("--keep-unlocked", action="store_true")
    add_identity_args(natural_search)

    init_instance = sub.add_parser("init-instance", help="initialize a new local knowledge instance")
    init_instance.add_argument("--target", type=Path, required=True)

    sub.add_parser("init", help="create and validate the local directory layout")

    keys = sub.add_parser("test-keys", help="generate ignored test identities")
    keys.add_argument("--force", action="store_true")

    add = sub.add_parser("add", help="add a public or encrypted object")
    add.add_argument("--request-id", required=True)
    add_write_scope = add.add_mutually_exclusive_group(required=True)
    add_write_scope.add_argument("--access", choices=("public", "basic", "advanced", "core"))
    add_write_scope.add_argument(
        "--tier",
        choices=("public", "archive", "basic", "advanced", "core"),
        help="legacy compatibility input",
    )
    add.add_argument(
        "--kind",
        choices=("source_profile", "subscription", "capture_event", "raw", "wiki", "release", "run", "feedback"),
        default="raw",
    )
    add.add_argument("--title", required=True)
    add.add_argument("--summary", default="")
    content_group = add.add_mutually_exclusive_group(required=True)
    content_group.add_argument("--content")
    content_group.add_argument("--content-file", type=Path)
    add.add_argument("--source-id", action="append", default=[])
    add.add_argument("--source-uri", default="")
    add.add_argument("--rights", choices=("owned", "licensed", "restricted", "unknown"), default="owned")
    add.add_argument("--maturity", choices=("seed", "draft", "reviewed", "verified"), default="seed")
    add.add_argument("--lifecycle", choices=("active", "superseded", "archived", "revoked"), default="active")
    add.add_argument("--catalog-visibility", choices=("public", "private", "none"), default="private")
    add.add_argument("--human-confirmed", action="store_true")
    add.add_argument("--object-id")
    add.add_argument("--catalog-title", default="")
    add.add_argument("--catalog-summary", default="")
    add.add_argument("--media-type", choices=("article", "screenshot", "site", "data", "book", "transcript", "idea", "conversation", "file"))
    add.add_argument("--capture-purpose", choices=("trend", "deep_learning", "case", "demand", "feedback", "reference"))
    add.add_argument("--wiki-kind", choices=("source_summary", "atomic_card", "topic_page"))
    add.add_argument("--card-kind", choices=("concept", "claim", "question", "pattern", "practice", "case", "decision"))
    add.add_argument("--topic-id", action="append", default=[])
    add.add_argument("--capture-state", choices=("captured", "queued", "accepted", "rejected", "duplicate", "failed"))
    add.add_argument("--compile-state", choices=("uncompiled", "compiling", "compiled", "needs_recompile", "failed"))
    add.add_argument("--review-state", choices=("candidate", "reviewed", "verified", "rejected"), default="candidate")
    add.add_argument(
        "--distribution-channel",
        choices=("local-only", "public-plaintext", "public-ciphertext", "controlled-channel"),
    )
    add.add_argument("--distribution-audience", action="append", default=[])
    add_authorship_args(add)
    add_recipient_args(add)

    source_register = sub.add_parser("source-register", help="register a reusable knowledge source")
    source_register.add_argument("--request-id", required=True)
    source_register.add_argument("--source-kind", choices=("website", "feed", "account", "dataset", "project", "person", "conversation_channel"), required=True)
    source_register.add_argument("--name", required=True)
    source_register.add_argument("--locator", required=True)
    source_register.add_argument("--tier", choices=("public", "archive", "basic", "advanced", "core"), default="basic")
    source_register.add_argument("--rights", choices=("owned", "licensed", "restricted", "unknown"), default="unknown")
    source_register.add_argument("--trust-notes", default="")
    source_register.add_argument("--inactive", action="store_true")
    add_recipient_args(source_register)

    subscribe = sub.add_parser("subscribe", help="record why and how often to follow a source")
    subscribe.add_argument("--request-id", required=True)
    subscribe.add_argument("--source-id", required=True)
    subscribe.add_argument(
        "--capture-purpose",
        choices=("trend", "deep_learning", "case", "demand", "feedback", "reference"),
        required=True,
    )
    subscribe.add_argument(
        "--frequency", choices=("manual", "hourly", "daily", "weekly", "monthly"), default="manual"
    )
    subscribe.add_argument("--inactive", action="store_true")
    subscribe.add_argument("--notes", default="")
    add_identity_args(subscribe)
    add_recipient_args(subscribe)

    capture = sub.add_parser("capture", help="record an input decision and preserve accepted evidence")
    capture.add_argument("--request-id", required=True)
    capture.add_argument("--source-id", required=True)
    capture.add_argument("--title", required=True)
    capture_content = capture.add_mutually_exclusive_group(required=True)
    capture_content.add_argument("--content")
    capture_content.add_argument("--content-file", type=Path)
    capture.add_argument("--media-type", choices=("article", "screenshot", "site", "data", "book", "transcript", "idea", "conversation", "file"), required=True)
    capture.add_argument("--capture-purpose", choices=("trend", "deep_learning", "case", "demand", "feedback", "reference"), required=True)
    capture.add_argument("--locator", default="")
    capture.add_argument("--interaction-ref", action="append", default=[])
    capture.add_argument("--tier", choices=("public", "archive", "basic", "advanced", "core"), default="basic")
    capture.add_argument("--rights", choices=("owned", "licensed", "restricted", "unknown"), default="unknown")
    capture.add_argument("--decision", choices=("accept", "reject", "fail"), default="accept")
    capture.add_argument("--decision-reason", default="")
    add_identity_args(capture)
    add_recipient_args(capture)

    curate = sub.add_parser("curate", help="compile reviewable knowledge candidates from raw evidence")
    curate.add_argument("--request-id", required=True)
    curate.add_argument("--raw-object-id", action="append", required=True)
    curate.add_argument("--summary", default="")
    curate.add_argument("--card-question", default="")
    curate.add_argument("--card-answer", default="")
    curate.add_argument("--card-kind", choices=("concept", "claim", "question", "pattern", "practice", "case", "decision"), default="concept")
    curate.add_argument("--topic", action="append", default=[])
    add_identity_args(curate)
    add_recipient_args(curate)

    taxonomy_propose = sub.add_parser(
        "taxonomy-propose", help="create a reviewable stable-topic proposal"
    )
    taxonomy_propose.add_argument("--request-id", required=True)
    taxonomy_propose.add_argument("--name", required=True)
    taxonomy_propose.add_argument("--definition", required=True)
    taxonomy_propose.add_argument("--alias", action="append", default=[])
    taxonomy_propose.add_argument("--parent-id", action="append", default=[])
    taxonomy_propose.add_argument("--evidence-id", action="append", default=[])
    taxonomy_propose.add_argument(
        "--tier", choices=("public", "archive", "basic", "advanced", "core"), default="basic"
    )
    add_identity_args(taxonomy_propose)
    add_recipient_args(taxonomy_propose)

    taxonomy_review = sub.add_parser(
        "taxonomy-review", help="confirm or reject one taxonomy proposal"
    )
    taxonomy_review.add_argument("--request-id", required=True)
    taxonomy_review.add_argument("--proposal-object-id", required=True)
    taxonomy_review.add_argument(
        "--decision", choices=("active", "merged", "deprecated", "rejected"), required=True
    )
    taxonomy_review.add_argument("--successor-id", action="append", default=[])
    taxonomy_review.add_argument("--human-confirmed", action="store_true")
    add_identity_args(taxonomy_review)
    add_recipient_args(taxonomy_review)

    relation_review = sub.add_parser(
        "relation-review", help="review a typed relation without rewriting its source object"
    )
    relation_review.add_argument("--request-id", required=True)
    relation_review.add_argument("--source-object-id", required=True)
    relation_review.add_argument("--relation-id", required=True)
    relation_review.add_argument(
        "--decision", choices=("reviewed", "verified", "rejected"), required=True
    )
    relation_review.add_argument("--note", default="")
    relation_review.add_argument("--human-confirmed", action="store_true")
    add_identity_args(relation_review)
    add_recipient_args(relation_review)

    review_package = sub.add_parser(
        "review-package", help="build an immutable review bundle for knowledge candidates"
    )
    review_package.add_argument("--request-id", required=True)
    review_package.add_argument("--candidate-object-id", action="append", required=True)
    review_package.add_argument("--taxonomy-proposal-id", action="append", default=[])
    add_identity_args(review_package)
    add_recipient_args(review_package)

    knowledge_review = sub.add_parser(
        "knowledge-review", help="confirm or reject a compiled knowledge candidate"
    )
    knowledge_review.add_argument("--request-id", required=True)
    knowledge_review.add_argument("--candidate-object-id", required=True)
    knowledge_review.add_argument(
        "--decision", choices=("reviewed", "verified", "rejected"), required=True
    )
    knowledge_review.add_argument("--note", default="")
    knowledge_review.add_argument("--human-confirmed", action="store_true")
    add_identity_args(knowledge_review)
    add_recipient_args(knowledge_review)

    run_record = sub.add_parser("run-record", help="record a knowledge use or evaluation run")
    run_record.add_argument("--request-id", required=True)
    run_record.add_argument("--title", required=True)
    run_record.add_argument("--operation", required=True)
    run_record.add_argument("--outcome", choices=("success", "partial", "failure"), required=True)
    run_record.add_argument("--input-object-id", action="append", default=[])
    run_record.add_argument("--output-object-id", action="append", default=[])
    run_record.add_argument("--notes", default="")
    run_record.add_argument(
        "--tier", choices=("public", "archive", "basic", "advanced", "core"), default="basic"
    )
    add_identity_args(run_record)
    add_recipient_args(run_record)

    feedback_record = sub.add_parser(
        "feedback-record", help="append outcome feedback and optional recompile request"
    )
    feedback_record.add_argument("--request-id", required=True)
    feedback_record.add_argument("--target-object-id", required=True)
    feedback_record.add_argument("--run-object-id", default="")
    feedback_record.add_argument(
        "--outcome",
        choices=("success", "failure", "partial", "outdated", "contradicted"),
        required=True,
    )
    feedback_record.add_argument("--notes", default="")
    feedback_record.add_argument("--scope-add", action="append", default=[])
    feedback_record.add_argument("--scope-remove", action="append", default=[])
    feedback_record.add_argument("--trigger-recompile", action="store_true")
    feedback_record.add_argument("--human-confirmed", action="store_true")
    add_identity_args(feedback_record)
    add_recipient_args(feedback_record)

    recompile = sub.add_parser(
        "recompile", help="compile new candidates from preserved raw evidence and feedback"
    )
    recompile.add_argument("--request-id", required=True)
    recompile.add_argument("--target-object-id", required=True)
    recompile.add_argument("--feedback-object-id", action="append", default=[])
    recompile.add_argument("--summary", default="")
    recompile.add_argument("--card-question", default="")
    recompile.add_argument("--card-answer", default="")
    recompile.add_argument(
        "--card-kind",
        choices=("concept", "claim", "question", "pattern", "practice", "case", "decision"),
        default="concept",
    )
    recompile.add_argument("--topic", action="append", default=[])
    add_identity_args(recompile)
    add_recipient_args(recompile)

    semantic_rebuild = sub.add_parser(
        "semantic-rebuild", help="rebuild local taxonomy, relation, applicability and recompile views"
    )
    add_identity_args(semantic_rebuild)

    health_check = sub.add_parser(
        "health-check", help="record semantic health findings without changing knowledge"
    )
    health_check.add_argument("--request-id", required=True)
    add_identity_args(health_check)
    add_recipient_args(health_check)

    trusted_build = sub.add_parser(
        "trusted-build", help="build permission-scoped local directories and FTS5 indexes"
    )
    add_identity_args(trusted_build)

    trusted_directory = sub.add_parser(
        "trusted-directory", help="list one prebuilt trusted directory scope"
    )
    trusted_directory.add_argument("--scope", choices=("private", "collaboration", "public"), required=True)

    trusted_search = sub.add_parser(
        "trusted-search", help="search one permission-filtered SQLite FTS5 index"
    )
    trusted_search.add_argument("query")
    trusted_search.add_argument("--scope", choices=("private", "collaboration", "public"), default="private")
    trusted_search.add_argument("--top-k", type=int, default=10)
    trusted_search.add_argument("--tier", action="append", default=[])
    trusted_search.add_argument("--object-kind", action="append", default=[])
    trusted_search.add_argument("--wiki-kind", action="append", default=[])
    trusted_search.add_argument("--card-kind", action="append", default=[])
    trusted_search.add_argument("--topic-id", action="append", default=[])
    trusted_search.add_argument("--review-state", action="append", default=[])
    trusted_search.add_argument("--lifecycle", action="append", default=[])
    trusted_search.add_argument("--source-id", action="append", default=[])
    trusted_search.add_argument("--exclude-outdated", action="store_true")

    trusted_trace = sub.add_parser(
        "trusted-trace", help="trace one visible result back to its evidence chain"
    )
    trusted_trace.add_argument("object_id")
    trusted_trace.add_argument("--scope", choices=("private", "collaboration", "public"), default="private")

    trusted_evaluate = sub.add_parser(
        "trusted-evaluate", help="evaluate a trusted index with a versioned query set"
    )
    trusted_evaluate.add_argument("--query-set", type=Path, required=True)
    trusted_evaluate.add_argument("--scope", choices=("private", "collaboration", "public"), default="private")

    sub.add_parser(
        "trusted-verify-public", help="verify that the public directory contains only approved fields"
    )

    capability_propose = sub.add_parser(
        "capability-propose", help="propose an Agent capability from verified knowledge"
    )
    capability_propose.add_argument("--request-id", required=True)
    capability_propose.add_argument("--name", required=True)
    capability_propose.add_argument("--slug", required=True)
    capability_propose.add_argument("--purpose", required=True)
    capability_propose.add_argument("--trigger", action="append", required=True)
    capability_propose.add_argument("--input-contract", required=True)
    capability_propose.add_argument("--output-contract", required=True)
    capability_propose.add_argument("--knowledge-ref", action="append", required=True)
    capability_propose.add_argument("--instruction", action="append", required=True)
    capability_propose.add_argument("--constraint", action="append", default=[])
    capability_propose.add_argument("--tool-permission", action="append", default=[])
    capability_propose.add_argument("--side-effect", action="append", default=[])
    capability_propose.add_argument("--model-requirement", action="append", default=[])
    capability_propose.add_argument("--evaluation-suite", required=True)
    capability_propose.add_argument("--failure-policy", default="Stop safely and explain the unmet contract.")
    capability_propose.add_argument("--runtime-target", action="append", required=True)
    capability_propose.add_argument("--authority", default="advise")
    capability_propose.add_argument("--version", default="1.0.0")
    add_identity_args(capability_propose)
    add_recipient_args(capability_propose)

    capability_review = sub.add_parser(
        "capability-review", help="review a proposed Agent capability"
    )
    capability_review.add_argument("--request-id", required=True)
    capability_review.add_argument("--proposal-object-id", required=True)
    capability_review.add_argument(
        "--decision", choices=("reviewed", "verified", "rejected", "needs-evidence"), required=True
    )
    capability_review.add_argument("--note", default="")
    capability_review.add_argument("--human-confirmed", action="store_true")
    add_identity_args(capability_review)
    add_recipient_args(capability_review)

    capability_build = sub.add_parser(
        "capability-build", help="build a reviewed capability package"
    )
    capability_build.add_argument("--capability-id", required=True)
    capability_build.add_argument("--version")
    capability_build.add_argument("--runtime", choices=("codex", "claude", "hermes", "generic"), default="generic")
    add_identity_args(capability_build)

    capability_validate = sub.add_parser(
        "capability-validate", help="build and validate a reviewed capability package"
    )
    capability_validate.add_argument("--capability-id", required=True)
    capability_validate.add_argument("--version")
    capability_validate.add_argument("--runtime", choices=("codex", "claude", "hermes", "generic"), default="generic")
    add_identity_args(capability_validate)

    capability_preference = sub.add_parser(
        "capability-preference", help="set or show the local Skill deployment preference"
    )
    capability_preference.add_argument("--scope", choices=("project", "global", "runtime-native", "ask"))
    capability_preference.add_argument("--fallback", action="append", default=[])
    capability_preference.add_argument("--runtime", choices=("codex", "claude", "hermes", "generic"))
    capability_preference.add_argument("--auto-update", action="store_true")

    capability_materialize = sub.add_parser(
        "capability-materialize", help="deploy a reviewed Skill using an explicit local preference"
    )
    capability_materialize.add_argument("--request-id", required=True)
    capability_materialize.add_argument("--capability-id", required=True)
    capability_materialize.add_argument("--version")
    capability_materialize.add_argument("--runtime", choices=("codex", "claude", "hermes", "generic"), required=True)
    capability_materialize.add_argument("--scope", choices=("project", "global", "runtime-native", "ask"))
    capability_materialize.add_argument("--project-root", type=Path)
    capability_materialize.add_argument("--target-root", type=Path)
    capability_materialize.add_argument("--confirm-global-install", action="store_true")
    add_identity_args(capability_materialize)
    add_recipient_args(capability_materialize)

    capability_list = sub.add_parser("capability-list", help="list active capabilities")
    add_identity_args(capability_list)

    capability_resolve = sub.add_parser(
        "capability-resolve", help="resolve a natural-language goal to an active capability"
    )
    capability_resolve.add_argument("--goal", required=True)
    capability_resolve.add_argument("--runtime", choices=("codex", "claude", "hermes", "generic"), required=True)
    capability_resolve.add_argument("--available-tool", action="append", default=[])
    capability_resolve.add_argument("--authority", default="advise")
    capability_resolve.add_argument("--model", default="")
    add_identity_args(capability_resolve)

    capability_run = sub.add_parser(
        "capability-run-record", help="record one governed capability invocation"
    )
    capability_run.add_argument("--request-id", required=True)
    capability_run.add_argument("--capability-id", required=True)
    capability_run.add_argument("--version", required=True)
    capability_run.add_argument("--goal-summary", required=True)
    capability_run.add_argument("--input-summary", default="")
    capability_run.add_argument("--output-hash", required=True)
    capability_run.add_argument("--outcome", choices=("success", "partial", "failure", "refused"), required=True)
    capability_run.add_argument("--runtime", required=True)
    capability_run.add_argument("--model", required=True)
    capability_run.add_argument("--authority", default="advise")
    capability_run.add_argument("--granted-tool", action="append", default=[])
    capability_run.add_argument("--side-effect-receipt", action="append", default=[])
    add_identity_args(capability_run)
    add_recipient_args(capability_run)

    capability_feedback = sub.add_parser(
        "capability-feedback", help="record outcome feedback without overwriting the capability"
    )
    capability_feedback.add_argument("--request-id", required=True)
    capability_feedback.add_argument("--capability-id", required=True)
    capability_feedback.add_argument("--version", required=True)
    capability_feedback.add_argument("--run-object-id", required=True)
    capability_feedback.add_argument("--outcome", required=True)
    capability_feedback.add_argument("--notes", default="")
    capability_feedback.add_argument("--improvement", default="")
    capability_feedback.add_argument("--human-confirmed", action="store_true")
    add_identity_args(capability_feedback)
    add_recipient_args(capability_feedback)

    capability_recompile = sub.add_parser(
        "capability-recompile", help="create a new capability version from confirmed feedback"
    )
    capability_recompile.add_argument("--request-id", required=True)
    capability_recompile.add_argument("--capability-id", required=True)
    capability_recompile.add_argument("--version", required=True)
    capability_recompile.add_argument("--feedback-object-id", action="append", required=True)
    capability_recompile.add_argument("--human-confirmed", action="store_true")
    add_identity_args(capability_recompile)
    add_recipient_args(capability_recompile)

    capability_deprecate = sub.add_parser(
        "capability-deprecate", help="change capability lifecycle while preserving history"
    )
    capability_deprecate.add_argument("--request-id", required=True)
    capability_deprecate.add_argument("--capability-id", required=True)
    capability_deprecate.add_argument("--version", required=True)
    capability_deprecate.add_argument("--lifecycle", choices=("active", "deprecated", "revoked", "archived"), default="deprecated")
    capability_deprecate.add_argument("--note", default="")
    capability_deprecate.add_argument("--human-confirmed", action="store_true")
    add_identity_args(capability_deprecate)
    add_recipient_args(capability_deprecate)

    capability_lock = sub.add_parser(
        "capability-lock", help="remove managed capability projections and local capability views"
    )
    add_identity_args(capability_lock)

    capability_restore = sub.add_parser(
        "capability-restore", help="restore a managed Skill from its last authorized deployment record"
    )
    capability_restore.add_argument("--request-id", required=True)
    capability_restore.add_argument("--capability-id", required=True)
    capability_restore.add_argument("--version", required=True)
    capability_restore.add_argument("--runtime", choices=("codex", "claude", "hermes", "generic"), required=True)
    capability_restore.add_argument("--confirm-global-install", action="store_true")
    add_identity_args(capability_restore)
    add_recipient_args(capability_restore)

    capability_verify = sub.add_parser(
        "capability-verify", help="verify capability references, state and managed deployments"
    )
    add_identity_args(capability_verify)

    get = sub.add_parser("get", help="decrypt one object to .local/decrypted")
    get.add_argument("object_id")
    add_identity_args(get)

    move = sub.add_parser("move", help="move and re-encrypt one object")
    move.add_argument("--request-id", required=True)
    move.add_argument("--object-id", required=True)
    move.add_argument("--target-tier", choices=("public", "archive", "basic", "advanced", "core"), required=True)
    move.add_argument("--confirm", action="store_true")
    add_identity_args(move)
    add_recipient_args(move)

    release = sub.add_parser("release", help="create an independent public release")
    release.add_argument("--request-id", required=True)
    release.add_argument("--source-object-id", required=True)
    release.add_argument("--title", required=True)
    release.add_argument("--summary", default="")
    release_group = release.add_mutually_exclusive_group(required=True)
    release_group.add_argument("--content")
    release_group.add_argument("--content-file", type=Path)
    release.add_argument("--rights", choices=("owned", "licensed", "restricted", "unknown"), default="owned")
    release.add_argument("--maturity", choices=("reviewed", "verified"), default="reviewed")
    release.add_argument("--human-confirmed", action="store_true")
    add_authorship_args(release)
    add_identity_args(release)

    unlock = sub.add_parser("unlock-index", help="build a local authorized search index")
    add_identity_args(unlock)

    search = sub.add_parser("search", help="search public and currently authorized indexes")
    search.add_argument("query")

    sub.add_parser("list", help="list all currently visible objects")
    workspace_views = sub.add_parser(
        "workspace-view-build",
        help="rebuild the local human-readable raw, library, and Wiki views",
    )
    add_identity_args(workspace_views)
    sub.add_parser(
        "workspace-view-audit",
        help="check local workspace properties before applying changes",
    )
    sub.add_parser("lock", help="remove generated private indexes and decrypted views")
    sub.add_parser("reindex", help="rebuild public indexes")

    verify = sub.add_parser("verify", help="verify objects, indexes, paths, and leaks")
    add_identity_args(verify)
    verify.add_argument("--forbid", action="append", default=[])
    verify.add_argument("--no-leak-scan", action="store_true")

    doctor = sub.add_parser("doctor", help="diagnose dependencies and key configuration")
    add_identity_args(doctor)

    hermes = sub.add_parser("hermes", help="execute a credential-free Hermes request envelope")
    hermes.add_argument("--request-file", type=Path, required=True)

    atlas_select = sub.add_parser("projection-select", aliases=["atlas-select"], help="explicitly select one object for a content projection")
    atlas_select.add_argument("--object-id", required=True)
    atlas_select.add_argument(
        "--distribution",
        choices=("local-only", "projection-plaintext", "projection-ciphertext", "service-only"),
        required=True,
    )
    atlas_select.add_argument("--license-id", required=True)
    atlas_select.add_argument("--confirm", action="store_true")

    atlas_unselect = sub.add_parser("projection-unselect", aliases=["atlas-unselect"], help="remove one object from content projection")
    atlas_unselect.add_argument("--object-id", required=True)
    atlas_unselect.add_argument("--confirm", action="store_true")

    atlas_build = sub.add_parser("projection-build", aliases=["atlas-build"], help="build the configured content projection")
    atlas_build.add_argument("--output", type=Path, required=True)

    atlas_verify = sub.add_parser("projection-verify", aliases=["atlas-verify"], help="verify a configured content projection")
    atlas_verify.add_argument("--output", type=Path, required=True)

    github_plan = sub.add_parser("github-plan", help="plan one verified projection Contents API write")
    add_github_target_args(github_plan)
    github_plan.add_argument("--message", required=True)
    github_plan.add_argument("--sha")

    github_check = sub.add_parser(
        "github-check", help="read one remote content projection file and compare it without printing content"
    )
    add_github_target_args(github_check)

    github_put = sub.add_parser(
        "github-put", help="write one verified content projection file through the GitHub Contents API"
    )
    add_github_target_args(github_put)
    github_put.add_argument("--message", required=True)
    github_put.add_argument("--sha")
    github_put.add_argument("--confirm-remote-write", action="store_true")

    github_connect = sub.add_parser(
        "github-connect", help="verify Token access to the allowlisted content projection repository"
    )
    github_connect.add_argument("--owner", required=True)
    github_connect.add_argument("--repo", required=True)
    github_connect.add_argument("--branch", default="main")
    github_connect.add_argument("--path", default="README.md")

    github_inbox = sub.add_parser(
        "github-inbox-add", help="encrypt if needed and append one new remote content projection inbox event"
    )
    github_inbox.add_argument("--owner", required=True)
    github_inbox.add_argument("--repo", required=True)
    github_inbox.add_argument("--branch", default="main")
    github_inbox.add_argument("--request-id", required=True)
    github_inbox.add_argument("--agent-id", required=True)
    github_inbox.add_argument("--object-id")
    github_inbox.add_argument(
        "--tier", choices=("public", "archive", "basic", "advanced", "core"), required=True
    )
    github_inbox.add_argument("--kind", choices=("raw", "wiki", "release"), default="raw")
    github_inbox.add_argument("--title", required=True)
    github_inbox.add_argument("--summary", default="")
    github_inbox.add_argument("--source-uri", default="")
    github_inbox.add_argument("--content-file", type=Path, required=True)
    github_inbox.add_argument("--human-confirmed", action="store_true")
    github_inbox.add_argument(
        "--distribution-channel",
        choices=("local-only", "public-plaintext", "public-ciphertext", "controlled-channel"),
    )
    github_inbox.add_argument("--distribution-audience", action="append", default=[])
    github_inbox.add_argument("--confirm-remote-write", action="store_true")
    return parser


def execute(args: argparse.Namespace) -> object:
    args.command = {
        "atlas-select": "projection-select",
        "atlas-unselect": "projection-unselect",
        "atlas-build": "projection-build",
        "atlas-verify": "projection-verify",
    }.get(args.command, args.command)
    entry_runtime = os.environ.get("ATLAS_ENTRY_RUNTIME", "").strip().lower()
    if entry_runtime == "remote" and args.command not in (
        "agent-start-plan",
        "agent-start",
    ):
        raise KBError("remote Atlas entry only supports onboarding")
    if args.command == "agent-start-plan":
        return github_first_plan(
            args.target,
            runtime=args.runtime,
            repository_name=args.repository_name,
            visibility=args.visibility,
            mode=args.mode,
            age_path=args.age_path,
        )
    if args.command == "agent-start":
        return github_first_setup(
            args.target,
            runtime=args.runtime,
            repository_name=args.repository_name,
            visibility=args.visibility,
            mode=args.mode,
            age_path=args.age_path,
            initialize_git=not args.no_git,
            run_self_test=not args.no_self_test,
            run_initial_sync=not args.no_initial_sync,
            confirm_github_cli_install=args.confirm_github_cli_install,
            confirm_github_login=args.confirm_github_login,
            confirm_age_install=args.confirm_age_install,
            confirm_repository_create=args.confirm_repository_create,
            confirm_existing_repository=args.confirm_existing_repository,
            confirm_initial_sync=args.confirm_initial_sync,
            confirm_production_key_use=args.confirm_production_key_use,
            confirm_nonempty_directory=args.confirm_nonempty_directory,
        )
    if args.command == "agent-migration-plan":
        return migration_plan(args.source, args.target, identities=identities(args))
    if args.command == "agent-migration-source":
        return prepare_migration_source(
            args.repository,
            args.target,
            confirm_existing_repository=args.confirm_existing_repository,
        )
    if args.command == "agent-migration-start":
        return migrate_instance(
            args.source,
            args.target,
            age_path=args.age_path,
            confirm_content_migration=args.confirm_content_migration,
            confirm_local_credential_transfer=args.confirm_local_credential_transfer,
            identities=identities(args),
        )
    if args.command == "agent-migration-review":
        return record_migration_candidate(
            args.target,
            source_path=args.source_path,
            raw_object_id=args.raw_object_id,
            wiki_object_ids=args.wiki_object_id,
            identities=identities(args),
            confirm_raw_only=args.confirm_raw_only,
            age_path=args.age_path,
        )
    if args.command == "agent-migration-repair-plan":
        return migration_repair_plan(args.target)
    if args.command == "agent-migration-repair-start":
        return repair_migration(
            args.target,
            confirm_migration_state_repair=args.confirm_migration_state_repair,
        )
    if args.command == "agent-retirement-plan":
        return retirement_plan(args.source, args.target)
    if args.command == "agent-retirement-start":
        return retire_source(
            args.source,
            args.target,
            action=args.action,
            delete_local=args.delete_local,
            delete_remote=args.delete_remote,
            expected_source_root=args.expected_source_root,
            expected_repository=args.expected_repository,
            confirm_archive=args.confirm_archive,
            confirm_delete_local=args.confirm_delete_local,
            confirm_delete_remote=args.confirm_delete_remote,
        )
    if args.command == "agent-local-plan":
        return local_plan(args.target, mode=args.mode, age_path=args.age_path)
    if args.command == "agent-local-setup":
        return local_setup(
            args.target,
            mode=args.mode,
            age_path=args.age_path,
            initialize_git=not args.no_git,
            run_self_test=not args.no_self_test,
            confirm_production_key_use=args.confirm_production_key_use,
            confirm_nonempty_directory=args.confirm_nonempty_directory,
        )
    if args.command == "init-instance":
        return initialize_personal_domain(args.target)
    vault = KnowledgeVault(resolve_knowledge_root(args.root), age_path=args.age_path)
    if args.command == "workspace-view-build":
        return build_workspace_views(
            vault,
            args.root,
            identities=identities(args),
        )
    if args.command == "workspace-view-audit":
        return audit_workspace_views(args.root)
    if args.command == "agent-save":
        return agent_save(
            vault,
            tier=write_tier(args),
            lifecycle=args.lifecycle,
            kind=args.kind,
            title=args.title,
            summary=args.summary,
            content=content_value(args),
            confirm_public=args.confirm_public,
            origin_kind=args.origin_kind,
            authorship_status=args.authorship_status,
            contributors=tuple(args.contributor),
            source_refs=tuple(args.source_ref),
            interaction_refs=tuple(args.interaction_ref),
            intended_role=args.intended_role or "unknown",
            corpus_eligibility=args.corpus_eligibility,
            style_eligibility=args.style_eligibility,
            training_permission=args.training_permission,
            clarification_status=args.clarification_status,
            clarification_refs=tuple(args.clarification_ref),
        )
    if args.command == "agent-search":
        return agent_search(
            vault,
            args.query,
            authorized=args.authorized,
            keep_unlocked=args.keep_unlocked,
            identities=identities(args),
        )
    if args.command == "init":
        return vault.init_layout()
    if args.command == "test-keys":
        return {"status": "ok", "recipients": vault.generate_test_keys(force=args.force)}
    if args.command == "add":
        return vault.add(
            request_id=args.request_id,
            tier=write_tier(args),
            kind=args.kind,
            title=args.title,
            summary=args.summary,
            content=content_value(args),
            source_ids=args.source_id,
            source_uri=args.source_uri,
            rights=args.rights,
            maturity=args.maturity,
            lifecycle=args.lifecycle,
            catalog_visibility=args.catalog_visibility,
            human_confirmed=args.human_confirmed,
            object_id=args.object_id,
            catalog_title=args.catalog_title,
            catalog_summary=args.catalog_summary,
            recipients=recipients(args),
            media_type=args.media_type,
            capture_purpose=args.capture_purpose,
            wiki_kind=args.wiki_kind,
            card_kind=args.card_kind,
            topic_ids=args.topic_id,
            capture_state=args.capture_state,
            compile_state=args.compile_state,
            review_state=args.review_state,
            distribution_channel=args.distribution_channel,
            distribution_audience=args.distribution_audience,
            origin_kind=args.origin_kind,
            authorship_status=args.authorship_status,
            contributors=args.contributor,
            source_refs=args.source_ref,
            interaction_refs=args.interaction_ref,
            intended_role=args.intended_role or "unknown",
            corpus_eligibility=args.corpus_eligibility,
            style_eligibility=args.style_eligibility,
            training_permission=args.training_permission,
            clarification_status=args.clarification_status,
            clarification_refs=args.clarification_ref,
        )
    if args.command == "source-register":
        return KnowledgeCurator(vault).register_source(
            request_id=args.request_id,
            source_kind=args.source_kind,
            name=args.name,
            locator=args.locator,
            tier=args.tier,
            rights_default=args.rights,
            trust_notes=args.trust_notes,
            active=not args.inactive,
            recipients=recipients(args),
        )
    if args.command == "subscribe":
        return KnowledgeCurator(vault).subscribe(
            request_id=args.request_id,
            source_id=args.source_id,
            capture_purpose=args.capture_purpose,
            frequency=args.frequency,
            active=not args.inactive,
            notes=args.notes,
            identities=identities(args),
            recipients=recipients(args),
        )
    if args.command == "capture":
        return KnowledgeCurator(vault).capture(
            request_id=args.request_id,
            source_id=args.source_id,
            title=args.title,
            content=content_value(args),
            media_type=args.media_type,
            capture_purpose=args.capture_purpose,
            locator=args.locator,
            interaction_refs=args.interaction_ref,
            tier=args.tier,
            rights=args.rights,
            decision=args.decision,
            decision_reason=args.decision_reason,
            identities=identities(args),
            recipients=recipients(args),
        )
    if args.command == "curate":
        return KnowledgeCurator(vault).curate(
            request_id=args.request_id,
            raw_object_ids=args.raw_object_id,
            summary=args.summary,
            card_question=args.card_question,
            card_answer=args.card_answer,
            card_kind=args.card_kind,
            topic_names=args.topic,
            identities=identities(args),
            recipients=recipients(args),
        )
    if args.command == "taxonomy-propose":
        return KnowledgeCycle(vault).propose_topic(
            request_id=args.request_id,
            name=args.name,
            definition=args.definition,
            aliases=args.alias,
            parent_ids=args.parent_id,
            evidence_ids=args.evidence_id,
            tier=args.tier,
            identities=identities(args),
            recipients=recipients(args),
        )
    if args.command == "taxonomy-review":
        return KnowledgeCycle(vault).review_topic(
            request_id=args.request_id,
            proposal_object_id=args.proposal_object_id,
            decision=args.decision,
            human_confirmed=args.human_confirmed,
            successor_ids=args.successor_id,
            identities=identities(args),
            recipients=recipients(args),
        )
    if args.command == "relation-review":
        return KnowledgeCycle(vault).review_relation(
            request_id=args.request_id,
            source_object_id=args.source_object_id,
            relation_id=args.relation_id,
            decision=args.decision,
            human_confirmed=args.human_confirmed,
            note=args.note,
            identities=identities(args),
            recipients=recipients(args),
        )
    if args.command == "review-package":
        return KnowledgeCycle(vault).create_review_package(
            request_id=args.request_id,
            candidate_object_ids=args.candidate_object_id,
            taxonomy_proposal_ids=args.taxonomy_proposal_id,
            identities=identities(args),
            recipients=recipients(args),
        )
    if args.command == "knowledge-review":
        return KnowledgeCycle(vault).review_candidate(
            request_id=args.request_id,
            candidate_object_id=args.candidate_object_id,
            decision=args.decision,
            human_confirmed=args.human_confirmed,
            note=args.note,
            identities=identities(args),
            recipients=recipients(args),
        )
    if args.command == "run-record":
        return KnowledgeCycle(vault).record_run(
            request_id=args.request_id,
            title=args.title,
            operation=args.operation,
            outcome=args.outcome,
            input_object_ids=args.input_object_id,
            output_object_ids=args.output_object_id,
            notes=args.notes,
            tier=args.tier,
            identities=identities(args),
            recipients=recipients(args),
        )
    if args.command == "feedback-record":
        return KnowledgeCycle(vault).record_feedback(
            request_id=args.request_id,
            target_object_id=args.target_object_id,
            run_object_id=args.run_object_id,
            outcome=args.outcome,
            notes=args.notes,
            scope_add=args.scope_add,
            scope_remove=args.scope_remove,
            trigger_recompile=args.trigger_recompile,
            human_confirmed=args.human_confirmed,
            identities=identities(args),
            recipients=recipients(args),
        )
    if args.command == "recompile":
        return KnowledgeCycle(vault).recompile(
            request_id=args.request_id,
            target_object_id=args.target_object_id,
            feedback_object_ids=args.feedback_object_id,
            summary=args.summary,
            card_question=args.card_question,
            card_answer=args.card_answer,
            card_kind=args.card_kind,
            topic_names=args.topic,
            identities=identities(args),
            recipients=recipients(args),
        )
    if args.command == "semantic-rebuild":
        return KnowledgeCycle(vault).rebuild_state(identities(args))
    if args.command == "health-check":
        return KnowledgeCycle(vault).health_check(
            request_id=args.request_id,
            identities=identities(args),
            recipients=recipients(args),
        )
    if args.command == "trusted-build":
        return TrustedRetrieval(vault).build(identities(args))
    if args.command == "trusted-directory":
        return {
            "status": "ok",
            "scope": args.scope,
            "results": TrustedRetrieval(vault).directory(args.scope),
        }
    if args.command == "trusted-search":
        return {
            "status": "ok",
            "scope": args.scope,
            "results": TrustedRetrieval(vault).search(
                args.query,
                scope=args.scope,
                top_k=args.top_k,
                tiers=args.tier,
                object_kinds=args.object_kind,
                wiki_kinds=args.wiki_kind,
                card_kinds=args.card_kind,
                topic_ids=args.topic_id,
                review_states=args.review_state,
                lifecycles=args.lifecycle,
                source_ids=args.source_id,
                include_outdated=not args.exclude_outdated,
            ),
        }
    if args.command == "trusted-trace":
        return TrustedRetrieval(vault).trace(args.object_id, scope=args.scope)
    if args.command == "trusted-evaluate":
        return TrustedRetrieval(vault).evaluate(args.query_set, default_scope=args.scope)
    if args.command == "trusted-verify-public":
        result = TrustedRetrieval(vault).verify_public_directory()
        if not result["ok"]:
            raise KBError("public directory verification failed: " + "; ".join(result["issues"]))
        return result
    capabilities = KnowledgeCapabilities(vault)
    if args.command == "capability-propose":
        return capabilities.propose(
            request_id=args.request_id,
            name=args.name,
            slug=args.slug,
            purpose=args.purpose,
            triggers=args.trigger,
            input_contract=json_object(args.input_contract, "input contract"),
            output_contract=json_object(args.output_contract, "output contract"),
            knowledge_refs=args.knowledge_ref,
            instructions=args.instruction,
            constraints=args.constraint,
            tool_permissions=args.tool_permission,
            side_effects=args.side_effect,
            model_requirements=args.model_requirement,
            evaluation_suite=json_array(args.evaluation_suite, "evaluation suite"),
            failure_policy=args.failure_policy,
            runtime_targets=args.runtime_target,
            authority_required=args.authority,
            version=args.version,
            identities=identities(args),
            recipients=recipients(args),
        )
    if args.command == "capability-review":
        return capabilities.review(
            request_id=args.request_id,
            proposal_object_id=args.proposal_object_id,
            decision=args.decision,
            human_confirmed=args.human_confirmed,
            note=args.note,
            identities=identities(args),
            recipients=recipients(args),
        )
    if args.command in ("capability-build", "capability-validate"):
        return capabilities.build(
            capability_id=args.capability_id,
            version=args.version,
            runtime=args.runtime,
            identities=identities(args),
        )
    if args.command == "capability-preference":
        if args.scope is None:
            return {"status": "ok", "preference": capabilities.preference()}
        return capabilities.set_preference(
            default_scope=args.scope,
            fallback_order=args.fallback,
            runtime=args.runtime,
            auto_update=args.auto_update,
        )
    if args.command == "capability-materialize":
        return capabilities.materialize(
            request_id=args.request_id,
            capability_id=args.capability_id,
            version=args.version,
            runtime=args.runtime,
            scope=args.scope,
            project_root=args.project_root,
            target_root=args.target_root,
            confirm_global_install=args.confirm_global_install,
            identities=identities(args),
            recipients=recipients(args),
        )
    if args.command == "capability-list":
        return {"status": "ok", "capabilities": capabilities.list_active(identities(args))}
    if args.command == "capability-resolve":
        return capabilities.resolve(
            goal=args.goal,
            runtime=args.runtime,
            available_tools=args.available_tool,
            authority=args.authority,
            model=args.model,
            identities=identities(args),
        )
    if args.command == "capability-run-record":
        return capabilities.record_run(
            request_id=args.request_id,
            capability_id=args.capability_id,
            version=args.version,
            goal_summary=args.goal_summary,
            input_summary=args.input_summary,
            output_hash=args.output_hash,
            outcome=args.outcome,
            runtime=args.runtime,
            model=args.model,
            authority=args.authority,
            granted_tools=args.granted_tool,
            side_effect_receipts=args.side_effect_receipt,
            identities=identities(args),
            recipients=recipients(args),
        )
    if args.command == "capability-feedback":
        return capabilities.feedback(
            request_id=args.request_id,
            capability_id=args.capability_id,
            version=args.version,
            run_object_id=args.run_object_id,
            outcome=args.outcome,
            notes=args.notes,
            improvement=args.improvement,
            human_confirmed=args.human_confirmed,
            identities=identities(args),
            recipients=recipients(args),
        )
    if args.command == "capability-recompile":
        return capabilities.recompile(
            request_id=args.request_id,
            capability_id=args.capability_id,
            version=args.version,
            feedback_object_ids=args.feedback_object_id,
            human_confirmed=args.human_confirmed,
            identities=identities(args),
            recipients=recipients(args),
        )
    if args.command == "capability-deprecate":
        return capabilities.set_lifecycle(
            request_id=args.request_id,
            capability_id=args.capability_id,
            version=args.version,
            lifecycle=args.lifecycle,
            note=args.note,
            human_confirmed=args.human_confirmed,
            identities=identities(args),
            recipients=recipients(args),
        )
    if args.command == "capability-lock":
        return capabilities.lock(identities(args))
    if args.command == "capability-restore":
        return capabilities.restore_deployment(
            request_id=args.request_id,
            capability_id=args.capability_id,
            version=args.version,
            runtime=args.runtime,
            confirm_global_install=args.confirm_global_install,
            identities=identities(args),
            recipients=recipients(args),
        )
    if args.command == "capability-verify":
        result = capabilities.verify(identities(args))
        if not result["ok"]:
            raise KBError("capability verification failed: " + "; ".join(result["issues"]))
        return result
    if args.command == "get":
        return vault.get(args.object_id, identities=identities(args))
    if args.command == "move":
        return vault.move(
            request_id=args.request_id,
            object_id=args.object_id,
            target_tier=args.target_tier,
            identities=identities(args),
            recipients=recipients(args),
            confirm=args.confirm,
        )
    if args.command == "release":
        return vault.release(
            request_id=args.request_id,
            source_object_id=args.source_object_id,
            title=args.title,
            summary=args.summary,
            content=content_value(args),
            identities=identities(args),
            rights=args.rights,
            maturity=args.maturity,
            human_confirmed=args.human_confirmed,
            origin_kind=None if args.origin_kind == "unknown" else args.origin_kind,
            authorship_status=(
                None if args.authorship_status == "unknown" else args.authorship_status
            ),
            contributors=args.contributor,
            source_refs=args.source_ref,
        )
    if args.command == "unlock-index":
        return vault.unlock_index(identities(args))
    if args.command == "search":
        return {"status": "ok", "results": vault.search(args.query)}
    if args.command == "list":
        return {"status": "ok", "results": vault.list_visible()}
    if args.command == "lock":
        return vault.lock()
    if args.command == "reindex":
        return vault.reindex()
    if args.command == "verify":
        result = vault.verify(
            identities=identities(args),
            forbidden_terms=args.forbid,
            leak_scan=not args.no_leak_scan,
        )
        if not result["ok"]:
            raise KBError("verification failed: " + "; ".join(result["issues"]))
        return result
    if args.command == "doctor":
        return vault.doctor(identities(args))
    if args.command == "hermes":
        request = json.loads(args.request_file.read_text(encoding="utf-8"))
        return handle_hermes_request(vault, request)
    if args.command == "projection-select":
        return ContentProjection(vault).select(
            args.object_id,
            args.distribution,
            args.license_id,
            confirm=args.confirm,
        )
    if args.command == "projection-unselect":
        return ContentProjection(vault).unselect(args.object_id, confirm=args.confirm)
    if args.command == "projection-build":
        return ContentProjection(vault).build(args.output)
    if args.command == "projection-verify":
        result = ContentProjection(vault).verify(args.output)
        if not result["ok"]:
            raise KBError("projection verification failed: " + "; ".join(result["issues"]))
        return result
    if args.command == "github-plan":
        projection = ContentProjection(vault)
        projection.assert_remote_target(owner=args.owner, repo=args.repo, branch=args.branch)
        content = projection.verified_file(args.projection_root, args.path)
        adapter = GitHubContentsAdapter(owner=args.owner, repo=args.repo)
        plan = adapter.plan_put(
            path=args.path,
            content=content,
            message=args.message,
            branch=args.branch,
            sha=args.sha,
        )
        return {"status": "ok", "request": plan.sanitized()}
    if args.command == "github-check":
        projection = ContentProjection(vault)
        projection.assert_remote_target(owner=args.owner, repo=args.repo, branch=args.branch)
        local_content = projection.verified_file(args.projection_root, args.path)
        adapter = GitHubContentsAdapter(owner=args.owner, repo=args.repo)
        remote = adapter.get_file(path=args.path, ref=args.branch)
        return {
            "status": "ok",
            "path": args.path,
            "branch": args.branch,
            "remote_sha": remote["sha"],
            "matches_local": remote["content"] == local_content,
        }
    if args.command == "github-put":
        if not args.confirm_remote_write:
            raise KBError("use --confirm-remote-write to authorize remote writes")
        projection = ContentProjection(vault)
        projection.assert_remote_target(owner=args.owner, repo=args.repo, branch=args.branch)
        content = projection.verified_file(args.projection_root, args.path)
        adapter = GitHubContentsAdapter(owner=args.owner, repo=args.repo)
        return adapter.put_file(
            path=args.path,
            content=content,
            message=args.message,
            branch=args.branch,
            sha=args.sha,
        )
    if args.command == "github-connect":
        projection = ContentProjection(vault)
        projection.assert_remote_target(owner=args.owner, repo=args.repo, branch=args.branch)
        adapter = GitHubContentsAdapter(owner=args.owner, repo=args.repo)
        return GitHubRemoteInbox(adapter, namespace=projection.namespace.as_posix()).check(
            path=args.path, ref=args.branch
        )
    if args.command == "github-inbox-add":
        if not args.confirm_remote_write:
            raise KBError("real GitHub write requires --confirm-remote-write")
        projection = ContentProjection(vault)
        projection.assert_remote_target(owner=args.owner, repo=args.repo, branch=args.branch)
        try:
            content = args.content_file.read_bytes()
        except OSError as exc:
            raise KBError("content file cannot be read") from exc
        recipient = None
        if args.tier in ("basic", "advanced", "core"):
            recipient_env = vault.tiers_config["tiers"][args.tier]["recipient_env"]
            recipient = os.environ.get(recipient_env, "")
            if not recipient:
                raise KBError(f"recipient environment variable {recipient_env} is required for this tier")
        adapter = GitHubContentsAdapter(owner=args.owner, repo=args.repo)
        return GitHubRemoteInbox(adapter, namespace=projection.namespace.as_posix()).add(
            branch=args.branch,
            confirm_remote_write=True,
            request_id=args.request_id,
            agent_id=args.agent_id,
            object_id=args.object_id,
            tier=args.tier,
            kind=args.kind,
            title=args.title,
            summary=args.summary,
            source_uri=args.source_uri,
            content=content,
            human_confirmed=args.human_confirmed,
            distribution_channel=args.distribution_channel,
            distribution_audience=args.distribution_audience,
            recipient=recipient,
            encrypt=vault.encrypt_bytes,
        )
    raise KBError("unsupported command")


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="strict")
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        result = execute(args)
    except KBError as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
