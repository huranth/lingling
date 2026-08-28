"""Build and install Codex's ``model_catalog_json`` so the effort dial reaches
the model -- the parallel of Claude Code's ``setup_gui`` for the
OpenAI-Responses side of the gateway.

Codex decides whether to put ``reasoning: {"effort": ...}`` on the wire at all by
looking the requested model up in its own catalog. A model it does not recognise
gets the field nulled, no matter what ``model_reasoning_effort`` says -- verified
against a capture proxy: with Codex's stock catalog, the wire carries
``"reasoning": null`` for ``deepseek-v4-flash-free``; declaring the same model in
a ``model_catalog_json`` with a non-empty ``supported_reasoning_levels`` and the
identical request carries ``{"effort": "high", "summary": "auto"}`` instead.
The catalog also has to be a clone of a real Codex stock model, because one of
its 35 fields is ``base_instructions`` -- the entire Codex agent prompt, 11-21 KB
of it -- and a hand-written entry with a short prompt parses "fine" and then
quietly turns Codex into a much dumber agent, because that string *is* the
harness (see :mod:`models.codex_catalog` for the contract).

This file is the launcher half: it asks Codex's own CLI (``codex debug models``)
for one template entry it can clone from, asks the running Lingling gateway
(``/api/models``) for the live free model list, calls
:func:`models.codex_catalog.build` to assemble the catalog payload, writes it
atomically over the user's existing ``~/.codex/lingling_models.json`` (backing
the old file up first), and patches ``~/.codex/config.toml`` so the top-level
``model_catalog_json = "..."`` line points at the new file. The catalog file is
the source of truth at runtime; the config patch is the addressing.

Everything is idempotent. Running it again after OpenCode rotates a model out
(or a Codex upgrade adds a field we never heard of) just rebuilds the file from
whatever is live today. The user's existing catalog is backed up before the
write, the config patch only touches the ``model_catalog_json`` key (and only
moves it when it lands in the wrong zone -- below a ``[section]`` header that
scops it), and the rest of their ``config.toml`` is left exactly as they had it.

Run with no arguments for the window. The headless form is
``python setup_codex.py --apply``.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# PATHEXT on Windows appends .CMD / .BAT to the npm global bins; subprocess with
# shell=False uses CreateProcess which refuses those scripts directly (it only
# spawns .EXE / .COM), so we route .CMD/.BAT through ``cmd /c``. POSIX has no
# such indirection -- ``codex`` is a plain executable there.
_WIN_CMD_EXTS = (".cmd", ".bat") if sys.platform == "win32" else ()


# ---------------------------------------------------------------------------
# Paths and constants
# ---------------------------------------------------------------------------
CODEX_DIR = Path.home() / ".codex"
CODEX_CONFIG_FILE = CODEX_DIR / "config.toml"
CODEX_CATALOG_FILE = CODEX_DIR / "lingling_models.json"
DEFAULT_BASE_URL = "http://127.0.0.1:8000"

# The catalog pointer in config.toml. TOML scopes every key after a ``[... ]``
# header into that section, and Codex reads ``model_catalog_json`` as a
# top-level key only, so the line has to sit above the first section -- which is
# why the patcher drops + re-inserts when the line has drifted inside one.
_CATALOG_KEY_RE = re.compile(r'^\s*model_catalog_json\s*=\s*".*"\s*$')
_MODEL_PROVIDER_RE = re.compile(r'^\s*model_provider\s*=\s*".*"\s*$')
_MODEL_RE = re.compile(r'^\s*model\s*=\s*".*"\s*$')
_SECTION_RE     = re.compile(r'^\s*\[')
_LINGLING_PROVIDER_HEADER_RE = re.compile(r'^\s*\[model_providers\.lingling\]\s*$')


# ---------------------------------------------------------------------------
# Template discovery (Codex side)
# ---------------------------------------------------------------------------
def dump_codex_models(timeout: float = 10.0) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """Run ``codex debug models`` and parse its JSON output.

    Returns ``(models, error)``. The CLI ships with the Codex install; we shell
    out because that is the one path Codex itself guarantees, and it carries
    every stock entry verbatim -- including ``base_instructions`` -- which is
    what the template contract needs.
    """
    exe = shutil.which("codex")
    if exe is None:
        return [], (
            "Codex CLI is not on PATH. Install it (npm i -g @openai/codex) "
            "and try again."
        )
    # npm installs a ``codex.cmd`` shim on Windows; CreateProcess cannot spawn
    # .CMD/.BAT scripts directly, so route those through ``cmd /c``. (POSIX
    # ``codex`` is a real executable and just runs.)
    argv: List[str] = [exe]
    if exe.lower().endswith(_WIN_CMD_EXTS):
        argv = ["cmd", "/c", exe]

    try:
        proc = subprocess.run(
            argv + ["debug", "models"],
            capture_output=True, text=True, timeout=timeout,
            encoding="utf-8", errors="replace",
        )
    except FileNotFoundError:
        return [], (
            "Codex CLI is not on PATH. Install it (npm i -g @openai/codex) "
            "and try again."
        )
    except subprocess.TimeoutExpired:
        return [], f"`codex debug models` did not finish in {timeout:g}s."
    except OSError as exc:
        return [], f"Could not spawn codex: {exc}."

    if proc.returncode != 0:
        snippet = (proc.stderr or "").strip()[:300]
        return [], f"`codex debug models` exited {proc.returncode}: {snippet}"
    text = (proc.stdout or "").strip()
    if not text:
        return [], "`codex debug models` produced no output."
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        return [], f"`codex debug models` did not return JSON ({exc})."
    models = payload.get("models") if isinstance(payload, dict) else None
    if not isinstance(models, list) or not models:
        return [], "`codex debug models` returned an empty or malformed model list."
    return [m for m in models if isinstance(m, dict)], None


def pick_template(
    models: List[Dict[str, Any]],
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Pick the Codex entry to clone the rest of the catalog from.

    Per :mod:`models.codex_catalog`'s contract, *every* rebuilt entry is a clone
    of one template with overridden identity, reasoning levels, and limits --
    so the template's heft (its ``base_instructions`` plus the ~30 other fields
    Codex's parser requires) becomes the backbone of every model. The wrong
    shape here parses "fine" and then quietly kills Codex's agent behaviour
    hours later, so the picker is conservative:

    1. The user's existing ``lingling-auto`` entry -- a previous run already
       templated this from a real Codex stock model, so reusing it preserves
       the exact agent prompt the user already had across rebuilds.
    2. Any other Lingling entry (slug ends in ``-free``) -- they all came through
       the same template pipeline, so any of them is a known-good template.
    3. The highest-priority stock model that still speaks the classic Responses
       shape -- on a fresh machine with no prior Lingling catalog. Codex's
       newest models set ``tool_mode = "code_mode_only"`` /
       ``use_responses_lite`` (a freeform JavaScript ``exec`` tool instead of
       the standard ``exec_command`` shell tool); ``entry_for`` normalises
       those fields away, but the code-mode template's ``base_instructions`` is
       a different harness variant, so a classic model is the better backbone.

    Whatever the picker selects is routed through :func:`_ensure_template_shape`
    before being returned, so a Codex upgrade that drops a
    ``REQUIRED_TEMPLATE_FIELDS`` field (Codex 0.146.1 dropped
    ``supports_reasoning_summaries``) does not break the build -- the missing
    field is polyfilled with the same value the builder itself would have
    written.
    """
    for m in models:
        if m.get("slug") == "lingling-auto":
            return _ensure_template_shape(m), None
    for m in models:
        slug = m.get("slug") or ""
        if isinstance(slug, str) and slug.endswith("-free"):
            return _ensure_template_shape(m), None
    classic = [
        m for m in models
        if not m.get("use_responses_lite") and m.get("tool_mode") != "code_mode_only"
    ]
    if classic:
        return _ensure_template_shape(min(classic, key=lambda m: m.get("priority", 1_000_000))), None
    if models:
        return _ensure_template_shape(models[0]), None
    return None, "Codex's catalog reports no models at all; nothing to template from."


# Polyfill defaults for required template fields removed by newer Codex
# versions. Each value is what ``codex_catalog.entry_for`` writes onto every
# entry it builds anyway (so the polyfill matches the builder's intent rather
# than inventing a value). Listing only the fields we know Codex dropped means
# a *new* silent schema change later surfaces as a real ``codex_catalog.build``
# "missing: <field>" error pointing at exactly the field we need to handle,
# rather than as quietly-injected garbage.
_TEMPLATE_FIELD_INJECTIONS: Dict[str, Any] = {
    # Removed in Codex 0.146.1's ``codex debug models`` output. The builder
    # writes ``True`` on every cloned entry via its own ``entry.update`` block,
    # so injecting ``True`` here lets the required-field check pass and matches
    # the value the builder wants every entry to carry regardless.
    "supports_reasoning_summaries": True,
}


def _ensure_template_shape(template: Dict[str, Any]) -> Dict[str, Any]:
    """Polyfill any required template field the installed Codex no longer emits.

    ``codex_catalog.REQUIRED_TEMPLATE_FIELDS`` is the contract; ``codex debug
    models`` is the source of templates. They have drifted -- not catastrophically
    (the catalog file from an earlier run still loads fine) but enough that one
    field the builder still pins has been removed from the CLI's output.

    Injection is the right level of surgery (vs. patching ``codex_catalog.py``)
    because the builder itself writes these same values when it produces entries;
    the only thing missing is the *template* shape requiring them in the first
    place. The lazy import keeps this file loadable without the backend present
    (the launcher should still be importable for, e.g., reading its docstring).
    """
    try:
        from models.codex_catalog import REQUIRED_TEMPLATE_FIELDS  # noqa: E402
    except ImportError:
        REQUIRED_TEMPLATE_FIELDS = ()
    out = dict(template)
    for field in REQUIRED_TEMPLATE_FIELDS:
        if field in out:
            continue
        if field in _TEMPLATE_FIELD_INJECTIONS:
            out[field] = _TEMPLATE_FIELD_INJECTIONS[field]
        # else: leave it missing. ``codex_catalog.build`` will surface a clear
        # ValueError naming the missing field rather than us inventing a value.
    return out


# ---------------------------------------------------------------------------
# Live models discovery (Lingling side)
# ---------------------------------------------------------------------------
def fetch_lingling_models(
    base_url: str,
    timeout: float = 5.0,
) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """Ask a running Lingling gateway for the live free model list.

    Returns ``(models, error)``. ``models`` are full ``LogicalModel.to_dict()``
    payloads from ``/api/models`` -- including ``capabilities.effort``, which is
    the field :func:`models.codex_catalog.build` reads to know what reasoning
    ladder each model speaks. ``/v1/models`` is the OpenAI-shape sibling and
    drops that field, so we deliberately use this route here.
    """
    url = base_url.rstrip("/") + "/api/models"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        return [], f"GET {url} -> HTTP {exc.code}"
    except (urllib.error.URLError, json.JSONDecodeError, OSError, ValueError) as exc:
        return [], f"Could not reach {url} ({exc}). Is the backend running?"

    if not isinstance(payload, dict):
        return [], f"{url} did not return a JSON object."
    models = payload.get("models")
    if not isinstance(models, list) or not models:
        return [], "The gateway is running but reported no free models."
    return [m for m in models if isinstance(m, dict)], None


# ---------------------------------------------------------------------------
# Catalog build + atomic write
# ---------------------------------------------------------------------------
def build_catalog(
    template: Dict[str, Any],
    models: List[Dict[str, Any]],
    auto_id: str,
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Call :func:`models.codex_catalog.build` on the supplied pieces.

    Lazy import: this package sits above the backend, so the import only runs
    when we actually need it -- and lets the module import cleanly in any
    environment that lacks the backend's deps (which the launcher should still
    be readable in, e.g. for its docstring).
    """
    if not template:
        return None, "No Codex template to clone from; nothing to build."
    if not models:
        return None, "The gateway reported no free Lingling models; nothing to declare."
    try:
        from models import codex_catalog
    except ImportError as exc:
        return None, f"Could not import codex_catalog from the backend ({exc})."
    try:
        payload = codex_catalog.build(template, models, auto_id=auto_id)
    except (TypeError, ValueError) as exc:
        return None, f"codex_catalog.build rejected its inputs ({exc})."
    if not isinstance(payload, dict) or not payload.get("models"):
        return None, "codex_catalog.build produced no entries; refusing to write an empty catalog."
    return payload, None


def write_catalog_atomic(
    payload: Dict[str, Any], target: Path,
) -> Tuple[Optional[Path], Optional[str]]:
    """Write the catalog JSON, backing up any existing file first.

    Returns the backup path, or ``None`` when there was nothing to back up. The
    write goes to a temporary file and is moved into place, so an interrupted
    run cannot leave Codex with a half-written catalog. The user's existing
    file is preserved as ``lingling_models.json.bak-<timestamp>`` so a run that
    goes wrong can be reverted by copying it back.
    """
    CODEX_DIR.mkdir(parents=True, exist_ok=True)
    backup: Optional[Path] = None
    if target.exists():
        backup = target.with_name(f"{target.name}.bak-{time.strftime('%Y%m%d-%H%M%S')}")
        shutil.copy2(target, backup)
    tmp = target.with_name(f"{target.name}.tmp")
    tmp.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    tmp.replace(target)
    # ``os.replace`` carries the new file's inode mode through (POSIX rename(2)),
    # so a user-set 0o600 on the previous file silently downgrades to 0o644 each
    # Apply. Re-assert owner-only.
    try:
        os.chmod(target, 0o600)
        if backup is not None:
            os.chmod(backup, 0o600)
    except OSError:
        pass
    return backup, None


def _windows_safe_path(p: Path) -> str:
    """Codex's TOML reader wants forward-slash paths even on Windows.

    Both slashes parse on Windows too, but the existing catalog pointer in the
    user's ``config.toml`` is forward-slash, and keeping that style across runs
    means a re-patch shows no spurious diff.
    """
    return str(p).replace("\\", "/")


def _ensure_top_level_key(lines: list[str], pattern: re.Pattern[str], new_line: str) -> list[str]:
    """Ensure a top-level (above first ``[section]``) key exists with ``new_line``.

    If the key already sits above the first section it is replaced in place;
    otherwise any stray copies (inside sections) are dropped and a fresh line is
    inserted just above the first section header. Preserves ordering of other keys.
    """
    first_section_idx = next(
        (i for i, ln in enumerate(lines) if _SECTION_RE.match(ln)),
        len(lines),
    )
    existing_idx = next(
        (i for i, ln in enumerate(lines) if pattern.match(ln)),
        None,
    )
    if existing_idx is not None and existing_idx < first_section_idx:
        out = list(lines)
        out[existing_idx] = new_line
        return out
    survivors = [ln for ln in lines if not pattern.match(ln)]
    first_section_idx = next(
        (i for i, ln in enumerate(survivors) if _SECTION_RE.match(ln)),
        len(survivors),
    )
    return survivors[:first_section_idx] + [new_line] + survivors[first_section_idx:]


def patch_config_toml(
    catalog_path: Path,
) -> Tuple[Optional[Path], Optional[str]]:
    """Insert/refresh Lingling's Codex wiring in ``~/.codex/config.toml``.

    Three things must sit correctly for Codex with a ChatGPT account to actually
    reach Lingling's free models (README § Codex):

    * ``model_catalog_json = "...lingling_models.json"`` -- top-level, above any
      ``[section]``, so Codex reads it.
    * ``model_provider = "lingling"`` -- top-level, so ``model`` resolves through
      Lingling's gateway instead of OpenAI (ChatGPT auth + ``laguna``/``muse-spark``
      = ``not supported when using Codex with a ChatGPT account``).
    * ``[model_providers.lingling]`` block with ``base_url`` + ``wire_api``.

    ``model`` itself is left alone when the user already has one -- a custom
    ``laguna-s-2.1-free`` or ``muse-spark-...`` is valid once the provider is
    ``lingling``. Only a fresh file gets ``model = "lingling-auto"`` seeded.

    A ``.bak-<timestamp>`` copy is taken first, so the patch is reversible.
    """
    catalog_line = f'model_catalog_json = "{_windows_safe_path(catalog_path)}"'
    provider_line = 'model_provider = "lingling"'

    if not CODEX_CONFIG_FILE.exists():
        CODEX_DIR.mkdir(parents=True, exist_ok=True)
        skeleton = [
            catalog_line,
            provider_line,
            'model = "lingling-auto"',
            "",
            "[model_providers.lingling]",
            'name = "Lingling"',
            'base_url = "http://127.0.0.1:8000/v1"',
            'wire_api = "responses"',
            "",
        ]
        CODEX_CONFIG_FILE.write_text("\n".join(skeleton), encoding="utf-8")
        try:
            os.chmod(CODEX_CONFIG_FILE, 0o600)
        except OSError:
            pass
        return None, None

    backup = CODEX_CONFIG_FILE.with_name(
        f"{CODEX_CONFIG_FILE.name}.bak-{time.strftime('%Y%m%d-%H%M%S')}"
    )
    shutil.copy2(CODEX_CONFIG_FILE, backup)

    raw_lines = CODEX_CONFIG_FILE.read_text(encoding="utf-8").splitlines()

    # 1) catalog pointer (top-level)
    out_lines = _ensure_top_level_key(raw_lines, _CATALOG_KEY_RE, catalog_line)
    # 2) provider pointer (top-level) -- the actual fix for the ChatGPT-account error
    out_lines = _ensure_top_level_key(out_lines, _MODEL_PROVIDER_RE, provider_line)
    # 3) seed a model line on fresh files only; existing model choice is respected
    #    (muse-spark/laguna/whatever is fine once provider is lingling)
    has_model = any(_MODEL_RE.match(ln) for ln in out_lines if not _SECTION_RE.match(ln) or out_lines.index(ln) < next((i for i, l in enumerate(out_lines) if _SECTION_RE.match(l)), len(out_lines)))
    # simpler: check top-level zone for a model key
    first_sec = next((i for i, ln in enumerate(out_lines) if _SECTION_RE.match(ln)), len(out_lines))
    has_model_top = any(_MODEL_RE.match(ln) for ln in out_lines[:first_sec])
    if not has_model_top:
        # insert model = "lingling-auto" right after model_provider (or catalog if provider missing)
        provider_idx = next((i for i, ln in enumerate(out_lines) if _MODEL_PROVIDER_RE.match(ln)), None)
        insert_at = (provider_idx + 1) if provider_idx is not None else first_sec
        out_lines = out_lines[:insert_at] + ['model = "lingling-auto"'] + out_lines[insert_at:]

    # 4) ensure [model_providers.lingling] block exists (append if missing)
    has_lingling_block = any(_LINGLING_PROVIDER_HEADER_RE.match(ln) for ln in out_lines)
    if not has_lingling_block:
        # ensure trailing newline before appending section
        if out_lines and out_lines[-1].strip() != "":
            out_lines.append("")
        out_lines.extend([
            "[model_providers.lingling]",
            'name = "Lingling"',
            'base_url = "http://127.0.0.1:8000/v1"',
            'wire_api = "responses"',
        ])
    else:
        # provider block exists but might be missing keys (old installs) -- patch them
        # Find block range: from header to next section or EOF
        header_idx = next(i for i, ln in enumerate(out_lines) if _LINGLING_PROVIDER_HEADER_RE.match(ln))
        next_sec = next((i for i, ln in enumerate(out_lines[header_idx+1:], start=header_idx+1) if _SECTION_RE.match(ln)), len(out_lines))
        block = out_lines[header_idx+1:next_sec]
        block_text = "\n".join(block)
        needed = {
            'name = "Lingling"': 'name',
            'base_url = "http://127.0.0.1:8000/v1"': 'base_url',
            'wire_api = "responses"': 'wire_api',
        }
        for line, key in needed.items():
            if key not in block_text:
                block.append(line)
        out_lines = out_lines[:header_idx+1] + block + out_lines[next_sec:]

    tmp = CODEX_CONFIG_FILE.with_name(f"{CODEX_CONFIG_FILE.name}.tmp")
    tmp.write_text("\n".join(out_lines).rstrip() + "\n", encoding="utf-8")
    tmp.replace(CODEX_CONFIG_FILE)
    try:
        os.chmod(CODEX_CONFIG_FILE, 0o600)
        if backup is not None:
            os.chmod(backup, 0o600)
    except OSError:
        pass
    return backup, None


# ---------------------------------------------------------------------------
# Orchestration (the button and the CLI run the same code)
# ---------------------------------------------------------------------------
def _lingling_auto_slug() -> str:
    """The router's own id; passed as ``auto_id`` so ``codex_catalog.build``
    prepends an entry for it. Lazy import so the module loads without the
    backend."""
    try:
        from core import config  # noqa: E402
        return getattr(config, "MULTIMODEL_ID", "lingling-auto")
    except ImportError:
        return "lingling-auto"


def apply(
    base_url: str = DEFAULT_BASE_URL,
) -> Dict[str, Any]:
    """End-to-end: template from Codex, models from Lingling, build, write, patch.

    Separated from the widgets so the command-line path and the window exercise
    exactly the same code the button does. Returns a result dict for both of
    them to render.
    """
    summary: Dict[str, Any] = {
        "base_url": base_url.rstrip("/"),
        "template_slug": None,
        "model_count": 0,
        "catalog_path": str(CODEX_CATALOG_FILE),
        "config_path": str(CODEX_CONFIG_FILE),
        "backup_catalog": None,
        "backup_config": None,
        "errors": [],
    }

    # 1. Template: the Codex stock entry everything will be cloned from.
    codex_models, err = dump_codex_models()
    if err:
        summary["errors"].append(err)
        return summary
    template, terr = pick_template(codex_models)
    if terr or not template:
        summary["errors"].append(terr or "no template chosen")
        return summary
    summary["template_slug"] = template.get("slug")

    # 2. Live free models from the running gateway.
    live, lerr = fetch_lingling_models(base_url)
    if lerr:
        summary["errors"].append(lerr)
        return summary

    # 3. Build the catalog. build() returns {"models": [...]}; if it produced
    #    nothing (e.g. the dispatcher has burned every model so catalog.free()
    #    returns []), we refuse to write -- overwriting the user's catalog with
    #    an empty file would silently strip Codex of every model until the
    #    next run, which is a fail-open we don't want.
    auto_id = _lingling_auto_slug()
    payload, berr = build_catalog(template, live, auto_id=auto_id)
    if berr:
        summary["errors"].append(berr)
        return summary
    entries = payload.get("models") if isinstance(payload, dict) else None
    summary["model_count"] = len(entries) if isinstance(entries, list) else 0

    # 4. Write the catalog JSON, replacing the existing file atomically.
    backup_cat, werr = write_catalog_atomic(payload, CODEX_CATALOG_FILE)
    if backup_cat:
        summary["backup_catalog"] = str(backup_cat)
    if werr:
        summary["errors"].append(werr)
        return summary

    # 5. Patch ~/.codex/config.toml's model_catalog_json pointer so Codex
    #    actually reads the new catalog.
    backup_cfg, perr = patch_config_toml(CODEX_CATALOG_FILE)
    if backup_cfg:
        summary["backup_config"] = str(backup_cfg)
    if perr:
        summary["errors"].append(perr)
    return summary


# ---------------------------------------------------------------------------
# The window
# ---------------------------------------------------------------------------
def run_gui() -> int:
    """Show the setup window; return a process exit code.

    tkinter ships with CPython on Windows, so this adds no dependency -- which
    matters for a launcher meant to be double-clicked on a fresh machine.
    """
    try:
        import tkinter as tk
        from tkinter import messagebox, ttk
    except ImportError:
        print("This build of Python has no tkinter. Use the command line instead:")
        print("  python setup_codex.py --apply")
        return 2

    root = tk.Tk()
    root.title("Lingling -- Codex setup")
    root.resizable(False, False)

    frame = ttk.Frame(root, padding=16)
    frame.grid(sticky="nsew")

    ttk.Label(
        frame, text="Point Codex at Lingling",
        font=("Segoe UI", 13, "bold"),
    ).grid(row=0, column=0, columnspan=3, sticky="w")
    ttk.Label(
        frame,
        text=("Writes Codex's model catalog (~/.codex/lingling_models.json) from\n"
              "the live Lingling free models, then patches ~/.codex/config.toml.\n"
              "Re-run any time OpenCode rotates a model."),
        foreground="#555555", justify="left",
    ).grid(row=1, column=0, columnspan=3, sticky="w", pady=(2, 12))

    # -- gateway ---------------------------------------------------------
    ttk.Label(frame, text="Gateway").grid(row=2, column=0, sticky="w", pady=4)
    url_var = tk.StringVar(value=DEFAULT_BASE_URL)
    ttk.Entry(frame, textvariable=url_var, width=32).grid(
        row=2, column=1, sticky="we", pady=4,
    )

    status = tk.Text(
        frame, height=11, width=66, wrap="word", relief="flat",
        background="#f4f2ee", foreground="#333333",
    )
    status.grid(row=6, column=0, columnspan=3, sticky="we", pady=(12, 8))
    status.configure(state="disabled")

    def say(text: str) -> None:
        status.configure(state="normal")
        status.delete("1.0", "end")
        status.insert("1.0", text)
        status.configure(state="disabled")

    def refresh() -> None:
        """Pre-flight without writing anything.

        Confirms the Codex CLI is installed (and which template entry it will
        donate) and the gateway is up, so the user does not click Apply blind.
        Apply still re-reads both; this is just an envelope.
        """
        codex_models, err = dump_codex_models()
        if err:
            say(f"{err}\n\nInstall Codex (npm i -g @openai/codex) and click Refresh.")
            return
        template, terr = pick_template(codex_models)
        if terr or not template:
            say(terr or "no template")
            return

        try:
            with urllib.request.urlopen(
                url_var.get().rstrip("/") + "/api/health", timeout=3.0,
            ) as resp:
                json.loads(resp.read().decode("utf-8", "replace"))
        except Exception:
            say(f"Could not reach the gateway at {url_var.get()}.\n\n"
                f"Start it with:\n  cd backend\n  python app.py\n\nthen click Refresh.")
            return

        say(
            "Ready to build.\n\n"
            f"  Template : {template.get('slug')}\n"
            f"  Gateway  : up\n\n"
            "Click Apply to fetch the live free models, build the catalog,\n"
            "write ~/.codex/lingling_models.json, and patch ~/.codex/config.toml."
        )

    def on_apply() -> None:
        try:
            result = apply(url_var.get().strip())
        except OSError as exc:
            messagebox.showerror("Could not write the catalog", str(exc))
            return
        except Exception as exc:
            # apply() reports failures through its summary, so anything that
            # escapes it is an unexpected fault. The handler must never crash
            # the Tkinter callback -- surface it as a dialog instead.
            messagebox.showerror("Unexpected error during setup", repr(exc))
            return

        lines = [
            f"Done. Codex catalog written to {result['catalog_path']}.",
            "",
            f"  Template       : {result['template_slug']}",
            f"  Catalog entries: {result['model_count']}",
            f"  Config         : {result['config_path']}",
        ]
        if result["backup_catalog"]:
            lines.append(f"  Backup         : {Path(result['backup_catalog']).name}")
        if result["backup_config"]:
            lines.append(f"  Backup config  : {Path(result['backup_config']).name}")
        if result["errors"]:
            lines += ["", "Errors:"]
            lines += [f"  - {e}" for e in result["errors"]]
        else:
            lines += [
                "",
                "Open a NEW terminal and run:  codex",
                "Codex's /model picker now lists every Lingling free model and",
                "sends a reasoning block on the wire for them.",
            ]
        say("\n".join(lines))

    buttons = ttk.Frame(frame)
    buttons.grid(row=5, column=0, columnspan=3, sticky="e", pady=(10, 0))
    ttk.Button(buttons, text="Refresh", command=refresh).grid(row=0, column=0, padx=4)
    ttk.Button(buttons, text="Apply", command=on_apply).grid(row=0, column=1, padx=4)
    ttk.Button(buttons, text="Close", command=root.destroy).grid(row=0, column=2, padx=4)

    refresh()
    root.mainloop()
    return 0


# ---------------------------------------------------------------------------
# Headless / CLI form
# ---------------------------------------------------------------------------
def run_cli(argv: List[str]) -> int:
    """Headless path: same code path as the button, no window.

    For a scripted install or a build of Python with no tkinter.
    """
    import argparse

    parser = argparse.ArgumentParser(
        prog="setup_codex",
        description="Build and install Codex's model_catalog_json for Lingling.",
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="Build, write ~/.codex/lingling_models.json, and patch ~/.codex/config.toml.",
    )
    parser.add_argument(
        "--base-url", default=DEFAULT_BASE_URL,
        help=f"Lingling base URL (default: {DEFAULT_BASE_URL}).",
    )
    args = parser.parse_args(argv)

    if not args.apply:
        parser.print_help()
        print("\nAdd --apply to build and install non-interactively.")
        return 0

    result = apply(args.base_url)
    print("=" * 70)
    print("Codex catalog generator")
    print("=" * 70)
    print(f"  Gateway         : {result['base_url']}")
    print(f"  Template        : {result['template_slug'] or '(none)'}")
    print(f"  Catalog entries : {result['model_count']}")
    print(f"  Catalog file    : {result['catalog_path']}")
    print(f"  Config file     : {result['config_path']}")
    if result["backup_catalog"]:
        print(f"  Backup catalog  : {result['backup_catalog']}")
    if result["backup_config"]:
        print(f"  Backup config   : {result['backup_config']}")
    if result["errors"]:
        print("\nErrors:")
        for err in result["errors"]:
            print(f"  - {err}")
        return 1
    print("\nDone. Run `codex` in a NEW terminal; /model now lists every Lingling")
    print("free model and the reasoning dial actually reaches them.")
    return 0


def main() -> int:
    if len(sys.argv) > 1:
        return run_cli(sys.argv[1:])
    return run_gui()


if __name__ == "__main__":
    sys.exit(main())
