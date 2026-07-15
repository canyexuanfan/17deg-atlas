#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
MODULE_ROOT = REPOSITORY_ROOT / "modules" / "knowledge"
sys.path.insert(0, str(MODULE_ROOT / "src"))

from kb_vault.atlas_cli import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())
