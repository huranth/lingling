"""A double-clickable setup window that puts a usable API key into Codex.

Claude Code takes its credential from ``settings.json``; Codex takes its from an
environment variable *named by* ``env_key`` in its ``[model_providers.lingling]``
block. So "setting the key" for Codex means two things this window automates:

* persist the key as the ``LINGLING_API_KEY`` user environment variable
  (``setx``), so a freshly opened terminal has it -- the manual
  ``set LINGLING_API_KEY=...`` step, automated; and
* make sure ``~/.codex/config.toml`` has the provider block wired with
  ``env_key = "LINGLING_API_KEY"`` so Codex actually reads that variable.

Everything else mirrors ``claudecode/setup_gui.py``: the key is auto-filled from
the gateway's own key store when one exists (never minted behind the user's
back -- that takes an explicit click or the CLI), a gateway with auth off needs
no key at all, and the window is plain tkinter so it can be double-clicked on a
fresh machine.

The two configuration files are intentionally separate. This window owns the
*credential* and the provider block; ``tools/codex_catalog.py`` owns the model
list (``model_catalog_json``) and is what you re-run when OpenCode publishes new
models. Neither clobbers the other.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from claudecode.setup_gui import auth_required, existing_key

CONFIG_PATH = Path.home() / ".codex" / "config.toml"
KEY_ENV = "LINGLING_API_KEY"
# The gateway root (health/models live here). Codex's provider base_url must be
# this + "/v1", which codex_base_url() derives -- we keep the root here so the
# API calls (auth_required, existing_key, mint) hit the right paths.
DEFAULT_BASE_URL = "http://127.0.0.1:8000"


def codex_base_url(base_url: str) -> str:
    """The provider base_url Codex needs.

    Codex appends ``/responses`` (and ``/models``) to the configured base_url
    without adding ``/v1``, but Lingling serves those at ``/v1/...`` -- so the
    base_url must carry the ``/v1`` prefix. This normalises any gateway root
    (or already-prefixed value) into the form Codex requires.
    """
    url = (base_url or "").strip().rstrip("/")
    if not url:
        url = DEFAULT_BASE_URL.rstrip("/")
    if not url.endswith("/v1"):
        url += "/v1"
    return url

# ---------------------------------------------------------------------------
# config.toml editing -- merge-only, never clobbers unrelated keys
# ---------------------------------------------------------------------------
def _load_config() -> str:
    return CONFIG_PATH.read_text(encoding="utf-8") if CONFIG_PATH.exists() else ""


def _has_top_level(text: str, key: str) -> bool:
    return any(
        line.strip().startswith(key) and "=" in line
        for line in text.splitlines()
    )


def _ensure_top_level(text: str, key: str, value: str) -> str:
    """Add ``key = "value"`` above the first ``[section]`` header if missing.

    TOML scopes any key below a ``[header]`` into that section, and Codex reads
    top-level keys only -- so the line must land above the first section, not
    at the end of the file.
    """
    if _has_top_level(text, key):
        return text
    line = f'{key} = "{value}"'
    lines = text.splitlines(keepends=True)
    idx = next((i for i, l in enumerate(lines) if l.lstrip().startswith("[")), None)
    if idx is None:
        lines.append(line + "\n")
    else:
        lines.insert(idx, line + "\n")
    return "".join(lines)


def _provider_section(text: str, base_url: str, token: str) -> str:
    """Merge the ``[model_providers.lingling]`` block into ``text``.

    Unrelated keys in an existing block (and the whole rest of the file) are
    preserved. The managed keys are name/base_url/wire_api/env_key; ``env_key``
    is written only when a key is being set, because a keyless gateway must not
    name a variable Codex will then demand.
    """
    managed = {
        "name": "Lingling",
        "base_url": codex_base_url(base_url),
        "wire_api": "responses",
    }
    if token:
        managed["env_key"] = KEY_ENV
    header = "[model_providers.lingling]"

    lines = text.splitlines(keepends=True)
    start = next(
        (i for i, l in enumerate(lines)
         if l.strip().lower() == header.lower()),
        None,
    )

    if start is None:
        block = [header + "\n"] + [
            f'{k} = "{v}"\n' for k, v in managed.items()
        ]
        first_section = next(
            (i for i, l in enumerate(lines) if l.lstrip().startswith("[")),
            None,
        )
        if first_section is None:
            lines.extend(["\n"] + block)
        else:
            lines[first_section:first_section] = block + ["\n"]
        return "".join(lines)

    # Merge into the existing block: the block ends at the next [section] or EOF.
    end = start + 1
    while end < len(lines) and not lines[end].lstrip().startswith("["):
        end += 1

    new_block_lines: List[str] = []
    seen: set = set()
    for raw in lines[start + 1:end]:
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            new_block_lines.append(raw)
            continue
        key = stripped.split("=", 1)[0].strip().strip('"').strip("'")
        if key in managed:
            seen.add(key)
            new_block_lines.append(f'{key} = "{managed[key]}"\n')
        else:
            new_block_lines.append(raw)
    for k, v in managed.items():
        if k not in seen:
            new_block_lines.append(f'{k} = "{v}"\n')

    return (
        "".join(lines[:start])
        + header
        + "\n"
        + "".join(new_block_lines)
        + "".join(lines[end:])
    )


def _write_config(text: str) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_PATH.with_name("config.toml.tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(CONFIG_PATH)


# ---------------------------------------------------------------------------
# Key handling
# ---------------------------------------------------------------------------
def persist_key_env(token: str) -> Tuple[bool, Optional[str]]:
    """Persist ``KEY_ENV`` for future processes via the user environment.

    ``setx`` writes the user-level registry value, so every terminal opened
    from now on has ``LINGLING_API_KEY`` set -- that is the "automate the
    `set LINGLING_API_KEY` step" part. Returns ``(ok, error)``.
    """
    try:
        proc = subprocess.run(
            ["setx", KEY_ENV, token], capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"Could not run setx ({exc}). Set {KEY_ENV} manually."
    if proc.returncode != 0:
        return False, (proc.stderr or proc.stdout or "setx failed").strip()[:200]
    return True, None


def mint_key(
    base_url: str, timeout: float = 4.0, label: str = "Codex",
) -> Tuple[str, Optional[str]]:
    """Create a brand-new key on the local gateway (explicit action only).

    Reachable through the window's "Create a key" button or the CLI. A keyless
    gateway returns ``("", None)`` -- there is nothing to mint.
    """
    if auth_required(base_url, timeout) is not True:
        return "", None
    try:
        from core import api_keys
    except ImportError:
        return "", "Paste a key from the dashboard (Keys view)."
    try:
        created = api_keys.create_key(label)
    except OSError as exc:
        return "", f"Could not mint a key ({exc}). Paste one from the dashboard."
    token = created.get("token")
    if isinstance(token, str) and token:
        return token, None
    return "", "Paste a key from the dashboard (Keys view)."


def apply(base_url: str, token: str, persist: bool = True) -> Dict[str, Any]:
    """Wire the key into Codex. Returns a result dict for the UI/CLI.

    ``persist=False`` (used by tests) skips ``setx``.
    """
    keyless = not token
    text = _ensure_top_level(_load_config(), "model_provider", "lingling")
    text = _provider_section(text, base_url, token)
    _write_config(text)

    persisted, persist_error = (False, None)
    if not keyless and persist:
        persisted, persist_error = persist_key_env(token)

    return {
        "config_path": str(CONFIG_PATH),
        "env_var": KEY_ENV,
        "keyless": keyless,
        "persisted": persisted,
        "persist_error": persist_error,
        "base_url": codex_base_url(base_url),
    }


# ---------------------------------------------------------------------------
# The window
# ---------------------------------------------------------------------------
def run_gui() -> int:
    """Show the API-key window. Returns a process exit code."""
    try:
        import tkinter as tk
        from tkinter import messagebox, ttk
    except ImportError:
        print("This build of Python has no tkinter. Use the command line instead:")
        print("  py -3 -m codex.setup_gui --key ll_...")
        return 2

    root = tk.Tk()
    root.title("Lingling -- Codex key setup")
    root.resizable(False, False)

    frame = ttk.Frame(root, padding=16)
    frame.grid(sticky="nsew")

    ttk.Label(
        frame,
        text="Put a usable API key into Codex",
        font=("Segoe UI", 13, "bold"),
    ).grid(row=0, column=0, columnspan=3, sticky="w")
    ttk.Label(
        frame,
        text=("Auto-fills the key already issued on the dashboard.\n"
              "Apply saves it as LINGLING_API_KEY and wires ~/.codex/config.toml."),
        foreground="#555555",
        justify="left",
    ).grid(row=1, column=0, columnspan=3, sticky="w", pady=(2, 12))

    # -- gateway ---------------------------------------------------------
    ttk.Label(frame, text="Gateway").grid(row=2, column=0, sticky="w", pady=4)
    url_var = tk.StringVar(value=DEFAULT_BASE_URL)
    ttk.Entry(frame, textvariable=url_var, width=34).grid(
        row=2, column=1, sticky="we", pady=4,
    )

    # -- key ---------------------------------------------------------------
    ttk.Label(frame, text="API key").grid(row=3, column=0, sticky="w", pady=4)
    token_var = tk.StringVar()
    ttk.Entry(frame, textvariable=token_var, width=34, show="*").grid(
        row=3, column=1, sticky="we", pady=4,
    )
    ttk.Label(
        frame, text="auto-fills from the dashboard; edit it if you must",
        foreground="#777777", font=("Segoe UI", 8),
    ).grid(row=4, column=1, sticky="w")

    status = tk.Text(frame, height=10, width=64, wrap="word", relief="flat",
                     background="#f4f2ee", foreground="#333333")
    status.grid(row=5, column=0, columnspan=3, sticky="we", pady=(12, 8))
    status.configure(state="disabled")

    def say(text: str) -> None:
        status.configure(state="normal")
        status.delete("1.0", "end")
        status.insert("1.0", text)
        status.configure(state="disabled")

    def find_key() -> None:
        """Fill the field with a key already on the dashboard (read-only)."""
        url = url_var.get().strip()
        required = auth_required(url)
        if required is False:
            token_var.set("")
            say("This gateway accepts unauthenticated calls (LINGLING_REQUIRE_KEY=0).\n"
                "No key is needed -- just click Apply & save.")
            return
        if required is None:
            token_var.set("")
            say(f"Could not reach {url}.\n\nStart it with:\n  cd backend\n  python app.py\n\n"
                "then click 'Find key'.")
            return
        found = existing_key(url)
        token_var.set(found)
        if found:
            say(f"Found a key already issued on the dashboard (ends \u2026{found[-4:]}).\n"
                "Click 'Apply & save' to persist it as LINGLING_API_KEY.")
        else:
            say("The dashboard has no key issued yet.\n"
                "Click 'Create a key' to mint one now, or paste a key from the Keys tab.")

    def create_key() -> None:
        url = url_var.get().strip()
        token, error = mint_key(url)
        if error:
            messagebox.showerror("Could not create a key", error)
            return
        if not token:
            say("This gateway accepts unauthenticated calls -- no key to create.")
            token_var.set("")
            return
        token_var.set(token)
        say(f"Created a new key (ends \u2026{token[-4:]}).\n"
            f"Click 'Apply & save' to persist it as {KEY_ENV}.")

    def on_apply() -> None:
        url = url_var.get().strip()
        token = token_var.get().strip()
        if not token and auth_required(url) is not False:
            if not messagebox.askyesno(
                    "No key entered",
                    "This gateway requires a key but the field is empty.\n"
                    "Continue anyway (every request will 401)?"):
                return
        try:
            result = apply(url, token)
        except OSError as exc:
            messagebox.showerror("Could not write config", str(exc))
            return

        lines = [
            f"Done. Codex will use {result['env_var']} for Lingling at {result['base_url']}.",
            "",
            f"Wired {result['config_path']}",
        ]
        if result["keyless"]:
            if auth_required(url) is False:
                lines.append("No key needed -- this gateway accepts unauthenticated calls.")
            else:
                lines.append("No key written, but this gateway requires one.")
        else:
            if result["persisted"]:
                lines.append(
                    f"Saved the key as a user environment variable ({result['env_var']})."
                )
            else:
                lines.append(f"note: {result['persist_error'] or 'key not persisted'}")
        lines += [
            "",
            "Open a NEW terminal and run:  codex",
            "The key is picked up automatically -- no 'set LINGLING_API_KEY' needed.",
        ]
        say("\n".join(lines))

    buttons = ttk.Frame(frame)
    buttons.grid(row=6, column=0, columnspan=3, sticky="e", pady=(10, 0))
    ttk.Button(buttons, text="Find key", command=find_key).pack(side="left", padx=4)
    ttk.Button(buttons, text="Create a key", command=create_key).pack(side="left", padx=4)
    ttk.Button(buttons, text="Apply & save", command=on_apply).pack(side="left", padx=4)
    ttk.Button(buttons, text="Close", command=root.destroy).pack(side="left", padx=4)

    find_key()
    root.mainloop()
    return 0


# ---------------------------------------------------------------------------
# Headless path
# ---------------------------------------------------------------------------
def run_cli(argv: List[str]) -> int:
    """Command-line path for a machine with no tkinter or a scripted install."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Put a usable API key into Codex "
                    "(writes ~/.codex/config.toml and persists LINGLING_API_KEY).",
    )
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--key", default=None,
                        help="API key; omit to reuse the dashboard's key or mint one")
    parser.add_argument("--no-setx", action="store_true",
                        help="do not persist the key to the user environment")
    args = parser.parse_args(argv)

    required = auth_required(args.base_url)
    if required is None:
        print(f"Could not reach {args.base_url}. Is the backend running?")
        return 1

    token = args.key
    if token is None:
        if required is False:
            token = ""
        else:
            found = existing_key(args.base_url)
            if found:
                token = found
                print(f"Using the key already on the dashboard (ends \u2026{found[-4:]}).")
            else:
                # The CLI may mint: unlike the GUI it is an explicit, scripted
                # invocation, and a headless install has no window to paste into.
                token, key_error = mint_key(args.base_url)
                if key_error:
                    print(key_error)
                    return 1
                print("Created a new key on the dashboard.")

    result = apply(args.base_url, token, persist=not args.no_setx)
    print(f"Wired {result['config_path']}")
    if result["keyless"]:
        print("No key needed -- this gateway accepts unauthenticated calls.")
    elif result["persisted"]:
        print(f"Saved {result['env_var']} for new terminals.")
    else:
        print(f"note: {result['persist_error'] or 'key not persisted'}")
    print(f"\nOpen a NEW terminal and run:  codex\n(no 'set {KEY_ENV}=...' step needed.)")
    return 0


def main() -> int:
    try:
        if len(sys.argv) > 1:
            return run_cli(sys.argv[1:])
        return run_gui()
    except Exception as exc:  # noqa: BLE001
        # A double-clicked .py closes its console the moment it dies, hiding the
        # reason. Print it and hold the window open instead of flashing away.
        import traceback
        traceback.print_exc()
        try:
            input("\nThe setup window could not start. Press Enter to close...")
        except EOFError:
            pass
        return 1


if __name__ == "__main__":
    sys.exit(main())
