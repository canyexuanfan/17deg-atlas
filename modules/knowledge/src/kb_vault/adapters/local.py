from __future__ import annotations

import os
import tempfile
from pathlib import Path


class LocalAdapter:
    """Atomic local filesystem adapter rooted at one vault working copy."""

    def __init__(self, root: Path):
        self.root = root.resolve()

    def resolve(self, relative: str | Path) -> Path:
        path = (self.root / relative).resolve()
        try:
            path.relative_to(self.root)
        except ValueError as exc:
            raise ValueError(f"path escapes vault root: {relative}") from exc
        return path

    def exists(self, relative: str | Path) -> bool:
        return self.resolve(relative).exists()

    def read_bytes(self, relative: str | Path) -> bytes:
        return self.resolve(relative).read_bytes()

    def read_text(self, relative: str | Path) -> str:
        return self.resolve(relative).read_text(encoding="utf-8")

    def atomic_write_bytes(self, relative: str | Path, data: bytes) -> Path:
        target = self.resolve(relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
        )
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, target)
        except Exception:
            try:
                os.unlink(tmp_name)
            except FileNotFoundError:
                pass
            raise
        return target

    def atomic_write_text(self, relative: str | Path, text: str) -> Path:
        return self.atomic_write_bytes(relative, text.encode("utf-8"))

    def unlink(self, relative: str | Path, *, missing_ok: bool = False) -> None:
        self.resolve(relative).unlink(missing_ok=missing_ok)

    def glob(self, pattern: str) -> list[Path]:
        return sorted(self.root.glob(pattern))
