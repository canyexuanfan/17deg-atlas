from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable, Mapping

from .core import KBError, PRIVATE_TIERS, KnowledgeVault, canonical_json, stable_token
from .model import CLASSIFICATION_RANK, classification_level_for_tier, highest_classification_level


TOPIC_STATES = ("proposed", "active", "merged", "deprecated")
TOPIC_REVIEW_DECISIONS = ("active", "merged", "deprecated", "rejected")
RELATION_REVIEW_DECISIONS = ("reviewed", "verified", "rejected")
KNOWLEDGE_REVIEW_DECISIONS = ("reviewed", "verified", "rejected")
RUN_OUTCOMES = ("success", "partial", "failure")
FEEDBACK_OUTCOMES = ("success", "failure", "partial", "outdated", "contradicted")


def _clean_strings(values: Iterable[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        cleaned = value.strip()
        if cleaned and cleaned not in result:
            result.append(cleaned)
    return result


def _event_payload(envelope: Mapping[str, Any]) -> dict[str, Any] | None:
    if envelope.get("object_kind") not in ("run", "feedback"):
        return None
    try:
        value = json.loads(str(envelope.get("content", "")))
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) and isinstance(value.get("event_type"), str) else None


class KnowledgeCycle:
    """KB2 governance and learning loop backed by immutable encrypted events."""

    def __init__(self, vault: KnowledgeVault):
        self.vault = vault

    @staticmethod
    def topic_id(name: str) -> str:
        cleaned = name.strip()
        if not cleaned:
            raise KBError("topic name is required")
        return stable_token("topic", cleaned.casefold())

    def _read_object(
        self,
        object_id: str,
        identities: Mapping[str, str | Path] | None,
    ) -> dict[str, Any]:
        return self.vault._read_object_path(self.vault._locate_object(object_id), identities)

    def _read_objects(
        self,
        object_ids: Iterable[str],
        identities: Mapping[str, str | Path] | None,
    ) -> list[dict[str, Any]]:
        return [self._read_object(object_id, identities) for object_id in _clean_strings(object_ids)]

    @staticmethod
    def _candidate_tier(objects: list[Mapping[str, Any]], fallback: str = "basic") -> str:
        inherited = (
            highest_classification_level(objects)
            if objects
            else classification_level_for_tier(fallback)
        )
        return "basic" if CLASSIFICATION_RANK[inherited] < CLASSIFICATION_RANK["basic"] else inherited

    def _accessible_envelopes(
        self, identities: Mapping[str, str | Path] | None
    ) -> Iterable[dict[str, Any]]:
        for tier in ("public", "archive", *PRIVATE_TIERS):
            if tier in PRIVATE_TIERS and not (identities or {}).get(tier):
                continue
            for path in self.vault.local.glob(f"vault/{tier}/*/*"):
                if not path.is_file() or path.suffix not in (".md", ".age", ".enc"):
                    continue
                try:
                    yield self.vault._read_object_path(path, identities)
                except KBError:
                    continue

    def propose_topic(
        self,
        *,
        request_id: str,
        name: str,
        definition: str,
        aliases: Iterable[str] = (),
        parent_ids: Iterable[str] = (),
        evidence_ids: Iterable[str] = (),
        tier: str = "basic",
        identities: Mapping[str, str | Path] | None = None,
        recipients: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        cleaned_name = name.strip()
        cleaned_definition = definition.strip()
        if not cleaned_name or not cleaned_definition:
            raise KBError("topic name and definition are required")
        cleaned_parents = _clean_strings(parent_ids)
        if any(not value.startswith("topic_") for value in cleaned_parents):
            raise KBError("parent topic ids must use stable topic_ ids")
        cleaned_evidence = _clean_strings(evidence_ids)
        evidence = self._read_objects(
            [value for value in cleaned_evidence if value.startswith("obj_")], identities
        )
        effective_tier = self._candidate_tier(evidence, fallback=tier)
        topic = {
            "topic_id": self.topic_id(cleaned_name),
            "name": cleaned_name,
            "aliases": _clean_strings(aliases),
            "definition": cleaned_definition,
            "parent_ids": cleaned_parents,
            "state": "proposed",
            "successor_ids": [],
        }
        payload = {
            "event_type": "taxonomy-proposal",
            "topic": topic,
            "evidence_ids": cleaned_evidence,
        }
        receipt = self.vault.add(
            request_id=request_id,
            tier=effective_tier,
            kind="run",
            title=f"Topic proposal: {cleaned_name}",
            summary="Reviewable taxonomy proposal",
            content=canonical_json(payload),
            source_ids=cleaned_evidence,
            rights="owned",
            maturity="draft",
            catalog_visibility="none",
            review_state="candidate",
            recipients=recipients,
            action="taxonomy-propose",
        )
        return {
            "status": "ok",
            "topic_id": topic["topic_id"],
            "proposal_object_id": receipt["object_id"],
            "review_state": "candidate",
            "active": False,
        }

    def review_topic(
        self,
        *,
        request_id: str,
        proposal_object_id: str,
        decision: str,
        human_confirmed: bool,
        successor_ids: Iterable[str] = (),
        identities: Mapping[str, str | Path] | None = None,
        recipients: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        if not human_confirmed:
            raise KBError("taxonomy review requires explicit human confirmation")
        if decision not in TOPIC_REVIEW_DECISIONS:
            raise KBError("unsupported taxonomy review decision")
        proposal = self._read_object(proposal_object_id, identities)
        payload = _event_payload(proposal)
        if not payload or payload.get("event_type") != "taxonomy-proposal":
            raise KBError("taxonomy review requires a proposal object")
        topic = payload.get("topic")
        if not isinstance(topic, dict) or not str(topic.get("topic_id", "")).startswith("topic_"):
            raise KBError("taxonomy proposal payload is invalid")
        successors = _clean_strings(successor_ids)
        if any(not value.startswith("topic_") for value in successors):
            raise KBError("successor topic ids must use stable topic_ ids")
        if decision == "merged" and not successors:
            raise KBError("merged topics require at least one successor")
        reviewed_topic = dict(topic)
        reviewed_topic["state"] = decision if decision != "rejected" else "proposed"
        reviewed_topic["successor_ids"] = successors
        review_payload = {
            "event_type": "taxonomy-review",
            "proposal_object_id": proposal_object_id,
            "decision": decision,
            "topic": reviewed_topic,
            "reviewed_by": "subject:self",
        }
        receipt = self.vault.add(
            request_id=request_id,
            tier=proposal["tier"],
            kind="run",
            title=f"Topic review: {reviewed_topic['name']}",
            summary=f"Taxonomy decision: {decision}",
            content=canonical_json(review_payload),
            source_ids=[proposal_object_id],
            rights="owned",
            maturity="verified",
            catalog_visibility="none",
            human_confirmed=True,
            review_state="verified",
            recipients=recipients,
            action="taxonomy-review",
        )
        state = self.rebuild_state(identities) if identities else None
        return {
            "status": "ok",
            "topic_id": reviewed_topic["topic_id"],
            "decision": decision,
            "review_object_id": receipt["object_id"],
            "registry_rebuilt": state is not None,
        }

    def review_relation(
        self,
        *,
        request_id: str,
        source_object_id: str,
        relation_id: str,
        decision: str,
        human_confirmed: bool,
        note: str = "",
        identities: Mapping[str, str | Path] | None = None,
        recipients: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        if not human_confirmed:
            raise KBError("relation review requires explicit human confirmation")
        if decision not in RELATION_REVIEW_DECISIONS:
            raise KBError("unsupported relation review decision")
        source = self._read_object(source_object_id, identities)
        relation = next(
            (item for item in source.get("relations", []) if item.get("relation_id") == relation_id),
            None,
        )
        if relation is None:
            raise KBError("relation does not exist on the source object")
        evidence_ids = list(relation.get("evidence_ids", []))
        if decision == "verified" and relation.get("type") == "causes" and len(evidence_ids) < 2:
            raise KBError("verified causal relations require at least two evidence objects")
        payload = {
            "event_type": "relation-review",
            "source_object_id": source_object_id,
            "relation_id": relation_id,
            "decision": decision,
            "note": note.strip(),
            "relation": relation,
            "reviewed_by": "subject:self",
        }
        review_tier = self._candidate_tier([source])
        receipt = self.vault.add(
            request_id=request_id,
            tier=review_tier,
            kind="run",
            title=f"Relation review: {relation_id}",
            summary=f"Relation decision: {decision}",
            content=canonical_json(payload),
            source_ids=[source_object_id, *evidence_ids],
            rights="owned",
            maturity="verified" if decision == "verified" else "reviewed",
            catalog_visibility="none",
            human_confirmed=True,
            review_state="verified" if decision == "verified" else "reviewed",
            recipients=recipients,
            action="relation-review",
        )
        state = self.rebuild_state(identities) if identities else None
        return {
            "status": "ok",
            "relation_id": relation_id,
            "decision": decision,
            "review_object_id": receipt["object_id"],
            "registry_rebuilt": state is not None,
        }

    def create_review_package(
        self,
        *,
        request_id: str,
        candidate_object_ids: Iterable[str],
        taxonomy_proposal_ids: Iterable[str] = (),
        identities: Mapping[str, str | Path] | None = None,
        recipients: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        candidate_ids = _clean_strings(candidate_object_ids)
        proposal_ids = _clean_strings(taxonomy_proposal_ids)
        candidates = self._read_objects(candidate_ids, identities)
        proposals = self._read_objects(proposal_ids, identities)
        if not candidates:
            raise KBError("review package requires at least one candidate")
        relations = [
            {"source_object_id": item["object_id"], **dict(relation)}
            for item in candidates
            for relation in item.get("relations", [])
        ]
        payload = {
            "event_type": "review-package",
            "candidate_object_ids": candidate_ids,
            "taxonomy_proposal_ids": proposal_ids,
            "relations": relations,
            "checks": {
                "all_candidates": all(item.get("review_state") == "candidate" for item in candidates),
                "evidence_linked": all(bool(item.get("source_ids")) for item in candidates),
                "canonical_changes": False,
            },
        }
        tier = self._candidate_tier([*candidates, *proposals])
        receipt = self.vault.add(
            request_id=request_id,
            tier=tier,
            kind="run",
            title="Knowledge review package",
            summary="Candidate knowledge, taxonomy and relation review bundle",
            content=canonical_json(payload),
            source_ids=[*candidate_ids, *proposal_ids],
            rights="owned",
            maturity="draft",
            catalog_visibility="none",
            review_state="candidate",
            recipients=recipients,
            action="review-package",
        )
        return {
            "status": "ok",
            "review_package_object_id": receipt["object_id"],
            "candidate_count": len(candidate_ids),
            "relation_count": len(relations),
            "canonical_changes": False,
        }

    def review_candidate(
        self,
        *,
        request_id: str,
        candidate_object_id: str,
        decision: str,
        human_confirmed: bool,
        note: str = "",
        identities: Mapping[str, str | Path] | None = None,
        recipients: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        if not human_confirmed:
            raise KBError("knowledge review requires explicit human confirmation")
        if decision not in KNOWLEDGE_REVIEW_DECISIONS:
            raise KBError("unsupported knowledge review decision")
        candidate = self._read_object(candidate_object_id, identities)
        if candidate.get("object_kind") != "wiki":
            raise KBError("only compiled wiki knowledge can enter canonical review")
        payload = {
            "event_type": "knowledge-review",
            "candidate_object_id": candidate_object_id,
            "decision": decision,
            "note": note.strip(),
            "reviewed_by": "subject:self",
        }
        review_tier = self._candidate_tier([candidate])
        receipt = self.vault.add(
            request_id=request_id,
            tier=review_tier,
            kind="run",
            title=f"Knowledge review: {candidate['title']}",
            summary=f"Knowledge decision: {decision}",
            content=canonical_json(payload),
            source_ids=[candidate_object_id],
            rights="owned",
            maturity="verified" if decision == "verified" else "reviewed",
            catalog_visibility="none",
            human_confirmed=True,
            review_state="verified" if decision == "verified" else "reviewed",
            recipients=recipients,
            action="knowledge-review",
        )
        state = self.rebuild_state(identities) if identities else None
        return {
            "status": "ok",
            "candidate_object_id": candidate_object_id,
            "decision": decision,
            "review_object_id": receipt["object_id"],
            "canonical": decision == "verified",
            "registry_rebuilt": state is not None,
        }

    def record_run(
        self,
        *,
        request_id: str,
        title: str,
        operation: str,
        outcome: str,
        input_object_ids: Iterable[str] = (),
        output_object_ids: Iterable[str] = (),
        notes: str = "",
        tier: str = "basic",
        identities: Mapping[str, str | Path] | None = None,
        recipients: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        if outcome not in RUN_OUTCOMES:
            raise KBError("unsupported run outcome")
        inputs = _clean_strings(input_object_ids)
        outputs = _clean_strings(output_object_ids)
        objects = self._read_objects([*inputs, *outputs], identities)
        effective_tier = self._candidate_tier(objects, fallback=tier)
        payload = {
            "event_type": "knowledge-run",
            "operation": operation.strip(),
            "outcome": outcome,
            "input_object_ids": inputs,
            "output_object_ids": outputs,
            "notes": notes.strip(),
        }
        if not payload["operation"]:
            raise KBError("run operation is required")
        receipt = self.vault.add(
            request_id=request_id,
            tier=effective_tier,
            kind="run",
            title=title.strip() or "Knowledge run",
            summary=f"Run outcome: {outcome}",
            content=canonical_json(payload),
            source_ids=[*inputs, *outputs],
            rights="owned",
            maturity="seed",
            catalog_visibility="none",
            review_state="candidate",
            recipients=recipients,
            action="run-record",
        )
        return {
            "status": "ok",
            "run_object_id": receipt["object_id"],
            "review_state": "candidate",
            "canonical": False,
        }

    def record_feedback(
        self,
        *,
        request_id: str,
        target_object_id: str,
        outcome: str,
        notes: str,
        run_object_id: str = "",
        scope_add: Iterable[str] = (),
        scope_remove: Iterable[str] = (),
        trigger_recompile: bool = False,
        human_confirmed: bool = False,
        identities: Mapping[str, str | Path] | None = None,
        recipients: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        if outcome not in FEEDBACK_OUTCOMES:
            raise KBError("unsupported feedback outcome")
        target = self._read_object(target_object_id, identities)
        source_ids = [target_object_id]
        if run_object_id:
            run = self._read_object(run_object_id, identities)
            if run.get("object_kind") != "run":
                raise KBError("feedback run reference must point to a run object")
            source_ids.append(run_object_id)
        payload = {
            "event_type": "knowledge-feedback",
            "target_object_id": target_object_id,
            "run_object_id": run_object_id or None,
            "outcome": outcome,
            "notes": notes.strip(),
            "applicability": {
                "add": _clean_strings(scope_add),
                "remove": _clean_strings(scope_remove),
            },
            "trigger_recompile": bool(trigger_recompile),
        }
        feedback_tier = self._candidate_tier([target])
        receipt = self.vault.add(
            request_id=request_id,
            tier=feedback_tier,
            kind="feedback",
            title=f"Feedback: {target['title']}",
            summary=f"Feedback outcome: {outcome}",
            content=canonical_json(payload),
            source_ids=source_ids,
            rights="owned",
            maturity="reviewed" if human_confirmed else "seed",
            catalog_visibility="none",
            human_confirmed=human_confirmed,
            review_state="reviewed" if human_confirmed else "candidate",
            recipients=recipients,
            action="feedback-record",
        )
        state = self.rebuild_state(identities) if identities else None
        return {
            "status": "ok",
            "feedback_object_id": receipt["object_id"],
            "review_state": "reviewed" if human_confirmed else "candidate",
            "recompile_queued": bool(human_confirmed and trigger_recompile),
            "state_rebuilt": state is not None,
        }

    def _raw_source_ids(
        self,
        object_id: str,
        identities: Mapping[str, str | Path] | None,
        seen: set[str] | None = None,
    ) -> list[str]:
        seen = seen or set()
        if object_id in seen:
            return []
        seen.add(object_id)
        envelope = self._read_object(object_id, identities)
        if envelope.get("object_kind") == "raw":
            return [object_id]
        result: list[str] = []
        for source_id in envelope.get("source_ids", []):
            if not isinstance(source_id, str) or not source_id.startswith("obj_"):
                continue
            try:
                nested = self._raw_source_ids(source_id, identities, seen)
            except KBError:
                continue
            for raw_id in nested:
                if raw_id not in result:
                    result.append(raw_id)
        return result

    def recompile(
        self,
        *,
        request_id: str,
        target_object_id: str,
        feedback_object_ids: Iterable[str] = (),
        summary: str = "",
        card_question: str = "",
        card_answer: str = "",
        card_kind: str = "concept",
        topic_names: Iterable[str] = (),
        identities: Mapping[str, str | Path] | None = None,
        recipients: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        target = self._read_object(target_object_id, identities)
        if target.get("object_kind") != "wiki":
            raise KBError("recompile target must be a knowledge candidate or canonical wiki object")
        feedback_ids = _clean_strings(feedback_object_ids)
        for feedback_id in feedback_ids:
            feedback = self._read_object(feedback_id, identities)
            if feedback.get("object_kind") != "feedback":
                raise KBError("recompile feedback references must point to feedback objects")
        raw_ids = self._raw_source_ids(target_object_id, identities)
        if not raw_ids:
            raise KBError("recompile target has no recoverable raw evidence")
        resolved_topics = _clean_strings(topic_names)
        if not resolved_topics and target.get("topic_ids"):
            self.rebuild_state(identities)
            taxonomy_path = self.vault.local.resolve(".local/semantic/taxonomy.json")
            taxonomy = json.loads(taxonomy_path.read_text(encoding="utf-8"))
            names_by_id = {
                item["topic_id"]: item["name"]
                for item in taxonomy.get("topics", [])
                if isinstance(item, dict) and item.get("topic_id") and item.get("name")
            }
            resolved_topics = [
                names_by_id[topic_id]
                for topic_id in target.get("topic_ids", [])
                if topic_id in names_by_id
            ]
        from .curator import KnowledgeCurator

        curated = KnowledgeCurator(self.vault).curate(
            request_id=f"{request_id}:candidates",
            raw_object_ids=raw_ids,
            summary=summary,
            card_question=card_question,
            card_answer=card_answer,
            card_kind=card_kind,
            topic_names=resolved_topics,
            identities=identities,
            recipients=recipients,
            supersedes_object_id=target_object_id,
        )
        new_ids = [curated["source_summary_id"], curated["atomic_card_id"]]
        new_ids.extend(item["object_id"] for item in curated["topic_pages"])
        payload = {
            "event_type": "recompile-completed",
            "target_object_id": target_object_id,
            "feedback_object_ids": feedback_ids,
            "raw_object_ids": raw_ids,
            "candidate_object_ids": new_ids,
            "old_object_preserved": True,
        }
        run_tier = self._candidate_tier([target])
        receipt = self.vault.add(
            request_id=f"{request_id}:run",
            tier=run_tier,
            kind="run",
            title=f"Recompile: {target['title']}",
            summary="New candidates compiled without replacing the prior object",
            content=canonical_json(payload),
            source_ids=[target_object_id, *feedback_ids, *raw_ids, *new_ids],
            rights="owned",
            maturity="draft",
            catalog_visibility="none",
            review_state="candidate",
            recipients=recipients,
            action="recompile",
        )
        state = self.rebuild_state(identities) if identities else None
        return {
            "status": "ok",
            "recompile_run_object_id": receipt["object_id"],
            "target_object_id": target_object_id,
            "candidate_object_ids": new_ids,
            "old_object_preserved": True,
            "queue_rebuilt": state is not None,
        }

    def health_check(
        self,
        *,
        request_id: str,
        identities: Mapping[str, str | Path] | None = None,
        recipients: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        """Record semantic integrity findings without changing knowledge objects."""
        envelopes = list(self._accessible_envelopes(identities))
        by_id = {item["object_id"]: item for item in envelopes}
        confirmed_feedback: dict[str, list[str]] = {}
        for envelope in envelopes:
            payload = _event_payload(envelope)
            if (
                payload
                and payload.get("event_type") == "knowledge-feedback"
                and envelope.get("human_confirmed")
                and envelope.get("review_state") in ("reviewed", "verified")
            ):
                confirmed_feedback.setdefault(str(payload.get("target_object_id", "")), []).append(
                    str(payload.get("outcome", ""))
                )

        issues: list[dict[str, Any]] = []
        relation_types: dict[tuple[str, str], set[str]] = {}
        for envelope in envelopes:
            object_id = str(envelope["object_id"])
            if envelope.get("object_kind") == "wiki":
                object_sources = [
                    value
                    for value in envelope.get("source_ids", [])
                    if isinstance(value, str) and value.startswith("obj_")
                ]
                if not object_sources:
                    issues.append({"code": "orphan-knowledge", "object_id": object_id})
                if envelope.get("compile_state") == "needs_recompile":
                    issues.append({"code": "needs-recompile", "object_id": object_id})
                if envelope.get("card_kind") == "claim" and not object_sources:
                    issues.append({"code": "claim-without-evidence", "object_id": object_id})
                outcomes = confirmed_feedback.get(object_id, [])
                if envelope.get("card_kind") == "practice" and any(
                    value in ("failure", "outdated", "contradicted") for value in outcomes
                ):
                    issues.append({"code": "practice-needs-review", "object_id": object_id})
                if envelope.get("card_kind") == "decision" and not outcomes:
                    issues.append({"code": "decision-without-retrospective", "object_id": object_id})
            for relation in envelope.get("relations", []):
                target_id = str(relation.get("target_id", ""))
                relation_type = str(relation.get("type", ""))
                relation_types.setdefault((object_id, target_id), set()).add(relation_type)

        for (source_id, target_id), types in relation_types.items():
            if {"supports", "contradicts"}.issubset(types):
                issues.append(
                    {
                        "code": "conflicting-relations",
                        "object_id": source_id,
                        "target_object_id": target_id,
                    }
                )

        issues.sort(
            key=lambda item: (
                str(item.get("code", "")),
                str(item.get("object_id", "")),
                str(item.get("target_object_id", "")),
            )
        )
        affected_ids = sorted(
            {
                value
                for issue in issues
                for value in (issue.get("object_id"), issue.get("target_object_id"))
                if isinstance(value, str) and value in by_id
            }
        )
        affected = [by_id[object_id] for object_id in affected_ids]
        payload = {
            "event_type": "semantic-health-check",
            "issue_count": len(issues),
            "issues": issues,
            "knowledge_modified": False,
        }
        receipt = self.vault.add(
            request_id=request_id,
            tier=self._candidate_tier(affected),
            kind="run",
            title="Knowledge semantic health check",
            summary=f"Semantic health findings: {len(issues)}",
            content=canonical_json(payload),
            source_ids=affected_ids,
            rights="owned",
            maturity="seed",
            catalog_visibility="none",
            review_state="candidate",
            recipients=recipients,
            action="semantic-health-check",
        )
        return {
            "status": "ok",
            "health_run_object_id": receipt["object_id"],
            "issue_count": len(issues),
            "issues": issues,
            "knowledge_modified": False,
            "canonical": False,
        }

    def rebuild_state(
        self, identities: Mapping[str, str | Path] | None
    ) -> dict[str, Any]:
        envelopes = sorted(
            self._accessible_envelopes(identities),
            key=lambda item: (str(item.get("created_at", "")), str(item.get("object_id", ""))),
        )
        proposals: dict[str, dict[str, Any]] = {}
        reviews: list[dict[str, Any]] = []
        relation_records: dict[str, dict[str, Any]] = {}
        relation_reviews: list[dict[str, Any]] = []
        knowledge_records: dict[str, dict[str, Any]] = {}
        knowledge_reviews: list[dict[str, Any]] = []
        feedback_records: list[dict[str, Any]] = []
        resolved_feedback: set[str] = set()

        for envelope in envelopes:
            if envelope.get("object_kind") == "wiki":
                knowledge_records[envelope["object_id"]] = {
                    "object_id": envelope["object_id"],
                    "wiki_kind": envelope.get("wiki_kind"),
                    "card_kind": envelope.get("card_kind"),
                    "title": envelope.get("title"),
                    "effective_review_state": envelope.get("review_state"),
                    "canonical": False,
                }
            for relation in envelope.get("relations", []):
                key = f"{envelope['object_id']}:{relation['relation_id']}"
                relation_records[key] = {
                    "source_object_id": envelope["object_id"],
                    **dict(relation),
                    "effective_review_state": relation["review_state"],
                }
            payload = _event_payload(envelope)
            if not payload:
                continue
            event_type = payload["event_type"]
            if event_type == "taxonomy-proposal":
                topic = payload.get("topic")
                if isinstance(topic, dict) and str(topic.get("topic_id", "")).startswith("topic_"):
                    proposals[topic["topic_id"]] = {**topic, "proposal_object_id": envelope["object_id"]}
            elif event_type == "taxonomy-review" and envelope.get("human_confirmed"):
                reviews.append({**payload, "review_object_id": envelope["object_id"]})
            elif event_type == "relation-review" and envelope.get("human_confirmed"):
                relation_reviews.append({**payload, "review_object_id": envelope["object_id"]})
            elif event_type == "knowledge-review" and envelope.get("human_confirmed"):
                knowledge_reviews.append({**payload, "review_object_id": envelope["object_id"]})
            elif event_type == "knowledge-feedback":
                feedback_records.append({"envelope": envelope, "payload": payload})
            elif event_type == "recompile-completed":
                resolved_feedback.update(payload.get("feedback_object_ids", []))

        registry = dict(proposals)
        decisions: list[dict[str, Any]] = []
        for review in reviews:
            topic = review.get("topic")
            if not isinstance(topic, dict):
                continue
            topic_id = str(topic.get("topic_id", ""))
            decision = str(review.get("decision", ""))
            decisions.append(review)
            if decision == "rejected":
                registry.pop(topic_id, None)
            elif decision in TOPIC_STATES:
                registry[topic_id] = {**topic, "review_object_id": review["review_object_id"]}

        for review in relation_reviews:
            key = f"{review.get('source_object_id')}:{review.get('relation_id')}"
            if key in relation_records:
                relation_records[key]["effective_review_state"] = review.get("decision")
                relation_records[key]["review_object_id"] = review["review_object_id"]

        for review in knowledge_reviews:
            object_id = str(review.get("candidate_object_id", ""))
            if object_id in knowledge_records:
                decision = str(review.get("decision", ""))
                knowledge_records[object_id]["effective_review_state"] = decision
                knowledge_records[object_id]["canonical"] = decision == "verified"
                knowledge_records[object_id]["review_object_id"] = review["review_object_id"]

        applicability: dict[str, dict[str, Any]] = {}
        queue: list[dict[str, Any]] = []
        for item in feedback_records:
            envelope = item["envelope"]
            payload = item["payload"]
            if not envelope.get("human_confirmed") or envelope.get("review_state") not in (
                "reviewed",
                "verified",
            ):
                continue
            target_id = str(payload.get("target_object_id", ""))
            scope = applicability.setdefault(target_id, {"target_object_id": target_id, "scope": []})
            changes = payload.get("applicability", {})
            if isinstance(changes, dict):
                for value in changes.get("add", []):
                    if value not in scope["scope"]:
                        scope["scope"].append(value)
                for value in changes.get("remove", []):
                    if value in scope["scope"]:
                        scope["scope"].remove(value)
            if payload.get("trigger_recompile") and envelope["object_id"] not in resolved_feedback:
                queue.append(
                    {
                        "feedback_object_id": envelope["object_id"],
                        "target_object_id": target_id,
                        "outcome": payload.get("outcome"),
                        "status": "pending",
                    }
                )

        root = Path(".local/semantic")
        taxonomy_document = {
            "schema_version": 1,
            "topics": sorted(registry.values(), key=lambda item: item["topic_id"]),
            "decisions": decisions,
        }
        self.vault.local.atomic_write_text(
            root / "taxonomy.json",
            json.dumps(taxonomy_document, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        )
        self.vault.local.atomic_write_text(
            root / "relations.jsonl", self.vault._render_jsonl(relation_records.values())
        )
        self.vault.local.atomic_write_text(
            root / "knowledge.jsonl", self.vault._render_jsonl(knowledge_records.values())
        )
        self.vault.local.atomic_write_text(
            root / "applicability.jsonl", self.vault._render_jsonl(applicability.values())
        )
        self.vault.local.atomic_write_text(root / "recompile-queue.jsonl", self.vault._render_jsonl(queue))
        return {
            "status": "ok",
            "topics": len(registry),
            "relations": len(relation_records),
            "knowledge": len(knowledge_records),
            "canonical": sum(1 for item in knowledge_records.values() if item["canonical"]),
            "applicability": len(applicability),
            "recompile_pending": len(queue),
            "root": root.as_posix(),
        }
