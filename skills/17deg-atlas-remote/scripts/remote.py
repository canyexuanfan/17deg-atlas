#!/usr/bin/env python3
from __future__ import annotations

import runpy
import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parent))
from runtime import require_product_root  # noqa: E402


try:
    PRODUCT_ROOT = require_product_root()
except RuntimeError as exc:
    print(str(exc), file=sys.stderr)
    raise SystemExit(2) from exc
TARGET = PRODUCT_ROOT / "skills" / "github-api-file-pusher" / "scripts" / "remote.py"


if __name__ == "__main__":
    runpy.run_path(str(TARGET), run_name="__main__")
