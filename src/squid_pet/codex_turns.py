"""Opaque, process-bound Codex turn markers; no transcript content reads."""
from __future__ import annotations

import json
from pathlib import Path

import psutil

TURN_DIR = Path.home() / '.squid-pet' / 'codex_turn_active'
STALE_SEC = 3600.0


def owner_alive(data: dict) -> bool:
    """Creation time prevents a recycled PID from reviving an abandoned turn."""
    try:
        pid, created = data.get('pid'), data.get('created')
        if type(pid) is not int or pid <= 0 or type(created) not in (int, float):
            return False
        proc = psutil.Process(pid)
        return proc.create_time() == created and proc.is_running() and proc.status() != psutil.STATUS_ZOMBIE
    except (psutil.Error, OSError):
        return False


def active_turns(now: float) -> list[dict]:
    active: list[dict] = []
    try:
        for path in TURN_DIR.glob('[!.]*'):
            try:
                if path.stat().st_size > 4096:
                    continue
                data = json.loads(path.read_text())
                if not isinstance(data, dict):
                    continue
                updated = data.get('updated')
                if isinstance(updated, bool) or not isinstance(updated, (int, float)) or not 0 <= now - updated <= STALE_SEC:
                    continue
                if owner_alive(data):
                    active.append({**data, 'key': path.name})
                else:
                    # A dead owner's marker can never become valid again.
                    path.unlink(missing_ok=True)
            except (OSError, ValueError):
                continue
    except OSError:
        pass
    return active


def turn_in_flight(now: float) -> bool:
    return bool(active_turns(now))
