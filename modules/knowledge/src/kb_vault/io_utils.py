from __future__ import annotations

import os
import stat
import time
from pathlib import Path


def atomic_replace(source: str | Path, destination: str | Path, *, attempts: int = 5) -> None:
    """Replace one file with bounded Windows sharing-violation retries."""
    source_path = Path(source)
    destination_path = Path(destination)
    last_error: OSError | None = None
    for attempt in range(max(1, attempts)):
        try:
            os.replace(source_path, destination_path)
            return
        except PermissionError as exc:
            last_error = exc
            if destination_path.exists():
                try:
                    destination_path.chmod(destination_path.stat().st_mode | stat.S_IWRITE)
                except OSError:
                    pass
            if attempt + 1 < max(1, attempts):
                time.sleep(0.05 * (attempt + 1))
    if last_error is not None:
        raise last_error
