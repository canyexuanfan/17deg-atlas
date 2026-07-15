from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable, Mapping

from .core import KBError, PRIVATE_TIERS, KnowledgeVault, canonical_json, sha256_text, utc_now
from .cycle import KnowledgeCycle


SEARCHABLE_KINDS = ("raw", "wiki", "release")
DIRECTORY_SCOPES = ("private", "collaboration", "public")
PUBLIC_DIRECTORY_FIELDS = (
    "object_id",
    "object_kind",
    "tier",
    "title",
    "summary",
    "path",
    "locked",
)


def _json_list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, list) else []


def _event_payload(envelope: Mapping[str, Any]) -> dict[str, Any] | None:
    if envelope.get("object_kind") not in ("run", "feedback"):
        return None
    try:
        payload = json.loads(str(envelope.get("content", "")))
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


class TrustedRetrieval:
    """Rebuildable, permission-scoped local directories and SQLite FTS5 search."""

    def __init__(self, vault: KnowledgeVault):
        self.vault = vault
        self.root = Path(".local/trusted-search")

    def _path(self, relative: str) -> Path:
        return self.vault.local.resolve(self.root / relative)

    def _visible_envelopes(
        self, identities: Mapping[str, str | Path] | None
    ) -> tuple[list[dict[str, Any]], list[str]]:
        if identities:
            unlocked = self.vault.unlock_index(identities)
            documents = self.vault._visible_documents()
            tiers = ["public", "archive", *unlocked["tiers"]]
        else:
            documents = self.vault._public_documents()
            tiers = ["public", "archive"]
        unique: dict[str, dict[str, Any]] = {}
        for item in documents:
            unique[str(item["object_id"])] = dict(item)
        return list(unique.values()), tiers

    @staticmethod
    def _scope_for(envelope: Mapping[str, Any], public_ids: set[str]) -> str:
        if envelope.get("object_id") in public_ids:
            return "public"
        decision = envelope.get("distribution_decision")
        if isinstance(decision, Mapping):
            audience = [str(value) for value in _json_list(decision.get("audience"))]
            if (
                decision.get("channel") == "controlled-channel"
                and decision.get("approved_by")
                and any(value not in ("self", "public") for value in audience)
            ):
                return "collaboration"
        return "private"

    @staticmethod
    def _source_chain(
        object_id: str,
        visible: Mapping[str, Mapping[str, Any]],
        seen: set[str] | None = None,
    ) -> list[str]:
        seen = set(seen or ())
        if object_id in seen:
            return []
        seen.add(object_id)
        envelope = visible.get(object_id)
        if not envelope:
            return []
        result: list[str] = []
        for source_id in _json_list(envelope.get("source_ids")):
            if not isinstance(source_id, str) or not source_id.startswith("obj_"):
                continue
            if source_id in visible and source_id not in result:
                result.append(source_id)
            for nested in TrustedRetrieval._source_chain(source_id, visible, seen):
                if nested not in result:
                    result.append(nested)
        return result

    @staticmethod
    def _topic_names(taxonomy_path: Path) -> dict[str, str]:
        if not taxonomy_path.is_file():
            return {}
        payload = json.loads(taxonomy_path.read_text(encoding="utf-8"))
        return {
            str(item["topic_id"]): str(item["name"])
            for item in payload.get("topics", [])
            if isinstance(item, dict) and item.get("topic_id") and item.get("name")
        }

    @staticmethod
    def _knowledge_state(path: Path) -> dict[str, dict[str, Any]]:
        if not path.is_file():
            return {}
        result: dict[str, dict[str, Any]] = {}
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                item = json.loads(line)
                result[str(item["object_id"])] = item
        return result

    def _records(
        self,
        envelopes: list[dict[str, Any]],
        public_ids: set[str],
    ) -> list[dict[str, Any]]:
        visible = {str(item["object_id"]): item for item in envelopes}
        searchable = [item for item in envelopes if item.get("object_kind") in SEARCHABLE_KINDS]
        superseded_by: dict[str, list[str]] = {}
        conflicts: dict[str, list[str]] = {}
        feedback: dict[str, list[str]] = {}

        for envelope in envelopes:
            payload = _event_payload(envelope)
            if (
                payload
                and payload.get("event_type") == "knowledge-feedback"
                and envelope.get("human_confirmed")
                and envelope.get("review_state") in ("reviewed", "verified")
            ):
                target_id = str(payload.get("target_object_id", ""))
                feedback.setdefault(target_id, []).append(str(payload.get("outcome", "")))

        for envelope in searchable:
            source_id = str(envelope["object_id"])
            for relation in _json_list(envelope.get("relations")):
                if not isinstance(relation, Mapping):
                    continue
                target_id = str(relation.get("target_id", ""))
                if relation.get("type") == "supersedes" and target_id:
                    superseded_by.setdefault(target_id, []).append(source_id)
                if relation.get("type") == "contradicts" and target_id:
                    conflicts.setdefault(source_id, []).append(target_id)
                    conflicts.setdefault(target_id, []).append(source_id)

        topic_names = self._topic_names(self._path("../semantic/taxonomy.json"))
        knowledge = self._knowledge_state(self._path("../semantic/knowledge.jsonl"))
        records: list[dict[str, Any]] = []
        for envelope in searchable:
            object_id = str(envelope["object_id"])
            topic_ids = [str(value) for value in _json_list(envelope.get("topic_ids"))]
            chain = self._source_chain(object_id, visible)
            raw_ids = [
                source_id
                for source_id in chain
                if visible.get(source_id, {}).get("object_kind") == "raw"
            ]
            outcomes = feedback.get(object_id, [])
            outdated = (
                envelope.get("lifecycle") in ("superseded", "archived", "revoked")
                or envelope.get("compile_state") == "needs_recompile"
                or any(value in ("outdated", "contradicted", "failure") for value in outcomes)
            )
            state = knowledge.get(object_id, {})
            classification = envelope.get("classification")
            level = (
                str(classification.get("level"))
                if isinstance(classification, Mapping)
                else str(envelope.get("tier", ""))
            )
            records.append(
                {
                    "object_id": object_id,
                    "object_kind": envelope.get("object_kind"),
                    "tier": envelope.get("tier"),
                    "classification": level,
                    "scope": self._scope_for(envelope, public_ids),
                    "title": envelope.get("title", ""),
                    "summary": envelope.get("summary", ""),
                    "content": envelope.get("content", ""),
                    "source_uri": envelope.get("source_uri", ""),
                    "source_ids": [str(value) for value in _json_list(envelope.get("source_ids"))],
                    "source_chain": chain,
                    "raw_source_ids": raw_ids,
                    "topic_ids": topic_ids,
                    "topic_names": [topic_names[value] for value in topic_ids if value in topic_names],
                    "wiki_kind": envelope.get("wiki_kind"),
                    "card_kind": envelope.get("card_kind"),
                    "review_state": state.get(
                        "effective_review_state", envelope.get("review_state", "candidate")
                    ),
                    "lifecycle": envelope.get("lifecycle", "active"),
                    "canonical": bool(state.get("canonical", False)),
                    "superseded_by": sorted(set(superseded_by.get(object_id, []))),
                    "conflicts_with": sorted(set(conflicts.get(object_id, []))),
                    "outdated": bool(outdated),
                    "feedback_outcomes": outcomes,
                    "path": envelope.get("path", ""),
                    "updated_at": envelope.get("updated_at", ""),
                    "content_hash": envelope.get("content_hash", ""),
                    "locked": bool(envelope.get("locked", False)),
                }
            )
        records.sort(key=lambda item: (str(item["tier"]), str(item["object_id"])))
        return records

    @staticmethod
    def _directory_record(record: Mapping[str, Any], scope: str) -> dict[str, Any]:
        if scope == "public":
            return {field: record.get(field) for field in PUBLIC_DIRECTORY_FIELDS}
        return {
            key: value
            for key, value in record.items()
            if key not in ("content", "source_uri")
        }

    @staticmethod
    def _scope_records(records: list[dict[str, Any]], scope: str) -> list[dict[str, Any]]:
        allowed = {
            "private": {"private", "collaboration", "public"},
            "collaboration": {"collaboration", "public"},
            "public": {"public"},
        }[scope]
        return [item for item in records if item["scope"] in allowed]

    def _write_database(self, scope: str, records: list[dict[str, Any]]) -> dict[str, Any]:
        path = self._path(f"indexes/{scope}.sqlite3")
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            path.unlink()
        connection = sqlite3.connect(path)
        tokenizer = "trigram"
        try:
            connection.executescript(
                """
                PRAGMA journal_mode=DELETE;
                PRAGMA synchronous=FULL;
                CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE documents (
                    object_id TEXT PRIMARY KEY,
                    object_kind TEXT NOT NULL,
                    tier TEXT NOT NULL,
                    classification TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    title TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    content TEXT NOT NULL,
                    source_uri TEXT NOT NULL,
                    source_ids_json TEXT NOT NULL,
                    source_chain_json TEXT NOT NULL,
                    raw_source_ids_json TEXT NOT NULL,
                    topic_ids_text TEXT NOT NULL,
                    topic_names TEXT NOT NULL,
                    wiki_kind TEXT,
                    card_kind TEXT,
                    review_state TEXT NOT NULL,
                    lifecycle TEXT NOT NULL,
                    canonical INTEGER NOT NULL,
                    superseded_by_json TEXT NOT NULL,
                    conflicts_with_json TEXT NOT NULL,
                    outdated INTEGER NOT NULL,
                    feedback_outcomes_json TEXT NOT NULL,
                    path TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    locked INTEGER NOT NULL
                );
                """
            )
            try:
                connection.execute(
                    "CREATE VIRTUAL TABLE search_fts USING fts5(title, summary, content, topic_names, source_uri, tokenize='trigram')"
                )
            except sqlite3.OperationalError:
                tokenizer = "unicode61"
                connection.execute(
                    "CREATE VIRTUAL TABLE search_fts USING fts5(title, summary, content, topic_names, source_uri, tokenize='unicode61')"
                )
            columns = (
                "object_id", "object_kind", "tier", "classification", "scope",
                "title", "summary", "content", "source_uri", "source_ids_json",
                "source_chain_json", "raw_source_ids_json", "topic_ids_text", "topic_names",
                "wiki_kind", "card_kind", "review_state", "lifecycle", "canonical",
                "superseded_by_json", "conflicts_with_json", "outdated",
                "feedback_outcomes_json", "path", "updated_at", "content_hash", "locked",
            )
            insert_sql = (
                f"INSERT INTO documents ({','.join(columns)}) "
                f"VALUES ({','.join('?' for _ in columns)})"
            )
            for record in records:
                values = (
                        record["object_id"],
                        record["object_kind"],
                        record["tier"],
                        record["classification"],
                        record["scope"],
                        record["title"],
                        record["summary"],
                        record["content"],
                        record["source_uri"],
                        canonical_json(record["source_ids"]),
                        canonical_json(record["source_chain"]),
                        canonical_json(record["raw_source_ids"]),
                        "|" + "|".join(record["topic_ids"]) + "|",
                        " ".join(record["topic_names"]),
                        record["wiki_kind"],
                        record["card_kind"],
                        record["review_state"],
                        record["lifecycle"],
                        int(record["canonical"]),
                        canonical_json(record["superseded_by"]),
                        canonical_json(record["conflicts_with"]),
                        int(record["outdated"]),
                        canonical_json(record["feedback_outcomes"]),
                        record["path"],
                        record["updated_at"],
                        record["content_hash"],
                        int(record["locked"]),
                    )
                cursor = connection.execute(insert_sql, values)
                rowid = int(cursor.lastrowid)
                connection.execute(
                    "INSERT INTO search_fts(rowid, title, summary, content, topic_names, source_uri) VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        rowid,
                        record["title"],
                        record["summary"],
                        record["content"],
                        " ".join(record["topic_names"]),
                        record["source_uri"],
                    ),
                )
            fingerprint_rows = [
                {key: value for key, value in record.items() if key != "content"}
                for record in records
            ]
            fingerprint = sha256_text(canonical_json(fingerprint_rows))
            metadata = {
                "schema_version": "1",
                "scope": scope,
                "records": str(len(records)),
                "tokenizer": tokenizer,
                "fingerprint": fingerprint,
            }
            connection.executemany(
                "INSERT INTO metadata(key, value) VALUES (?, ?)", metadata.items()
            )
            connection.commit()
        finally:
            connection.close()
        return {
            "scope": scope,
            "records": len(records),
            "tokenizer": tokenizer,
            "fingerprint": fingerprint,
            "path": self.vault._relative(path),
        }

    def build(
        self, identities: Mapping[str, str | Path] | None = None
    ) -> dict[str, Any]:
        envelopes, tiers = self._visible_envelopes(identities)
        if identities:
            KnowledgeCycle(self.vault).rebuild_state(identities)
        public_catalog = self.vault.public_catalog_records()
        public_ids = {str(item["object_id"]) for item in public_catalog}
        records = self._records(envelopes, public_ids)
        indexes: dict[str, Any] = {}
        directories: dict[str, int] = {}
        for scope in DIRECTORY_SCOPES:
            scoped = self._scope_records(records, scope)
            directory_records = [self._directory_record(item, scope) for item in scoped]
            self.vault.local.atomic_write_text(
                self.root / f"directories/{scope}.jsonl",
                self.vault._render_jsonl(directory_records),
            )
            directories[scope] = len(directory_records)
            indexes[scope] = self._write_database(scope, scoped)
        state = {
            "schema_version": 1,
            "built_at": utc_now(),
            "authorized_tiers": tiers,
            "directories": directories,
            "indexes": indexes,
        }
        self.vault.local.atomic_write_text(
            self.root / "state.json",
            json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        )
        return {"status": "ok", **state}

    def directory(self, scope: str) -> list[dict[str, Any]]:
        if scope not in DIRECTORY_SCOPES:
            raise KBError("unsupported directory scope")
        return self.vault._load_jsonl(self._path(f"directories/{scope}.jsonl"))

    @staticmethod
    def _decode_result(row: sqlite3.Row, scope: str, match_reason: str) -> dict[str, Any]:
        return {
            "object_id": row["object_id"],
            "object_kind": row["object_kind"],
            "tier": row["tier"],
            "classification": row["classification"],
            "scope": row["scope"],
            "title": row["title"],
            "summary": row["summary"],
            "snippet": row["snippet"],
            "wiki_kind": row["wiki_kind"],
            "card_kind": row["card_kind"],
            "topic_ids": [
                value for value in row["topic_ids_text"].strip("|").split("|") if value
            ],
            "review_state": row["review_state"],
            "lifecycle": row["lifecycle"],
            "canonical": bool(row["canonical"]),
            "superseded_by": json.loads(row["superseded_by_json"]),
            "conflicts_with": json.loads(row["conflicts_with_json"]),
            "outdated": bool(row["outdated"]),
            "source_ids": json.loads(row["source_ids_json"]),
            "source_chain": json.loads(row["source_chain_json"]),
            "raw_source_ids": json.loads(row["raw_source_ids_json"]),
            "path": row["path"],
            "updated_at": row["updated_at"],
            "score": float(row["rank"]),
            "explanation": {
                "match": match_reason,
                "permission": f"prebuilt-{scope}-visible-set",
                "canonical_priority": bool(row["canonical"]),
                "status_penalty": bool(row["outdated"] or row["superseded_by_json"] != "[]"),
            },
        }

    def search(
        self,
        query: str,
        *,
        scope: str = "private",
        top_k: int = 10,
        tiers: Iterable[str] = (),
        object_kinds: Iterable[str] = (),
        wiki_kinds: Iterable[str] = (),
        card_kinds: Iterable[str] = (),
        topic_ids: Iterable[str] = (),
        review_states: Iterable[str] = (),
        lifecycles: Iterable[str] = (),
        source_ids: Iterable[str] = (),
        include_outdated: bool = True,
    ) -> list[dict[str, Any]]:
        if scope not in DIRECTORY_SCOPES:
            raise KBError("unsupported search scope")
        if top_k < 1 or top_k > 100:
            raise KBError("top_k must be between 1 and 100")
        path = self._path(f"indexes/{scope}.sqlite3")
        if not path.is_file():
            raise KBError("trusted search index is not built")
        connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        try:
            clauses: list[str] = []
            params: list[Any] = []

            def add_in(column: str, values: Iterable[str]) -> None:
                cleaned = [value for value in values if value]
                if cleaned:
                    clauses.append(f"d.{column} IN ({','.join('?' for _ in cleaned)})")
                    params.extend(cleaned)

            add_in("tier", tiers)
            add_in("object_kind", object_kinds)
            add_in("wiki_kind", wiki_kinds)
            add_in("card_kind", card_kinds)
            add_in("review_state", review_states)
            add_in("lifecycle", lifecycles)
            for topic_id in topic_ids:
                clauses.append("d.topic_ids_text LIKE ?")
                params.append(f"%|{topic_id}|%")
            for source_id in source_ids:
                clauses.append("d.source_chain_json LIKE ?")
                params.append(f'%"{source_id}"%')
            if not include_outdated:
                clauses.append("d.outdated = 0 AND d.superseded_by_json = '[]'")
            where = " AND ".join(clauses) if clauses else "1 = 1"
            needle = query.strip()
            if needle:
                phrase = '"' + needle.replace('"', '""') + '"'
                sql = f"""
                    SELECT d.*, bm25(search_fts) AS rank,
                           snippet(search_fts, 2, '[', ']', ' … ', 24) AS snippet
                    FROM search_fts
                    JOIN documents d ON d.rowid = search_fts.rowid
                    WHERE search_fts MATCH ? AND {where}
                    ORDER BY d.canonical DESC, d.outdated ASC,
                             CASE WHEN d.superseded_by_json = '[]' THEN 0 ELSE 1 END,
                             rank ASC, d.updated_at DESC, d.object_id ASC
                    LIMIT ?
                """
                rows = connection.execute(sql, [phrase, *params, top_k]).fetchall()
                match_reason = "sqlite-fts5"
                if not rows:
                    like = f"%{needle}%"
                    sql = f"""
                        SELECT d.*, 0.0 AS rank,
                               substr(d.content, 1, 160) AS snippet
                        FROM documents d
                        WHERE (d.title LIKE ? OR d.summary LIKE ? OR d.content LIKE ?
                               OR d.topic_names LIKE ? OR d.source_uri LIKE ?) AND {where}
                        ORDER BY d.canonical DESC, d.outdated ASC,
                                 CASE WHEN d.superseded_by_json = '[]' THEN 0 ELSE 1 END,
                                 d.updated_at DESC, d.object_id ASC
                        LIMIT ?
                    """
                    rows = connection.execute(
                        sql, [like, like, like, like, like, *params, top_k]
                    ).fetchall()
                    match_reason = "substring-fallback"
            else:
                sql = f"""
                    SELECT d.*, 0.0 AS rank, d.summary AS snippet
                    FROM documents d WHERE {where}
                    ORDER BY d.canonical DESC, d.outdated ASC,
                             CASE WHEN d.superseded_by_json = '[]' THEN 0 ELSE 1 END,
                             d.updated_at DESC, d.object_id ASC LIMIT ?
                """
                rows = connection.execute(sql, [*params, top_k]).fetchall()
                match_reason = "directory-filter"
            return [self._decode_result(row, scope, match_reason) for row in rows]
        except sqlite3.OperationalError as exc:
            raise KBError(f"trusted search query failed: {exc}") from exc
        finally:
            connection.close()

    def trace(self, object_id: str, *, scope: str = "private") -> dict[str, Any]:
        path = self._path(f"indexes/{scope}.sqlite3")
        if not path.is_file():
            raise KBError("trusted search index is not built")
        connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        try:
            row = connection.execute(
                "SELECT * FROM documents WHERE object_id = ?", (object_id,)
            ).fetchone()
            if row is None:
                raise KBError("object is not visible in this directory scope")
            chain_ids = json.loads(row["source_chain_json"])
            nodes = []
            for source_id in chain_ids:
                source = connection.execute(
                    "SELECT object_id, object_kind, title, tier FROM documents WHERE object_id = ?",
                    (source_id,),
                ).fetchone()
                if source is not None:
                    nodes.append(dict(source))
            return {
                "status": "ok",
                "object_id": object_id,
                "scope": scope,
                "source_chain": nodes,
                "raw_source_ids": json.loads(row["raw_source_ids_json"]),
            }
        finally:
            connection.close()

    def evaluate(self, query_set: Path, *, default_scope: str = "private") -> dict[str, Any]:
        payload = json.loads(query_set.read_text(encoding="utf-8-sig"))
        cases = payload.get("cases") if isinstance(payload, dict) else payload
        if not isinstance(cases, list) or not cases:
            raise KBError("query set requires at least one case")
        details: list[dict[str, Any]] = []
        recalls: list[float] = []
        reciprocal_ranks: list[float] = []
        for position, case in enumerate(cases, 1):
            if not isinstance(case, dict):
                raise KBError("query set cases must be objects")
            expected = [str(value) for value in case.get("expected_ids", [])]
            if not expected:
                raise KBError("each query case requires expected_ids")
            filters = case.get("filters", {})
            if not isinstance(filters, dict):
                raise KBError("query filters must be an object")
            top_k = int(case.get("top_k", 5))
            results = self.search(
                str(case.get("query", "")),
                scope=str(case.get("scope", default_scope)),
                top_k=top_k,
                tiers=filters.get("tiers", []),
                object_kinds=filters.get("object_kinds", []),
                wiki_kinds=filters.get("wiki_kinds", []),
                card_kinds=filters.get("card_kinds", []),
                topic_ids=filters.get("topic_ids", []),
                review_states=filters.get("review_states", []),
                lifecycles=filters.get("lifecycles", []),
                source_ids=filters.get("source_ids", []),
                include_outdated=bool(filters.get("include_outdated", True)),
            )
            actual = [item["object_id"] for item in results]
            hits = [object_id for object_id in expected if object_id in actual]
            recall = len(hits) / len(expected)
            ranks = [actual.index(object_id) + 1 for object_id in hits]
            reciprocal_rank = 1 / min(ranks) if ranks else 0.0
            recalls.append(recall)
            reciprocal_ranks.append(reciprocal_rank)
            details.append(
                {
                    "case": position,
                    "query": case.get("query", ""),
                    "expected_ids": expected,
                    "actual_ids": actual,
                    "hit_at_k": bool(hits),
                    "recall_at_k": recall,
                    "reciprocal_rank": reciprocal_rank,
                }
            )
        failures = [item for item in details if not item["hit_at_k"]]
        return {
            "status": "ok",
            "query_set_version": payload.get("version", 1) if isinstance(payload, dict) else 1,
            "cases": len(details),
            "hit_at_k": sum(1 for item in details if item["hit_at_k"]) / len(details),
            "recall_at_k": sum(recalls) / len(recalls),
            "mrr": sum(reciprocal_ranks) / len(reciprocal_ranks),
            "failures": failures,
            "details": details,
        }

    def verify_public_directory(self) -> dict[str, Any]:
        public = self.directory("public")
        approved = {str(item["object_id"]) for item in self.vault.public_catalog_records()}
        issues: list[str] = []
        for item in public:
            object_id = str(item.get("object_id", ""))
            if object_id not in approved:
                issues.append(f"unapproved public directory object: {object_id}")
            unknown = set(item) - set(PUBLIC_DIRECTORY_FIELDS)
            if unknown:
                issues.append(f"public directory exposes extra fields for {object_id}")
            if item.get("tier") not in ("public", "archive"):
                issues.append(f"public directory exposes private tier for {object_id}")
        return {"status": "ok", "ok": not issues, "records": len(public), "issues": issues}
