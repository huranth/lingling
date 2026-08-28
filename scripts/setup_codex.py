"""Double-click me: point Codex at Lingling.

Kept in ``scripts/`` as a plain launcher so it can be double-clicked on Windows
without anyone needing to know where the package lives or what a module path is.
Everything real happens in ``backend/codex/setup_gui.py``.

Run with no arguments for the window; pass ``--apply`` for the headless form,
or ``--help`` for the full flag list.
"""
from __future__ import annotations
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))
from codex.setup_gui import main  # noqa: E402
if __name__ == "__main__":
    sys.exit(main())
