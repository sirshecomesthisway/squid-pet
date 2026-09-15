"""
config.py -- user-facing settings persisted to ~/.squid-pet/config.json.

Currently tracks:
  - muted: bool -- suppresses all observer speech bubbles when True

Read with `get(key, default)`; write with `set(key, value)`. Writes are
atomic via temp-file + os.replace.

Reads are cached and only re-parse the file when its mtime changes -- the
watcher reads config several times per 1 Hz tick (see watcher.compute), so
re-opening + re-parsing the JSON on every call was a needless cost against
a project that otherwise works hard to keep the tick cheap. This mirrors
StateMachine._maybe_reload_settings's mtime-guarded reload for settings.json.
Cross-process edits (e.g. a menu toggle in another process) still land: that
process's set() rewrites the file with a new mtime, which this process's next
read detects and reloads.
"""
from __future__ import annotations

import json
import os
import pathlib
import threading
from typing import Any

CONFIG_DIR = pathlib.Path.home() / ".squid-pet"
CONFIG_FILE = CONFIG_DIR / "config.json"

# Sentinel distinguishing "caller passed no default" from "caller passed a
# falsy default" (0, "", False). Without it, get(key, False) would silently
# ignore the False and fall through to DEFAULTS.
_UNSET: Any = object()

# mtime-guarded parse cache. None == not yet loaded (distinct from a loaded
# empty dict). Guarded by _LOCK because get() runs on the watcher thread while
# set()/toggles run on the menu / JS-RPC thread.
_CACHE: dict[str, Any] | None = None
_CACHE_MTIME: float = 0.0
_LOCK = threading.Lock()

DEFAULTS: dict[str, Any] = {
    "muted": False,
    # post-e2e-polish 2026-06-27 Fix 1: celebrate hold duration in seconds.
    # Was 4s hard-coded in 3 places; bumped to 20s after Pink noted
    # she never sees Squid celebrate (window closed before she glanced).
    # Hot-reloadable via config.get() pattern. Valid range 4-60.
    "celebrate_hold_sec": 20,
    # post-e2e-polish 2026-06-27 Fix 6: how long after the last tool-call
    # write Squid stays in "working" before falling to "thinking". Was
    # 8s hard-coded; bumped to 20s after Pink noted Squid kept flickering
    # to "thinking" during LLM-generation gaps in active sessions. Hot-
    # reloadable. Set to 60+ for very long generation windows.
    "tool_active_window_sec": 20,
    # Fix 7: sticky working window -- bridges model-generation gaps
    "working_hold_sec": 25,
    # Pink-2026-09-15: how long a turn may stay open with a silent transcript
    # before the cascade stops calling it "thinking" and falls through to idle.
    # Guards against Claude Code hitting a usage limit (turn never closes, no
    # Stop hook) pinning her to "thinking" for up to an hour. See watcher.py's
    # branch 4c / TURN_STALL_SEC_DEFAULT.
    "turn_stall_sec": 180,
}


def _load_raw() -> dict[str, Any]:
    """Parsed config dict, re-read only when the file's mtime changes.

    Returns a shared dict -- callers read values out of it but must not
    mutate it in place (set() builds a fresh dict instead).
    """
    global _CACHE, _CACHE_MTIME
    with _LOCK:
        try:
            mtime = CONFIG_FILE.stat().st_mtime
        except OSError:
            # No file (or unreadable) -> empty config. Reset the cache so a
            # file that appears later is picked up on the next read.
            _CACHE, _CACHE_MTIME = {}, 0.0
            return _CACHE
        if _CACHE is not None and mtime == _CACHE_MTIME:
            return _CACHE
        try:
            with open(CONFIG_FILE) as f:
                data = json.load(f)
            if not isinstance(data, dict):
                data = {}
        except (json.JSONDecodeError, OSError):
            data = {}
        _CACHE, _CACHE_MTIME = data, mtime
        return _CACHE


def get(key: str, default: Any = _UNSET) -> Any:
    raw = _load_raw()
    if key in raw:
        return raw[key]
    if default is not _UNSET:
        return default
    return DEFAULTS.get(key)


def set(key: str, value: Any) -> None:  # noqa: A001 -- shadowing builtin OK in module
    global _CACHE, _CACHE_MTIME
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    merged = dict(_load_raw())
    merged[key] = value
    tmp = CONFIG_FILE.with_suffix(".json.tmp")
    with open(tmp, "w") as f:
        json.dump(merged, f, indent=2)
    os.replace(tmp, CONFIG_FILE)
    # Update the cache in-process so a get() in the same tick sees the write
    # regardless of filesystem mtime granularity.
    with _LOCK:
        _CACHE = merged
        try:
            _CACHE_MTIME = CONFIG_FILE.stat().st_mtime
        except OSError:
            _CACHE_MTIME = 0.0


def is_muted() -> bool:
    return bool(get("muted", False))


def toggle_muted() -> bool:
    """Flip the mute flag, persist, return new value."""
    new_val = not is_muted()
    set("muted", new_val)
    return new_val

def approval_alert_enabled() -> bool:
    """Approval-needed notification + sticky bubble (default ON)."""
    return bool(get("approval_alert_enabled", True))


def toggle_approval_alert() -> bool:
    """Flip the approval-alert flag. Returns new value."""
    new = not approval_alert_enabled()
    set("approval_alert_enabled", new)
    return new
