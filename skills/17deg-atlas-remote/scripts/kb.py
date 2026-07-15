#!/usr/bin/env python3
from __future__ import annotations

import os
import sys
from pathlib import Path


os.environ.setdefault("ATLAS_WORKSPACE", str(Path.cwd().resolve()))
os.environ["ATLAS_ENTRY_RUNTIME"] = "remote"
sys.path.insert(0, str(Path(__file__).resolve().parent))
from runtime import require_product_root  # noqa: E402


try:
    PRODUCT_ROOT = require_product_root()
except RuntimeError as exc:
    print(str(exc), file=sys.stderr)
    raise SystemExit(2) from exc
sys.path.insert(0, str(PRODUCT_ROOT / "src"))

from kb_vault.cli import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())
