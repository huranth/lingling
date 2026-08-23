"""Write the model catalog Codex needs, so its effort setting reaches the model.

    python tools/codex_catalog.py

Dumps the installed Codex binary's own catalog for a template, reads Lingling's
live free models, and writes ``~/.codex/lingling_models.json``. By default it
also wires the file into ``~/.codex/config.toml`` (applying the
``model_catalog_json`` line there) and opens the API-key setup window; pass
``--no-write-config`` to just print the config line, or ``--no-key-setup`` to
suppress the window (useful for scripted runs).

Codex only accepts a filesystem path for ``model_catalog_json`` -- a URL is
treated as a path and fails ("The filename, directory name, or volume label
syntax is incorrect") -- so this writes a file instead of Lingling serving one.
Re-run it after a Codex upgrade or when OpenCode publishes new free models.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import config  # noqa: E402
from models import codex_catalog  # noqa: E402
from models.catalog import UnifiedCatalog  # noqa: E402
from providers import registry  # noqa: E402

DEFAULT_OUT = Path.home() / ".codex" / "lingling_models.json"


def _run_codex(argv: list, binary: str):
    """Run ``codex debug models``, surviving bare-name launch on Windows.

    npm installs codex as a ``codex.cmd``/``codex.ps1`` wrapper. ``subprocess``
    with a bare ``["codex", ...]`` resolves that to a ``.CMD`` and then lets
    ``CreateProcess`` try to execute it directly, which fails with
    ``FileNotFoundError`` even though ``codex --version`` works in a shell. A
    full path that keeps the explicit ``.cmd`` extension is handled correctly, so
    retry with the ``shutil.which`` result. A genuinely-missing binary still gets
    a clear error instead of a raw traceback.
    """
    try:
        return subprocess.run(argv, capture_output=True)
    except FileNotFoundError:
        resolved = shutil.which(binary)
        if not resolved:
            raise SystemExit(
                f"codex binary not found on PATH: {binary!r}. "
                "Install it or pass --codex <path to the codex executable>."
            )
        return subprocess.run([resolved] + argv[1:], capture_output=True)


def dump_codex_catalog(binary: str, bundled: bool = False) -> list:
    """Return the model list from ``codex debug models``.

    The output carries a UTF-8 BOM, hence ``utf-8-sig``.

    ``bundled`` adds ``--bundled``, which makes Codex ignore both the network
    refresh *and* any ``model_catalog_json`` override. That matters because once
    the generated file is wired into ``config.toml``, a plain ``debug models``
    returns *this file* -- so a second run would clone Lingling's own entry
    instead of a real Codex model, and any drift would compound on every
    regeneration. The caller uses the refreshed dump when it contains real Codex
    models and falls back to the bundled one when it does not.
    """
    argv = [binary, "debug", "models"] + (["--bundled"] if bundled else [])
    proc = _run_codex(argv, binary)
    if proc.returncode != 0:
        raise SystemExit(
            f"`{' '.join(argv[1:])}` failed ({proc.returncode}): "
            + proc.stderr.decode("utf-8", "replace").strip()[:400]
        )
    payload = json.loads(proc.stdout.decode("utf-8-sig", "replace"))
    models = payload.get("models") or []
    if not models:
        raise SystemExit("Codex reported an empty catalog; nothing to use as a template.")
    return models


def pick_template(models: list, slug: str | None) -> dict:
    """Choose which real Codex model to clone.

    Default is the highest-priority model that still speaks the classic Responses
    shape. Codex's newest models set ``use_responses_lite``/``tool_mode =
    "code_mode_only"``, and cloning one changes what Codex *sends*: no
    ``instructions`` field, no ``tools`` array, and the tool definitions moved into
    an ``additional_tools`` input item wrapping a JavaScript ``exec`` tool.
    Lingling's bridge reads ``instructions`` and ``tools``, so that variant loses
    the system prompt and every tool. Verified against a capture proxy.

    ``--template`` overrides this for anyone who wants to experiment.
    """
    if slug:
        for model in models:
            if model.get("slug") == slug:
                return model
        raise SystemExit(
            f"No model named {slug!r} in Codex's catalog. Available: "
            + ", ".join(m.get("slug", "?") for m in models)
        )
    listed = [m for m in models if m.get("visibility") == "list"] or models
    classic = [
        m for m in listed
        if not m.get("use_responses_lite") and m.get("tool_mode") != "code_mode_only"
    ]
    if not classic:
        raise SystemExit(
            "Every model in Codex's catalog uses the responses-lite/code-mode wire "
            "shape, which the Lingling bridge does not read. Pass --template "
            "explicitly if you want to try one anyway."
        )
    return min(classic, key=lambda m: m.get("priority", 1_000_000))



def strip_our_own_entries(models: list, ours: set) -> list:
    """Drop entries that came from a previously generated Lingling catalog.

    Once ``model_catalog_json`` is wired into ``config.toml``, ``codex debug
    models`` echoes that file back. Cloning one of our own entries would work but
    is wrong: the template's identity fields would already be Lingling's, and any
    drift would compound on every regeneration. Matching against the ids Lingling
    actually serves is exact -- no guessing from slug shapes.
    """
    return [m for m in models if m.get("slug") not in ours]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT, help=f"output file (default: {DEFAULT_OUT})")
    ap.add_argument("--codex", default="codex", help="path to the codex binary")
    ap.add_argument("--template", default=None, help="Codex model slug to clone (default: its top-priority classic-Responses model)")
    ap.add_argument("--no-write-config", action="store_true",
                    help="do not edit ~/.codex/config.toml (just print the line)")
    ap.add_argument("--no-key-setup", action="store_true",
                    help="do not open the API-key setup window after generating")
    args = ap.parse_args()

    catalog = UnifiedCatalog(registry.build_providers())
    models = [lm.to_dict() for lm in catalog.free()]
    if not models:
        raise SystemExit("Lingling's catalog is empty -- check network access to OpenCode.")

    # Ids Lingling will write, so a previous run's own entries can be filtered
    # out of the dump before one of them is picked as the template.
    ours = {m["id"] for m in models} | {config.MULTIMODEL_ID}
    dumped = strip_our_own_entries(dump_codex_catalog(args.codex), ours)
    if not dumped:
        # The whole dump was our own file: ask Codex for the catalog compiled
        # into the binary, which no config override can shadow.
        dumped = dump_codex_catalog(args.codex, bundled=True)
    template = pick_template(dumped, args.template)
    print(f"template: {template['slug']} ({len(template.get('base_instructions') or '')} chars of instructions)")

    payload = codex_catalog.build(
        template,
        models,
        auto_id=config.MULTIMODEL_ID,
        auto_name=config.MULTIMODEL_NAME,
        auto_description=config.MULTIMODEL_DESCRIPTION,
    )
    if not payload["models"]:
        raise SystemExit("No free model publishes reasoning effort values; nothing to declare.")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    print(f"wrote {args.out} ({args.out.stat().st_size / 1024:.0f} KB)")
    for entry in payload["models"]:
        levels = "/".join(lv["effort"] for lv in entry["supported_reasoning_levels"])
        print(f"  {entry['slug']:<26} {levels}")
    dial_less = [
        m["id"] for m in models
        if not codex_catalog.levels_for((m.get("capabilities") or {}).get("effort"))
    ]
    if dial_less:
        print("  (`default` = no dial published; runs on the model's own setting): "
              + ", ".join(dial_less))
    line = f'model_catalog_json = "{args.out.as_posix()}"'
    if args.no_write_config:
        print("\nAdd to ~/.codex/config.toml:")
        print(f"  {line}")
    else:
        cfg = Path.home() / ".codex" / "config.toml"
        ensure_config_line(cfg, line)
        print(f"\nwired into {cfg} (restart codex for it to take effect)")

    if not args.no_key_setup:
        _open_key_setup()
    return 0


def _open_key_setup() -> None:
    """Open the API-key setup window so the ``set LINGLING_API_KEY`` step is gone.

    Launched as its own process so the generator's terminal stays usable. The
    window auto-fills a key already on the dashboard (see
    ``backend/codex/setup_gui.py``); ``--no-key-setup`` skips it for scripted
    or CI runs.
    """
    backend_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    flags = getattr(subprocess, "DETACHED_PROCESS", 0)
    try:
        subprocess.Popen(
            [sys.executable, "-m", "codex.setup_gui"],
            cwd=backend_dir,
            creationflags=flags,
        )
        print("\nOpened the API-key setup window -- it auto-fills a key already")
        print("issued on the dashboard. Close it when done, open a NEW terminal,")
        print("and run codex -- no 'set LINGLING_API_KEY' step needed.")
    except Exception as exc:  # noqa: BLE001
        print(f"\nCould not open the API-key window ({exc}). Run it yourself:")
        print("  double-click setup_codex.bat (or: py -3 -m codex.setup_gui)")


def ensure_config_line(config_path: Path, line: str) -> bool:
    """Idempotently ensure ``line`` is a top-level key in the Codex config.

    ``model_catalog_json`` must sit above the first ``[section]`` header --
    TOML scopes any key below a header into that section, and Codex reads this
    key at top level only. The rest of the file is left byte-for-byte intact.
    Returns True when the key is present afterwards (added or already there).
    """
    path = Path(config_path)
    key = line.split("=", 1)[0].strip()
    if path.exists():
        text = path.read_text(encoding="utf-8")
        if any(l.strip().startswith(key) and "=" in l for l in text.splitlines()):
            return True
        lines = text.splitlines(keepends=True)
        idx = next((i for i, l in enumerate(lines) if l.lstrip().startswith("[")), None)
        if idx is None:
            lines.append(line + "\n")
        else:
            lines.insert(idx, line + "\n")
        path.write_text("".join(lines), encoding="utf-8")
        return True
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(line + "\n", encoding="utf-8")
    return True


if __name__ == "__main__":
    sys.exit(main())
