#!/usr/bin/env python3
from __future__ import annotations

import os
import runpy
import sys
from pathlib import Path


WORKSPACE = Path(os.environ.get("ATLAS_WORKSPACE", Path.cwd())).expanduser().resolve()
os.environ["ATLAS_WORKSPACE"] = str(WORKSPACE)
os.environ["ATLAS_ENTRY_RUNTIME"] = "local"
sys.path.insert(0, str(Path(__file__).resolve().parent))
from runtime import require_product_root  # noqa: E402

try:
    product_root = require_product_root()
except RuntimeError as exc:
    print(str(exc), file=sys.stderr)
    raise SystemExit(2) from exc

from bootstrap import BootstrapError, bootstrap  # noqa: E402

try:
    installed = bootstrap(WORKSPACE, source=product_root)
except BootstrapError as exc:
    print(str(exc), file=sys.stderr)
    raise SystemExit(2) from exc
runpy.run_path(str(installed["cli_path"]), run_name="__main__")
