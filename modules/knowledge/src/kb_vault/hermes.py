from __future__ import annotations

from typing import Any, Mapping

from .core import KBError, KnowledgeVault


FORBIDDEN_KEY_FRAGMENTS = ("identity", "private_key", "secret_key", "token", "password")


def _reject_credentials(value: Any, path: str = "request") -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            normalized = str(key).casefold().replace("-", "_")
            if any(fragment in normalized for fragment in FORBIDDEN_KEY_FRAGMENTS):
                raise KBError(f"Hermes request contains a forbidden credential field at {path}")
            _reject_credentials(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject_credentials(child, f"{path}[{index}]")


def handle_hermes_request(
    vault: KnowledgeVault,
    request: Mapping[str, Any],
    *,
    recipients: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    _reject_credentials(request)
    if request.get("schema_version") != 1 or request.get("runtime") != "hermes":
        raise KBError("invalid Hermes request envelope")
    request_id = str(request.get("request_id", "")).strip()
    action = request.get("action")
    payload = request.get("payload")
    if not request_id or not isinstance(payload, Mapping):
        raise KBError("Hermes request_id and payload are required")
    if action == "add":
        tier = str(payload.get("tier", ""))
        if tier not in ("basic", "advanced", "core"):
            raise KBError("Hermes v0.1 may only append to encrypted tiers")
        if payload.get("human_confirmed") or payload.get("catalog_visibility") == "public":
            raise KBError("Hermes cannot confirm public catalog metadata")
        receipt = vault.add(
            request_id=request_id,
            tier=tier,
            kind=str(payload.get("kind", "raw")),
            title=str(payload.get("title", "")),
            summary=str(payload.get("summary", "")),
            content=str(payload.get("content", "")),
            source_ids=list(payload.get("source_ids", [])),
            source_uri=str(payload.get("source_uri", "")),
            rights=str(payload.get("rights", "unknown")),
            maturity=str(payload.get("maturity", "seed")),
            catalog_visibility=str(payload.get("catalog_visibility", "private")),
            human_confirmed=False,
            recipients=recipients,
        )
        return {
            "status": "ok",
            "request_id": request_id,
            "receipt_id": receipt["receipt_id"],
            "object_id": receipt.get("object_id", receipt["object_ids"][0]),
        }
    if action == "list":
        return {"status": "ok", "request_id": request_id, "results": vault.search_public("")}
    if action == "search":
        query = str(payload.get("query", ""))
        return {"status": "ok", "request_id": request_id, "results": vault.search_public(query)}
    raise KBError("unsupported Hermes action")
