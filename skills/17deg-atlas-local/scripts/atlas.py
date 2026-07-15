#!/usr/bin/env python3
from __future__ import annotations

import os
import runpy
import sys
from pathlib import Path


WORKSPACE = Path(os.environ.get("ATLAS_WORKSPACE", Path.cwd())).expanduser().resolve()
os.environ["ATLAS_WORKSPACE"] = str(WORKSPACE)
os.environ["ATLAS_ENTRY_RUNTIME"] = "local"
launcher = WORKSPACE / ".17deg-atlas" / "bin" / "17deg-atlas.py"

if launcher.is_file():
    runpy.run_path(str(launcher), run_name="__main__")
else:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from runtime import require_product_root  # noqa: E402

    try:
        product_root = require_product_root()
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2) from exc
    sys.path.insert(0, str(product_root / "src"))
    from kb_vault.atlas_cli import main  # noqa: E402

    raise SystemExit(main())
