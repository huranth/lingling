"""A double-clickable setup window that points Claude Code at Lingling.

What this configures, and what it deliberately leaves alone
----------------------------------------------------------
Only one thing is a setup-time decision: **which gateway to talk to**. Everything
about *how* a turn is run -- the model, the thinking depth -- belongs to the
terminal, where ``/model`` and ``/effort`` change it mid-session. Lingling reads
the depth off every single request and clamps it to what the routed model
publishes, exactly as it does for Codex, so pinning either one here would replace
a live dial with a dead one.

That is not a style preference, it is Anthropic's precedence rule:
``ANTHROPIC_MODEL`` in the environment *overrides* the ``model`` setting, and
"Claude Code uses the settings key only when the environment variable is unset".
Writing ``ANTHROPIC_MODEL`` therefore breaks ``/model``: the picker shows your
choice and the request carries the pinned one. So this writes ``model`` as a
*starting* default and never touches the environment variable.

The two things it does write to ``env`` are the endpoint and the credential.
Neither is switchable from inside a session, and both need to beat whatever a
previous gateway left behind -- Anthropic documents a settings-file ``env`` entry
as taking precedence over a shell export of the same name, which is the only way
to neutralise a persisted ``ANTHROPIC_BASE_URL`` without editing the user's
environment.

It also removes any ``provider`` block. That key is absent from Anthropic's
settings reference -- some third-party launchers invent it -- and it outranks
every documented setting, so a leftover one silently wins over all of the above.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

SETTINGS_DIR = Path.home() / ".claude"
SETTINGS_FILE = SETTINGS_DIR / "settings.json"

# Written so editors and Claude Code itself can validate the file.
SCHEMA = "https://json.schemastore.org/claude-code-settings.json"

# Endpoint and credential only. `ANTHROPIC_MODEL` is deliberately absent: it would
# override the `model` setting and break `/model`. `CLAUDE_CODE_EFFORT_LEVEL` is
# absent for the same reason with respect to `/effort`.
MANAGED_ENV = ("ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN")

# Variables that pin something the terminal is supposed to own. Reported loudly,
# because a leftover one makes `/model` or `/effort` look broken.
HIJACK_VARS = ("ANTHROPIC_MODEL", "CLAUDE_CODE_EFFORT_LEVEL")

DEFAULT_BASE_URL = "http://127.0.0.1:8000"


def load_settings() -> Tuple[Dict[str, Any], Optional[str]]:
    """Read the existing settings file, tolerating a broken one.

    Returns ``(settings, warning)``. A file that will not parse is *not* silently
    replaced -- the caller is told, so the user can decide, because that file may
    hold permissions and hooks they care about.
    """
    if not SETTINGS_FILE.exists():
        return {}, None
    try:
        text = SETTINGS_FILE.read_text(encoding="utf-8")
    except OSError as exc:
        return {}, f"Could not read {SETTINGS_FILE}: {exc}"
    if not text.strip():
        return {}, None
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        return {}, f"{SETTINGS_FILE.name} is not valid JSON ({exc}). Applying will replace it."
    if not isinstance(data, dict):
        return {}, f"{SETTINGS_FILE.name} does not contain a JSON object. Applying will replace it."
    return data, None


def stale_conflicts(settings: Dict[str, Any]) -> List[str]:
    """Everything on this machine that could override what we are about to write.

    The ``provider`` block is removed by :func:`build_settings`; the rest is
    reported, because silently deleting a user's environment variables is a bigger
    surprise than telling them one is in the way.
    """
    found: List[str] = []

    provider = settings.get("provider")
    if isinstance(provider, dict):
        where = provider.get("apiUrl") or provider.get("name") or "unknown"
        found.append(
            f"an undocumented 'provider' block pointing at {where} "
            f"(removed -- it overrides every documented setting)"
        )

    # These are the ones that actually break the terminal. ANTHROPIC_MODEL beats
    # the `model` setting, so /model appears to do nothing; CLAUDE_CODE_EFFORT_LEVEL
    # does the same to /effort.
    for name in HIJACK_VARS:
        value = os.environ.get(name)
        if value:
            found.append(
                f"{name}={value[:40]} is set in your environment -- unset it, or "
                f"/{'model' if name == 'ANTHROPIC_MODEL' else 'effort'} will not take effect"
            )

    # A settings-file `env` entry beats a shell export of the same name, so these
    # are handled rather than blocking -- but worth naming so the user knows why
    # their old gateway stopped being used.
    for name in MANAGED_ENV:
        value = os.environ.get(name)
        if value:
            found.append(
                f"{name}={value[:40]} is exported in your shell "
                f"-- the settings file now takes precedence over it"
            )

    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_DEFAULT_HAIKU_MODEL",
                 "ANTHROPIC_DEFAULT_SONNET_MODEL", "ANTHROPIC_DEFAULT_OPUS_MODEL"):
        if os.environ.get(name):
            found.append(f"{name} is set in your environment ({os.environ[name][:40]})")

    if settings.get("primaryApiKey"):
        found.append("a 'primaryApiKey' key (not a documented setting; left alone)")

    return found


def fetch_model_overrides(
    base_url: str, timeout: float = 6.0,
) -> Dict[str, Any]:
    """contextWindowOverrides + maxTokensOverrides for every free model.

    Claude Code warns and assumes a 200k window for any model id it doesn't
    recognise -- wrong for the 1M-context models -- so each model's real window
    is declared under ``contextWindowOverrides`` (and its output cap under
    ``maxTokensOverrides``). Read live from the gateway, so a newly published
    model is covered the next time setup runs. Both maps are empty when the
    gateway is unreachable; callers then leave existing overrides alone.
    """
    url = base_url.rstrip("/") + "/v1/models"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, json.JSONDecodeError, OSError, ValueError):
        return {}

    ctx_map: Dict[str, int] = {}
    out_map: Dict[str, int] = {}
    no_window: List[str] = []
    for m in payload.get("data", []):
        if not isinstance(m, dict) or not m.get("id"):
            continue
        mid = m["id"]
        ctx = m.get("context_length")
        if not ctx:
            # The router (lingling-auto) has no fixed window; it routes to any
            # free model, so its declared window is the smallest one -- the only
            # value that can never overestimate what it lands on.
            no_window.append(mid)
            continue
        ctx_map[mid] = int(ctx)
        out_max = m.get("max_output")
        if out_max:
            out_map[mid] = int(out_max)

    smallest = min(ctx_map.values(), default=None)
    for mid in no_window:
        if smallest:
            ctx_map[mid] = smallest
    return {"contextWindowOverrides": ctx_map, "maxTokensOverrides": out_map}


def build_settings(
    existing: Dict[str, Any], base_url: str, token: str, model: str,
    overrides: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Merge Lingling's configuration into whatever is already there.

    Everything unrelated -- permissions, hooks, theme, MCP servers -- is preserved.
    ``model`` is written as the *starting* model, which ``/model`` overwrites the
    moment it is used; no effort key is written at all, because ``/effort`` owns
    that and Lingling reads it off each request.
    """
    out = dict(existing)
    out["$schema"] = SCHEMA

    # The undocumented block that beats every documented setting. Removing it is
    # the single most important thing this function does.
    out.pop("provider", None)

    env = dict(out.get("env") or {}) if isinstance(out.get("env"), dict) else {}
    env["ANTHROPIC_BASE_URL"] = base_url.rstrip("/")
    # An empty string is documented as "treated as unset for provider selection",
    # which is what a keyless local gateway wants -- and it still neutralises a
    # leftover export from another gateway.
    env["ANTHROPIC_AUTH_TOKEN"] = token
    # Never written: it would outrank the `model` key and make /model a no-op.
    env.pop("ANTHROPIC_MODEL", None)
    env.pop("CLAUDE_CODE_EFFORT_LEVEL", None)
    out["env"] = env

    out["model"] = model
    # Any previously pinned depth is dropped rather than replaced. /effort writes
    # this key itself when the user picks a level, and leaving a stale value here
    # would silently set the floor for every new session.
    out.pop("effortLevel", None)
    # Declare each free model's real context window / output cap so Claude Code
    # stops assuming 200k for ids it doesn't recognise (fetch_model_overrides).
    if overrides:
        for key in ("contextWindowOverrides", "maxTokensOverrides"):
            vals = overrides.get(key) or {}
            if vals:
                merged = dict(out.get(key) or {})
                merged.update(vals)
                out[key] = merged
    return out


def write_settings(data: Dict[str, Any]) -> Optional[Path]:
    """Write settings.json, backing up any existing file first.

    Returns the backup path, or None when there was nothing to back up. The write
    goes to a temporary file and is then moved into place, so an interrupted run
    cannot leave Claude Code with a half-written config.
    """
    SETTINGS_DIR.mkdir(parents=True, exist_ok=True)
    backup: Optional[Path] = None
    if SETTINGS_FILE.exists():
        backup = SETTINGS_FILE.with_suffix(f".json.bak-{time.strftime('%Y%m%d-%H%M%S')}")
        shutil.copy2(SETTINGS_FILE, backup)

    tmp = SETTINGS_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    tmp.replace(SETTINGS_FILE)
    return backup


def fetch_models(base_url: str, timeout: float = 4.0) -> Tuple[List[str], Optional[str]]:
    """Ask a running Lingling for its free models.

    Returns ``(model_ids, error)``. Read live rather than hardcoded: OpenCode's
    free tier changes, and a stale list would offer models that 404 when chosen.
    """
    url = base_url.rstrip("/") + "/v1/models"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.URLError as exc:
        return [], f"Could not reach {url} ({exc.reason}). Is the backend running?"
    except (json.JSONDecodeError, OSError, ValueError) as exc:
        return [], f"{url} did not return a model list ({exc})."

    ids = [
        entry["id"] for entry in payload.get("data", [])
        if isinstance(entry, dict) and isinstance(entry.get("id"), str)
    ]
    if not ids:
        return [], "The gateway is running but reported no models."
    return ids, None


def fetch_or_mint_key(
    base_url: str, timeout: float = 4.0, label: str = "Claude Code",
) -> Tuple[str, Optional[str]]:
    """Get a usable API key without the user copying one by hand.

    Read from Lingling's own key store on disk rather than over HTTP. ``/api/keys``
    is authenticated -- correctly, since it can mint and revoke credentials -- so
    asking it for a key requires the key we are trying to obtain. The file is the
    way out of that circle: anything that can write ``~/.claude/settings.json``
    already has the filesystem access the store would need, so reading it grants
    no privilege the caller did not have.

    Order:

    * auth is switched off -- no key is needed, return ``""``;
    * a key already exists -- reuse it, so repeated runs do not litter the keyring;
    * none exists -- mint one.

    Returns ``(token, error)``. An error means the user must paste one, which the
    window then asks for -- the case for a gateway on another machine.
    """
    # A gateway with auth disabled answers /api/health with required=false, and a
    # key would be pointless noise. This also proves it is actually running.
    try:
        with urllib.request.urlopen(base_url.rstrip("/") + "/api/health", timeout=timeout) as resp:
            health = json.loads(resp.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, json.JSONDecodeError, OSError, ValueError) as exc:
        return "", f"Could not reach {base_url.rstrip('/')}/api/health ({exc})."

    auth = health.get("auth") if isinstance(health, dict) else None
    if isinstance(auth, dict) and auth.get("required") is False:
        return "", None

    try:
        from core import api_keys
        from core import config
    except ImportError:
        return "", "Paste a key from the dashboard (Keys view)."

    # list_keys() masks the token by design -- it feeds the dashboard, which must
    # never redisplay a secret. The store itself holds the real one, and that is
    # what Claude Code needs, so read it directly.
    try:
        raw = json.loads(Path(config.API_KEYS_FILE).read_text(encoding="utf-8"))
    except FileNotFoundError:
        raw = []
    except (OSError, json.JSONDecodeError) as exc:
        return "", f"Could not read the key store ({exc}). Paste a key instead."

    if isinstance(raw, list):
        for entry in raw:
            if isinstance(entry, dict) and isinstance(entry.get("token"), str) and entry["token"]:
                return entry["token"], None

    try:
        created = api_keys.create_key(label)
    except OSError as exc:
        return "", f"Could not mint a key ({exc}). Paste one from the dashboard."
    token = created.get("token")
    if isinstance(token, str) and token:
        return token, None
    return "", "Paste a key from the dashboard (Keys view)."


def auth_required(base_url: str, timeout: float = 4.0) -> Optional[bool]:
    """Whether the gateway demands a key. ``None`` when it cannot be reached.

    Three-valued on purpose. "No key" means two opposite things -- a gateway
    running with ``LINGLING_REQUIRE_KEY=0`` needs none, while a gateway with auth
    on and an empty keyring needs one that does not exist yet -- and telling the
    user the wrong one sends them to a session where every request 401s.
    """
    try:
        with urllib.request.urlopen(base_url.rstrip("/") + "/api/health", timeout=timeout) as resp:
            health = json.loads(resp.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, json.JSONDecodeError, OSError, ValueError):
        return None
    auth = health.get("auth") if isinstance(health, dict) else None
    if isinstance(auth, dict) and auth.get("required") is False:
        return False
    return True


def existing_key(base_url: str, timeout: float = 4.0) -> str:
    """The first key already on the dashboard, or ``""`` when there is none.

    Read-only on purpose. :func:`fetch_or_mint_key` *creates* a key when the
    keyring is empty, which no unattended path should do: it filled the setup
    window on a gateway whose Keys view read "No keys issued", and the token then
    outlived its keyring by surviving in ``settings.json``. Offering what already
    exists keeps issuing credentials a deliberate act in the dashboard.
    """
    if auth_required(base_url, timeout) is not True:
        return ""

    try:
        from core import config
    except ImportError:
        return ""

    try:
        raw = json.loads(Path(config.API_KEYS_FILE).read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return ""

    if isinstance(raw, list):
        for entry in raw:
            if isinstance(entry, dict) and isinstance(entry.get("token"), str) and entry["token"]:
                return entry["token"]
    return ""


def apply(base_url: str, token: str, model: str) -> Dict[str, Any]:
    """Do the whole job. Returns a result dict for the UI to render.

    Separated from the widgets so the command-line path and the tests exercise
    exactly the same code the button does.
    """
    existing, warning = load_settings()
    conflicts = stale_conflicts(existing)
    overrides = fetch_model_overrides(base_url)
    data = build_settings(existing, base_url, token, model, overrides=overrides)
    backup = write_settings(data)
    return {
        "settings_path": str(SETTINGS_FILE),
        "backup": str(backup) if backup else None,
        "conflicts": conflicts,
        "warning": warning,
        "model": model,
        "base_url": base_url.rstrip("/"),
        "keyless": not token,
    }


# ---------------------------------------------------------------------------
# The window
# ---------------------------------------------------------------------------
def run_gui() -> int:
    """Show the setup window. Returns a process exit code.

    tkinter ships with CPython on Windows, so this adds no dependency -- which
    matters for something meant to be double-clicked on a fresh machine.
    """
    try:
        import tkinter as tk
        from tkinter import messagebox, ttk
    except ImportError:
        print("This build of Python has no tkinter. Use the command line instead:")
        print("  python setup_claude_code.py --model deepseek-v4-flash-free")
        return 2

    root = tk.Tk()
    root.title("Lingling -- Claude Code setup")
    root.resizable(False, False)

    frame = ttk.Frame(root, padding=16)
    frame.grid(sticky="nsew")

    ttk.Label(
        frame,
        text="Point Claude Code at Lingling",
        font=("Segoe UI", 13, "bold"),
    ).grid(row=0, column=0, columnspan=3, sticky="w")
    ttk.Label(
        frame,
        text=("Pick a starting model, click Apply, then run  claude.\n"
              "Change model and thinking depth any time with /model and /effort."),
        foreground="#555555",
        justify="left",
    ).grid(row=1, column=0, columnspan=3, sticky="w", pady=(2, 12))

    # -- gateway ---------------------------------------------------------
    ttk.Label(frame, text="Gateway").grid(row=2, column=0, sticky="w", pady=4)
    url_var = tk.StringVar(value=DEFAULT_BASE_URL)
    url_entry = ttk.Entry(frame, textvariable=url_var, width=32)
    url_entry.grid(row=2, column=1, sticky="we", pady=4)

    # -- model -----------------------------------------------------------
    ttk.Label(frame, text="Starting model").grid(row=3, column=0, sticky="w", pady=4)
    model_var = tk.StringVar()
    model_box = ttk.Combobox(frame, textvariable=model_var, width=30, state="readonly")
    model_box.grid(row=3, column=1, sticky="we", pady=4)
    ttk.Label(
        frame, text="/model switches it later, per session",
        foreground="#777777", font=("Segoe UI", 8),
    ).grid(row=4, column=1, sticky="w")

    # -- key -------------------------------------------------------------
    # The user pastes a key from the dashboard if needed. No key is auto-fetched
    # or shown -- an auto-filled token in a screenshot looks like a leak.
    ttk.Label(frame, text="API key").grid(row=5, column=0, sticky="w", pady=4)
    token_var = tk.StringVar()
    token_entry = ttk.Entry(frame, textvariable=token_var, width=32, show="*")
    token_entry.grid(row=5, column=1, sticky="we", pady=4)
    ttk.Label(
        frame, text="paste a key from the Keys tab",
        foreground="#777777", font=("Segoe UI", 8),
    ).grid(row=6, column=1, sticky="w")

    status = tk.Text(frame, height=9, width=62, wrap="word", relief="flat",
                     background="#f4f2ee", foreground="#333333")
    status.grid(row=8, column=0, columnspan=3, sticky="we", pady=(12, 8))
    status.configure(state="disabled")

    def say(text: str) -> None:
        status.configure(state="normal")
        status.delete("1.0", "end")
        status.insert("1.0", text)
        status.configure(state="disabled")

    def refresh() -> None:
        """Repopulate the model list and fetch a key, from whatever gateway is named."""
        ids, error = fetch_models(url_var.get())
        if error:
            model_box["values"] = []
            say(f"{error}\n\nStart it with:\n  cd backend\n  python app.py\n\n"
                f"then click Refresh.")
            return
        model_box["values"] = ids

        # Prefer whatever is already configured, so Apply is idempotent and the
        # window reflects reality rather than resetting the user's choice.
        existing, _ = load_settings()
        current = existing.get("model")
        model_var.set(current if current in ids else ids[0])

        # Only a key that is live on the dashboard is offered. The settings file is
        # deliberately not consulted: it holds whatever a previous run wrote, so a
        # key revoked in the Keys view kept reappearing here and the field looked
        # populated on a gateway with none issued. No keys -> empty field.
        token_var.set(existing_key(url_var.get()))

        say(f"Found {len(ids)} model(s). Pick a starting model and click Apply.")

    def on_apply() -> None:
        model = model_var.get().strip()
        if not model:
            messagebox.showwarning("Pick a model", "Choose a model first, or click Refresh.")
            return
        try:
            result = apply(url_var.get().strip(), token_var.get().strip(), model)
        except OSError as exc:
            messagebox.showerror("Could not write settings", str(exc))
            return

        lines = [
            f"Done. Claude Code starts on {result['model']} at {result['base_url']}.",
            "",
            f"Wrote {result['settings_path']}",
        ]
        if result["backup"]:
            lines.append(f"Backed up your old file to {Path(result['backup']).name}")
        if result["keyless"]:
            # Two very different situations, and calling both "keyless" sent the
            # user to a session where every request 401s.
            if auth_required(url_var.get()) is False:
                lines.append("No key needed -- this gateway accepts unauthenticated calls.")
            else:
                lines.append(
                    "No key written, but this gateway requires one. Create a key in "
                    "the dashboard's Keys view, paste it above, and Apply again."
                )
        if result["warning"]:
            lines += ["", f"Note: {result['warning']}"]
        if result["conflicts"]:
            lines += ["", "Leftovers found on this machine:"]
            lines += [f"  - {c}" for c in result["conflicts"]]
        lines += [
            "",
            "Open a NEW terminal and run:  claude",
            "Inside it, /model and /effort change the model and thinking depth",
            "for that session -- Lingling translates whatever you pick.",
        ]
        say("\n".join(lines))

    buttons = ttk.Frame(frame)
    buttons.grid(row=7, column=0, columnspan=3, sticky="e", pady=(10, 0))
    ttk.Button(buttons, text="Refresh models", command=refresh).grid(row=0, column=0, padx=4)
    ttk.Button(buttons, text="Apply", command=on_apply).grid(row=0, column=1, padx=4)
    ttk.Button(buttons, text="Close", command=root.destroy).grid(row=0, column=2, padx=4)

    refresh()
    root.mainloop()
    return 0


def run_cli(argv: List[str]) -> int:
    """Headless path, for a machine with no tkinter or a scripted install."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Point Claude Code at Lingling by writing ~/.claude/settings.json.",
    )
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--model", help="the starting model; omit to list what is available")
    parser.add_argument(
        "--key", default=None,
        help="API key; omitted means ask the gateway for one",
    )
    args = parser.parse_args(argv)

    ids, error = fetch_models(args.base_url)
    if error:
        print(error)
        return 1
    if not args.model:
        print("Available models:")
        for mid in ids:
            print(f"  {mid}")
        print("\nRe-run with --model <id>.")
        return 0
    if args.model not in ids:
        print(f"{args.model!r} is not offered by {args.base_url}. Available: {', '.join(ids)}")
        return 1

    token = args.key
    if token is None:
        # The CLI keeps minting: it is an explicit, scripted invocation, and a
        # headless install has no window to paste a key into. The GUI does not,
        # because a field that fills itself with a brand-new credential on a
        # gateway showing no keys issued is indistinguishable from a leak.
        token, key_error = fetch_or_mint_key(args.base_url)
        if key_error:
            print(key_error)
            return 1

    result = apply(args.base_url, token, args.model)
    print(f"Wrote {result['settings_path']}")
    if result["backup"]:
        print(f"Backed up to {result['backup']}")
    for conflict in result["conflicts"]:
        print(f"note: {conflict}")
    print(f"\nClaude Code starts on {result['model']} at {result['base_url']}.")
    print("Open a new terminal and run: claude")
    print("Use /model and /effort inside the session to change model and depth.")
    return 0


def main() -> int:
    if len(sys.argv) > 1:
        return run_cli(sys.argv[1:])
    return run_gui()


if __name__ == "__main__":
    sys.exit(main())
