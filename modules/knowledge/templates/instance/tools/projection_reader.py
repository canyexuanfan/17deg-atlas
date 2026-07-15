#!/usr/bin/env python3
"""Read-only local reader for configured content projections: view authorized content on your own machine."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


TIERS = ("basic", "advanced", "core")


class ReaderError(RuntimeError):
    pass


def sha256_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def sha256_text(value: str) -> str:
    return sha256_bytes(value.encode("utf-8"))


def parse_markdown(text: str) -> dict[str, Any]:
    if not text.startswith("---\n"):
        raise ReaderError("Markdown object is missing frontmatter")
    marker = text.find("\n---\n", 4)
    if marker < 0:
        raise ReaderError("Markdown object frontmatter is not closed")
    values: dict[str, Any] = {}
    for line in text[4:marker].splitlines():
        if ":" not in line:
            raise ReaderError("Markdown frontmatter is invalid")
        key, raw = line.split(":", 1)
        try:
            values[key.strip()] = json.loads(raw.strip())
        except json.JSONDecodeError:
            values[key.strip()] = raw.strip()
    values["content"] = text[marker + 5 :]
    return values


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise


class AtlasReader:
    def __init__(self, root: Path, age: str | Path | None = None):
        self.root = root.resolve()
        self.manifest_name, manifest = self._load_manifest()
        producers = manifest.get("producers", [])
        if not isinstance(producers, list) or len(producers) != 1:
            raise ReaderError("projection must register exactly one knowledge producer")
        producer = producers[0]
        self.knowledge_root = Path(str(producer.get("namespace", "")))
        expected_manifest = (self.knowledge_root / "knowledge-manifest.json").as_posix()
        if (
            self.knowledge_root.is_absolute()
            or ".." in self.knowledge_root.parts
            or producer.get("manifest") != expected_manifest
        ):
            raise ReaderError("knowledge producer namespace is invalid")
        explicit = Path(age).resolve() if age else None
        local = self.root / ".local" / "bin" / ("age.exe" if os.name == "nt" else "age")
        found = shutil.which("age")
        self.age = explicit if explicit and explicit.is_file() else local if local.is_file() else Path(found).resolve() if found else None

    def _load_manifest(self) -> tuple[str, dict[str, Any]]:
        for name in ("projection-manifest.json", "atlas-manifest.json"):
            path = self.root / name
            if path.is_file():
                try:
                    value = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError) as exc:
                    raise ReaderError("projection manifest is invalid") from exc
                if not str(value.get("repo_name", "")).strip():
                    raise ReaderError("projection repository name is missing")
                if not str(value.get("projection_mode", "")).strip():
                    raise ReaderError("projection mode is missing")
                return name, value
        raise ReaderError("projection-manifest.json is missing")

    def _inside(self, relative: str | Path) -> Path:
        path = (self.root / relative).resolve()
        try:
            path.relative_to(self.root)
        except ValueError as exc:
            raise ReaderError("path escapes projection root") from exc
        return path

    def verify(self) -> dict[str, Any]:
        manifest_path = self.root / self.manifest_name
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        producers = manifest.get("producers", [])
        if not any(item.get("namespace") == self.knowledge_root.as_posix() for item in producers):
            raise ReaderError("knowledge producer manifest is not registered")
        issues: list[str] = []
        expected = {item["path"]: item["sha256"] for item in manifest.get("files", [])}
        actual = {
            path.relative_to(self.root).as_posix(): path
            for path in self.root.rglob("*")
            if path.is_file()
            and ".git" not in path.relative_to(self.root).parts
            and ".local" not in path.relative_to(self.root).parts
            and path.name != self.manifest_name
        }
        for relative in sorted(set(expected).difference(actual)):
            issues.append(f"missing file: {relative}")
        for relative in sorted(set(actual).difference(expected)):
            issues.append(f"unexpected file: {relative}")
        for relative in sorted(set(actual).intersection(expected)):
            if sha256_bytes(actual[relative].read_bytes()) != expected[relative]:
                issues.append(f"hash mismatch: {relative}")
        return {"ok": not issues, "issues": issues, "files_checked": len(actual)}

    def _decrypt(self, path: Path, identity: Path) -> dict[str, Any]:
        if not self.age or not self.age.is_file():
            raise ReaderError("age executable is unavailable; install age or pass --age")
        if not identity.is_file():
            raise ReaderError("identity path does not exist")
        result = subprocess.run(
            [str(self.age), "-d", "-i", str(identity.resolve())],
            input=path.read_bytes(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if result.returncode != 0:
            raise ReaderError("identity is not authorized for the requested tier")
        try:
            envelope = json.loads(result.stdout.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ReaderError("decrypted object is invalid") from exc
        if envelope.get("content_hash") != sha256_text(str(envelope.get("content", ""))):
            raise ReaderError("decrypted object hash mismatch")
        return envelope

    def lock(self) -> dict[str, Any]:
        removed = 0
        for relative in (".local/private-index", ".local/decrypted"):
            path = self._inside(relative)
            if path.exists():
                removed += sum(1 for item in path.rglob("*") if item.is_file())
                shutil.rmtree(path)
            path.mkdir(parents=True, exist_ok=True)
        return {"status": "ok", "removed_files": removed}

    def unlock_index(self, identities: dict[str, Path]) -> dict[str, Any]:
        self.lock()
        documents: list[dict[str, Any]] = []
        unlocked: list[str] = []
        try:
            for tier in TIERS:
                identity = identities.get(tier)
                if not identity:
                    continue
                tier_files = sorted((self.root / self.knowledge_root / "encrypted" / tier).glob("**/*.age"))
                tier_documents: list[dict[str, Any]] = []
                for path in tier_files:
                    envelope = self._decrypt(path, identity)
                    if envelope.get("tier") != tier:
                        raise ReaderError("decrypted object tier does not match its path")
                    item = dict(envelope)
                    item["path"] = path.relative_to(self.root).as_posix()
                    tier_documents.append(item)
                documents.extend(tier_documents)
                unlocked.append(tier)
            documents.sort(key=lambda item: (item["tier"], item["object_id"]))
            payload = "\n".join(json.dumps(item, ensure_ascii=False, sort_keys=True) for item in documents)
            atomic_write(
                self._inside(".local/private-index/authorized.jsonl"),
                (payload + ("\n" if payload else "")).encode("utf-8"),
            )
        except Exception:
            self.lock()
            raise
        return {"status": "ok", "tiers": unlocked, "records": len(documents)}

    def _public_documents(self) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        index = self.root / self.knowledge_root / "catalog" / "index.jsonl"
        if not index.is_file():
            return records
        for line in index.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            record = json.loads(line)
            path = self._inside(record["path"])
            content = ""
            if path.is_file() and path.suffix == ".md":
                content = str(parse_markdown(path.read_text(encoding="utf-8")).get("content", ""))
            item = dict(record)
            item["content"] = content
            records.append(item)
        return records

    def _authorized_documents(self) -> list[dict[str, Any]]:
        path = self._inside(".local/private-index/authorized.jsonl")
        if not path.is_file():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]

    def search(self, query: str) -> dict[str, Any]:
        needle = query.casefold().strip()
        results: list[dict[str, Any]] = []
        by_id: dict[str, dict[str, Any]] = {}
        for item in self._public_documents() + self._authorized_documents():
            by_id[item["object_id"]] = item
        for item in sorted(by_id.values(), key=lambda value: (value.get("tier", ""), value["object_id"])):
            haystack = "\n".join(
                str(item.get(field, "")) for field in ("title", "summary", "content", "source_uri")
            ).casefold()
            if needle and needle not in haystack:
                continue
            results.append(
                {
                    "object_id": item["object_id"],
                    "tier": item.get("tier"),
                    "title": item.get("title", ""),
                    "summary": item.get("summary", ""),
                    "path": item.get("path", ""),
                }
            )
        return {"status": "ok", "results": results}


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description="Read-only local reader for configured content projection authorized content")
    value.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    value.add_argument("--age", type=Path)
    sub = value.add_subparsers(dest="command", required=True)
    unlock = sub.add_parser("unlock-index")
    for tier in TIERS:
        unlock.add_argument(f"--identity-{tier}", type=Path)
    search = sub.add_parser("search")
    search.add_argument("query")
    sub.add_parser("list")
    sub.add_parser("lock")
    sub.add_parser("verify")
    return value


def main() -> int:
    args = parser().parse_args()
    reader = AtlasReader(args.root, args.age)
    try:
        if args.command == "unlock-index":
            identities = {
                tier: value
                for tier in TIERS
                if (value := getattr(args, f"identity_{tier}")) is not None
            }
            result = reader.unlock_index(identities)
        elif args.command == "search":
            result = reader.search(args.query)
        elif args.command == "list":
            result = reader.search("")
        elif args.command == "lock":
            result = reader.lock()
        elif args.command == "verify":
            result = reader.verify()
            if not result["ok"]:
                raise ReaderError("content projection verification failed: " + "; ".join(result["issues"]))
        else:
            raise ReaderError("unsupported command")
    except (ReaderError, OSError, json.JSONDecodeError) as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
