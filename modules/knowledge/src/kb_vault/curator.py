from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable, Mapping

from .core import KBError, PRIVATE_TIERS, KnowledgeVault, canonical_json, sha256_text, stable_token
from .compiler_quality import normalize_topic_names, validate_generated_text
from .model import CLASSIFICATION_RANK, highest_classification_level


SOURCE_KINDS = ("website", "feed", "account", "dataset", "project", "person", "conversation_channel")
CAPTURE_DECISIONS = ("accept", "reject", "fail")
SUBSCRIPTION_FREQUENCIES = ("manual", "hourly", "daily", "weekly", "monthly")


class KnowledgeCurator:
    """Deterministic KB2 candidate pipeline; it never promotes or publishes knowledge."""

    def __init__(self, vault: KnowledgeVault):
        self.vault = vault

    @staticmethod
    def _candidate_tier(requested: str) -> str:
        return "basic" if requested in ("public", "archive") else requested

    @staticmethod
    def _source_id(name: str, locator: str) -> str:
        return stable_token("src", f"{name.strip()}:{locator.strip()}")

    @staticmethod
    def _event_id(request_id: str) -> str:
        return stable_token("evt", request_id)

    def register_source(
        self,
        *,
        request_id: str,
        source_kind: str,
        name: str,
        locator: str,
        tier: str = "basic",
        rights_default: str = "unknown",
        trust_notes: str = "",
        active: bool = True,
        recipients: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        if source_kind not in SOURCE_KINDS:
            raise KBError("unsupported source kind")
        if not name.strip() or not locator.strip():
            raise KBError("source name and locator are required")
        source_id = self._source_id(name, locator)
        payload = {
            "source_id": source_id,
            "source_kind": source_kind,
            "name": name.strip(),
            "locator": locator.strip(),
            "rights_default": rights_default,
            "trust_notes": trust_notes.strip(),
            "active": bool(active),
        }
        effective_tier = self._candidate_tier(tier)
        receipt = self.vault.add(
            request_id=request_id,
            tier=effective_tier,
            kind="source_profile",
            title=name.strip(),
            summary=f"{source_kind} source profile",
            content=canonical_json(payload),
            rights=rights_default,
            maturity="seed",
            catalog_visibility="none",
            review_state="candidate",
            recipients=recipients,
            action="source-register",
        )
        return {
            "status": "ok",
            "source_id": source_id,
            "object_id": receipt["object_id"],
            "tier": effective_tier,
            "review_state": "candidate",
            "idempotent_replay": bool(receipt.get("idempotent_replay", False)),
        }

    def _accessible_envelopes(
        self, identities: Mapping[str, str | Path] | None = None
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

    def _raw_with_hash(
        self,
        content_hash: str,
        identities: Mapping[str, str | Path] | None,
    ) -> dict[str, Any] | None:
        for envelope in self._accessible_envelopes(identities):
            if envelope["object_kind"] == "raw" and envelope["content_hash"] == content_hash:
                return envelope
        return None

    def _source_profile(
        self,
        source_id: str,
        identities: Mapping[str, str | Path] | None,
    ) -> dict[str, Any]:
        for envelope in self._accessible_envelopes(identities):
            if envelope.get("object_kind") != "source_profile":
                continue
            try:
                payload = json.loads(str(envelope.get("content", "")))
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict) and payload.get("source_id") == source_id:
                return envelope
        raise KBError("source id is not registered or not accessible")

    def subscribe(
        self,
        *,
        request_id: str,
        source_id: str,
        capture_purpose: str,
        frequency: str = "manual",
        active: bool = True,
        notes: str = "",
        identities: Mapping[str, str | Path] | None = None,
        recipients: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        if frequency not in SUBSCRIPTION_FREQUENCIES:
            raise KBError("unsupported subscription frequency")
        source = self._source_profile(source_id, identities)
        subscription_id = stable_token(
            "sub", f"{source_id}:{capture_purpose}:{frequency}"
        )
        payload = {
            "subscription_id": subscription_id,
            "source_id": source_id,
            "capture_purpose": capture_purpose,
            "frequency": frequency,
            "active": bool(active),
            "notes": notes.strip(),
        }
        receipt = self.vault.add(
            request_id=request_id,
            tier=source["tier"],
            kind="subscription",
            title=f"Subscription {subscription_id}",
            summary=f"{frequency} source subscription",
            content=canonical_json(payload),
            source_ids=[source_id, source["object_id"]],
            rights=source["rights"],
            maturity="seed",
            catalog_visibility="none",
            capture_purpose=capture_purpose,
            review_state="candidate",
            recipients=recipients,
            action="subscribe",
        )
        return {
            "status": "ok",
            "subscription_id": subscription_id,
            "subscription_object_id": receipt["object_id"],
            "source_id": source_id,
            "active": bool(active),
            "review_state": "candidate",
        }

    def capture(
        self,
        *,
        request_id: str,
        source_id: str,
        title: str,
        content: str,
        media_type: str,
        capture_purpose: str,
        locator: str = "",
        interaction_refs: Iterable[str] = (),
        tier: str = "basic",
        rights: str = "unknown",
        decision: str = "accept",
        decision_reason: str = "",
        identities: Mapping[str, str | Path] | None = None,
        recipients: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        if not source_id.startswith("src_"):
            raise KBError("invalid source id")
        self._source_profile(source_id, identities)
        if decision not in CAPTURE_DECISIONS:
            raise KBError("unsupported capture decision")
        if not content:
            decision = "fail"
            decision_reason = decision_reason or "empty-input"
        effective_tier = self._candidate_tier(tier)
        content_hash = sha256_text(content)
        duplicate = self._raw_with_hash(content_hash, identities)
        if duplicate is not None:
            state = "duplicate"
            raw_object_id = duplicate["object_id"]
        elif decision == "accept":
            state = "accepted"
            raw_request_id = f"{request_id}:raw"
            raw_object_id = stable_token("obj", f"{raw_request_id}:raw")
        elif decision == "reject":
            state = "rejected"
            raw_object_id = None
        else:
            state = "failed"
            raw_object_id = None

        event_id = self._event_id(request_id)
        event_request_id = f"{request_id}:capture"
        event_object_id = stable_token("obj", f"{event_request_id}:capture_event")

        raw_receipt: dict[str, Any] | None = None
        if state == "accepted":
            raw_receipt = self.vault.add(
                request_id=f"{request_id}:raw",
                object_id=raw_object_id,
                tier=effective_tier,
                kind="raw",
                title=title.strip() or "Untitled capture",
                summary="",
                content=content,
                source_ids=[source_id, event_id],
                source_uri=locator,
                rights=rights,
                maturity="seed",
                catalog_visibility="none",
                media_type=media_type,
                capture_purpose=capture_purpose,
                capture_state="accepted",
                compile_state="uncompiled",
                review_state="candidate",
                origin_kind="external",
                authorship_status="external",
                source_refs=[source_id, *([locator] if locator else [])],
                interaction_refs=list(interaction_refs),
                intended_role="evidence",
                recipients=recipients,
                action="capture-raw",
            )

        event_payload = {
            "event_id": event_id,
            "source_id": source_id,
            "capture_purpose": capture_purpose,
            "locator": locator,
            "capture_state": state,
            "candidate_raw_id": raw_object_id,
            "content_hash": content_hash,
            "decision_reason": decision_reason,
        }
        event_receipt = self.vault.add(
            request_id=event_request_id,
            object_id=event_object_id,
            tier=effective_tier,
            kind="capture_event",
            title=f"Capture {event_id}",
            summary=f"{state} capture receipt",
            content=canonical_json(event_payload),
            source_ids=[source_id],
            source_uri=locator,
            rights=rights,
            maturity="seed",
            catalog_visibility="none",
            media_type=media_type,
            capture_purpose=capture_purpose,
            capture_state=state,
            review_state="candidate",
            recipients=recipients,
            action="capture-event",
        )
        return {
            "status": "ok",
            "event_id": event_id,
            "capture_event_object_id": event_receipt["object_id"],
            "capture_state": state,
            "raw_object_id": raw_object_id if state in ("accepted", "duplicate") else None,
            "raw_created": raw_receipt is not None,
            "tier": effective_tier,
        }

    def _read_raws(
        self,
        raw_object_ids: Iterable[str],
        identities: Mapping[str, str | Path] | None,
    ) -> list[dict[str, Any]]:
        raws: list[dict[str, Any]] = []
        for object_id in raw_object_ids:
            path = self.vault._locate_object(object_id)
            envelope = self.vault._read_object_path(path, identities)
            if envelope["object_kind"] != "raw":
                raise KBError("curation sources must be raw objects")
            raws.append(envelope)
        if not raws:
            raise KBError("at least one raw object is required")
        return raws

    @staticmethod
    def _derived_rights(objects: Iterable[Mapping[str, Any]]) -> str:
        rights = {str(item.get("rights", "unknown")) for item in objects}
        if len(rights) == 1:
            return next(iter(rights))
        if "restricted" in rights:
            return "restricted"
        if "unknown" in rights:
            return "unknown"
        # Combining owned and licensed material needs a fresh rights review.
        return "restricted"

    def _latest_topic_page(
        self,
        topic_id: str,
        classification: str,
        identities: Mapping[str, str | Path] | None,
    ) -> dict[str, Any] | None:
        pages = [
            item
            for item in self._accessible_envelopes(identities)
            if item.get("object_kind") == "wiki"
            and item.get("wiki_kind") == "topic_page"
            and topic_id in item.get("topic_ids", [])
            and item.get("lifecycle") not in ("revoked",)
            and (
                item.get("classification", {}).get("level")
                if isinstance(item.get("classification"), Mapping)
                else item.get("tier")
            )
            == classification
        ]
        if not pages:
            return None
        return max(
            pages,
            key=lambda item: (str(item.get("updated_at", "")), str(item.get("object_id", ""))),
        )

    @staticmethod
    def _summary_text(raws: list[Mapping[str, Any]], supplied: str) -> str:
        if supplied.strip():
            return supplied.strip()
        paragraphs: list[str] = []
        for raw in raws:
            for paragraph in str(raw.get("content", "")).splitlines():
                paragraph = paragraph.strip()
                if paragraph:
                    paragraphs.append(paragraph)
                if len(paragraphs) >= 2:
                    break
            if len(paragraphs) >= 2:
                break
        summary = " ".join(paragraphs)
        return summary[:600] if summary else "该来源尚未形成可用摘要。"

    @staticmethod
    def _relation(
        *, relation_seed: str, relation_type: str, target_id: str, statement: str, evidence_ids: list[str]
    ) -> dict[str, Any]:
        return {
            "relation_id": stable_token("rel", relation_seed),
            "type": relation_type,
            "target_id": target_id,
            "statement": statement,
            "evidence_ids": evidence_ids,
            "review_state": "candidate",
            "created_by": "agent",
        }

    def curate(
        self,
        *,
        request_id: str,
        raw_object_ids: Iterable[str],
        summary: str = "",
        card_question: str = "",
        card_answer: str = "",
        card_kind: str = "concept",
        topic_names: Iterable[str] = (),
        identities: Mapping[str, str | Path] | None = None,
        recipients: Mapping[str, str] | None = None,
        supersedes_object_id: str = "",
    ) -> dict[str, Any]:
        raws = self._read_raws(raw_object_ids, identities)
        inherited = highest_classification_level(raws)
        candidate_tier = "basic" if CLASSIFICATION_RANK[inherited] < CLASSIFICATION_RANK["basic"] else inherited
        raw_ids = [item["object_id"] for item in raws]
        title = str(raws[0]["title"])
        summary_text = self._summary_text(raws, summary)
        summary_text = validate_generated_text(
            summary_text,
            label="Wiki source summary",
            minimum_alnum=12,
        )
        raw_origins = {str(item.get("origin_kind", "unknown")) for item in raws}
        compiled_origin = next(iter(raw_origins)) if len(raw_origins) == 1 else "mixed"
        compiled_rights = self._derived_rights(raws)
        compiled_clarification = (
            "required"
            if any(
                item.get("clarification_status") == "required"
                or item.get("rights") == "unknown"
                for item in raws
            )
            else "answered"
            if all(item.get("clarification_status") == "answered" for item in raws)
            else "not_needed"
        )
        topics = normalize_topic_names(topic_names)
        topic_ids = [stable_token("topic", name.casefold()) for name in topics]
        existing_topic_pages = {
            topic_id: self._latest_topic_page(topic_id, candidate_tier, identities)
            for topic_id in topic_ids
        }
        superseded: dict[str, Any] | None = None
        if supersedes_object_id:
            superseded = self.vault._read_object_path(
                self.vault._locate_object(supersedes_object_id), identities
            )
            if superseded.get("object_kind") != "wiki":
                raise KBError("superseded object must be a wiki knowledge object")

        summary_request = f"{request_id}:summary"
        summary_object_id = stable_token("obj", f"{summary_request}:wiki")
        summary_relations = [
            self._relation(
                relation_seed=f"{summary_object_id}:derived:{raw_id}",
                relation_type="derived_from",
                target_id=raw_id,
                statement="该摘要候选由此原始证据提炼，尚待审核。",
                evidence_ids=[raw_id],
            )
            for raw_id in raw_ids
        ]
        if superseded and superseded.get("wiki_kind") == "source_summary":
            summary_relations.append(
                self._relation(
                    relation_seed=f"{summary_object_id}:supersedes:{supersedes_object_id}",
                    relation_type="supersedes",
                    target_id=supersedes_object_id,
                    statement="该重新编译候选拟替代旧摘要；旧对象继续保留，尚待审核。",
                    evidence_ids=raw_ids,
                )
            )
        summary_receipt = self.vault.add(
            request_id=summary_request,
            object_id=summary_object_id,
            tier=candidate_tier,
            kind="wiki",
            title=f"{title}：来源摘要候选",
            summary=summary_text,
            content=summary_text,
            source_ids=raw_ids,
            rights=compiled_rights,
            maturity="draft",
            catalog_visibility="none",
            wiki_kind="source_summary",
            topic_ids=topic_ids,
            relations=summary_relations,
            compile_state="compiled",
            review_state="candidate",
            origin_kind=compiled_origin,
            authorship_status="ai_assisted",
            source_refs=raw_ids,
            intended_role="knowledge",
            clarification_status=compiled_clarification,
            recipients=recipients,
            action="curate-summary",
        )

        question = card_question.strip() or f"这份材料对“{title}”提供了什么可复用认识？"
        answer = card_answer.strip() or summary_text
        if not question.endswith(("？", "?")):
            question += "？"
        question = validate_generated_text(
            question,
            label="Wiki atomic-card question",
            minimum_alnum=4,
        )
        answer = validate_generated_text(
            answer,
            label="Wiki atomic-card answer",
            minimum_alnum=8,
        )
        card_request = f"{request_id}:card"
        card_object_id = stable_token("obj", f"{card_request}:wiki")
        card_relations = [self._relation(
            relation_seed=f"{card_object_id}:derived:{summary_object_id}",
            relation_type="derived_from",
            target_id=summary_object_id,
            statement="该原子卡片候选由来源摘要提炼，需结合原始证据审核。",
            evidence_ids=raw_ids,
        )]
        if superseded and superseded.get("wiki_kind") == "atomic_card":
            card_relations.append(
                self._relation(
                    relation_seed=f"{card_object_id}:supersedes:{supersedes_object_id}",
                    relation_type="supersedes",
                    target_id=supersedes_object_id,
                    statement="该重新编译候选拟替代旧卡片；旧对象继续保留，尚待审核。",
                    evidence_ids=raw_ids,
                )
            )
        card_receipt = self.vault.add(
            request_id=card_request,
            object_id=card_object_id,
            tier=candidate_tier,
            kind="wiki",
            title=question,
            summary=answer[:240],
            content=f"## 问题\n\n{question}\n\n## 候选回答\n\n{answer}",
            source_ids=[summary_object_id, *raw_ids],
            rights=compiled_rights,
            maturity="draft",
            catalog_visibility="none",
            wiki_kind="atomic_card",
            card_kind=card_kind,
            topic_ids=topic_ids,
            relations=card_relations,
            compile_state="compiled",
            review_state="candidate",
            origin_kind=compiled_origin,
            authorship_status="ai_assisted",
            source_refs=raw_ids,
            intended_role="knowledge",
            clarification_status=compiled_clarification,
            recipients=recipients,
            action="curate-card",
        )

        topic_pages: list[dict[str, str]] = []
        for index, (topic_name, topic_id) in enumerate(zip(topics, topic_ids, strict=True)):
            topic_request = f"{request_id}:topic:{index}"
            topic_object_id = stable_token("obj", f"{topic_request}:wiki")
            previous_topic = existing_topic_pages.get(topic_id)
            previous_card_ids: list[str] = []
            if previous_topic:
                previous_card_ids = [
                    str(relation.get("target_id"))
                    for relation in previous_topic.get("relations", [])
                    if relation.get("type") == "supports"
                    and str(relation.get("target_id", "")).startswith("obj_")
                ]
            card_ids = list(dict.fromkeys([*previous_card_ids, card_object_id]))
            topic_sources = list(
                dict.fromkeys(
                    [
                        *card_ids,
                        summary_object_id,
                        *raw_ids,
                        *(
                            [str(previous_topic["object_id"])]
                            if previous_topic
                            else []
                        ),
                    ]
                )
            )
            topic_raw_refs = list(
                dict.fromkeys(
                    [
                        *(
                            [str(value) for value in previous_topic.get("source_refs", [])]
                            if previous_topic
                            else []
                        ),
                        *raw_ids,
                    ]
                )
            )
            topic_relations = [
                self._relation(
                    relation_seed=f"{topic_object_id}:includes:{candidate_card_id}",
                    relation_type="supports",
                    target_id=candidate_card_id,
                    statement="该卡片是此主题页的候选组成部分，关系尚待审核。",
                    evidence_ids=topic_raw_refs,
                )
                for candidate_card_id in card_ids
            ]
            replaced_topic = previous_topic or (
                superseded if superseded and superseded.get("wiki_kind") == "topic_page" else None
            )
            if replaced_topic:
                topic_relations.append(
                    self._relation(
                        relation_seed=f"{topic_object_id}:supersedes:{replaced_topic['object_id']}",
                        relation_type="supersedes",
                        target_id=str(replaced_topic["object_id"]),
                        statement="该聚合候选拟替代旧主题页投影；旧对象继续保留，尚待审核。",
                        evidence_ids=topic_raw_refs,
                    )
                )
            topic_objects: list[Mapping[str, Any]] = [*raws]
            if previous_topic:
                topic_objects.append(previous_topic)
            topic_tier = highest_classification_level(topic_objects)
            if CLASSIFICATION_RANK[topic_tier] < CLASSIFICATION_RANK["basic"]:
                topic_tier = "basic"
            topic_rights = self._derived_rights(topic_objects)
            topic_origins = {
                str(item.get("origin_kind", "unknown")) for item in topic_objects
            }
            topic_origin = next(iter(topic_origins)) if len(topic_origins) == 1 else "mixed"
            topic_clarification = (
                "required"
                if any(
                    item.get("clarification_status") == "required"
                    or item.get("rights") == "unknown"
                    for item in topic_objects
                )
                else "answered"
                if all(item.get("clarification_status") == "answered" for item in topic_objects)
                else "not_needed"
            )
            topic_content = "# " + topic_name + "\n\n候选卡片：\n" + "\n".join(
                f"- {candidate_card_id}" for candidate_card_id in card_ids
            )
            topic_receipt = self.vault.add(
                request_id=topic_request,
                object_id=topic_object_id,
                tier=topic_tier,
                kind="wiki",
                title=f"{topic_name}：主题页候选",
                summary=f"自动聚合 {len(card_ids)} 张候选卡片的主题入口，尚未成为正式 taxonomy。",
                content=topic_content,
                source_ids=topic_sources,
                rights=topic_rights,
                maturity="draft",
                catalog_visibility="none",
                wiki_kind="topic_page",
                topic_ids=[topic_id],
                relations=topic_relations,
                compile_state="compiled",
                review_state="candidate",
                origin_kind=topic_origin,
                authorship_status="ai_assisted",
                source_refs=topic_raw_refs,
                intended_role="knowledge",
                clarification_status=topic_clarification,
                recipients=recipients,
                action="curate-topic",
            )
            topic_pages.append(
                {
                    "topic_id": topic_id,
                    "object_id": topic_receipt["object_id"],
                    "supersedes_object_id": (
                        str(replaced_topic["object_id"]) if replaced_topic else ""
                    ),
                }
            )

        from .cycle import KnowledgeCycle

        cycle = KnowledgeCycle(self.vault)
        taxonomy_proposals: list[dict[str, str]] = []
        for index, (topic_name, topic_page) in enumerate(zip(topics, topic_pages, strict=True)):
            if existing_topic_pages.get(topic_page["topic_id"]):
                continue
            proposal = cycle.propose_topic(
                request_id=f"{request_id}:taxonomy:{index}",
                name=topic_name,
                definition=f"围绕“{topic_name}”组织经来源支持的知识对象。",
                evidence_ids=[topic_page["object_id"], card_receipt["object_id"], *raw_ids],
                tier=candidate_tier,
                identities=identities,
                recipients=recipients,
            )
            taxonomy_proposals.append(
                {
                    "topic_id": proposal["topic_id"],
                    "proposal_object_id": proposal["proposal_object_id"],
                }
            )

        candidate_ids = [summary_receipt["object_id"], card_receipt["object_id"]]
        candidate_ids.extend(item["object_id"] for item in topic_pages)
        review_package = cycle.create_review_package(
            request_id=f"{request_id}:review-package",
            candidate_object_ids=candidate_ids,
            taxonomy_proposal_ids=[item["proposal_object_id"] for item in taxonomy_proposals],
            identities=identities,
            recipients=recipients,
        )

        return {
            "status": "ok",
            "classification": candidate_tier,
            "review_state": "candidate",
            "canonical": False,
            "source_summary_id": summary_receipt["object_id"],
            "atomic_card_id": card_receipt["object_id"],
            "topic_pages": topic_pages,
            "taxonomy_proposals": taxonomy_proposals,
            "review_package_object_id": review_package["review_package_object_id"],
        }
