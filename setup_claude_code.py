"""Double-click me: point Claude Code at Lingling.

Kept at the repo root as a plain launcher so it can be double-clicked on Windows
without anyone needing to know where the package lives or what a module path is.
Everything real happens in ``backend/claudecode/setup_gui.py``.

Run with no arguments for the window; pass ``--help`` for the headless form.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "backend"))

from claudecode.setup_gui import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
