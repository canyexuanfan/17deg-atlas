#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path


PRODUCT_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(PRODUCT_ROOT / "src"))

from kb_vault.cli import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())
