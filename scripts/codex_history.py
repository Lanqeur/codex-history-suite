#!/usr/bin/env python3
"""Run the bundled Codex History CLI without installing the Python package."""

from __future__ import annotations

import os
import sys
from pathlib import Path


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_ROOT / "src"))
os.environ["CODEX_HISTORY_ENTRY_SCRIPT"] = str(Path(__file__).resolve())
existing_pythonpath = os.environ.get("PYTHONPATH")
os.environ["PYTHONPATH"] = os.pathsep.join(
    value for value in (str(PLUGIN_ROOT / "src"), existing_pythonpath) if value
)

from codex_history.cli import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())
