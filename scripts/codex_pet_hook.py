#!/usr/bin/env python3
"""Advisory Codex hooks: signal human waits without making approval decisions.

Install with install_codex_hooks.py, then review/trust through Codex /hooks.
Only opaque keys, counts, timestamps, and owning-process identity are persisted.
No prompt, command, answer, or tool output is logged. PermissionRequest lacks
a tool_use_id, so it is paired with PostToolUse by session, turn, tool, and input.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import sys
import time
from pathlib import Path


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def record_permission(flags: Path, prefix: str, key: str) -> None:
    """Count shell requests for decision reconciliation without persisting inputs.

    Called under the same lock as the markers. Retain the cohort through tool
    completion; reset once all its markers are gone so old decisions cannot
    answer a new request in the same turn.
    """
    ledger = flags / ('.permissions.' + prefix.rstrip('.'))
    try:
        data = json.loads(ledger.read_text())
        if (not isinstance(data, dict) or not isinstance(data.get('keys'), list)
                or type(data.get('total')) is not int or type(data.get('since_ns')) is not int
                or not any((flags / name).is_file() for name in data['keys']
                           if isinstance(name, str) and name.startswith(prefix) and '/' not in name)):
            data = {}
    except (OSError, ValueError):
        data = {}
    if not data:
        data = {'since_ns': time.time_ns(), 'total': 0, 'keys': []}
    data['total'] += 1
    name = prefix + key
    if name not in data['keys']:
        data['keys'].append(name)
    temp = ledger.with_name(ledger.name + '.tmp')
    temp.write_text(json.dumps(data))
    temp.replace(ledger)


def codex_owner() -> dict | None:
    """Find the actual agent ancestor, not the short-lived hook shell."""
    try:
        import psutil
    except ImportError:
        return None  # Approval hooks still work with a stdlib-only interpreter.
    try:
        for proc in psutil.Process().parents():
            try:
                if Path(proc.exe()).name in {'codex', 'codex-tui'}:
                    return {'pid': proc.pid, 'created': proc.create_time()}
            except (psutil.Error, OSError, SystemError):
                continue
    except (psutil.Error, OSError, SystemError):
        pass
    return None


def record_owner(flags: Path, name: str) -> None:
    owner = codex_owner()
    if owner is None:
        return
    path = flags / ('.owner.' + name)
    temp = path.with_name(path.name + '.tmp')
    temp.write_text(json.dumps(owner))
    temp.replace(path)


def update_turn(root: Path, session: str, turn, event: str, transcript=None) -> None:
    directory = root / 'codex_turn_active'
    directory.mkdir(parents=True, exist_ok=True)
    prefix = digest(session) + '.'
    if event == 'SessionEnd':
        for path in directory.glob(prefix + '*'):
            path.unlink(missing_ok=True)
        return
    if not isinstance(turn, str) or not turn:
        return
    path = directory / (prefix + digest(turn))
    if event in {'Stop', 'Interrupt'}:
        path.unlink(missing_ok=True)
    elif event == 'UserPromptSubmit' or (event == 'PostToolUse' and path.exists()):
        owner = codex_owner()
        if owner is not None:
            temp = path.with_name('.' + path.name)
            data = {**owner, 'updated': time.time()}
            if isinstance(transcript, str) and transcript:
                data['transcript_key'] = digest(transcript)
            temp.write_text(json.dumps(data))
            temp.replace(path)


def handle(payload: dict, root: Path) -> None:
    session = payload.get('session_id')
    event = payload.get('hook_event_name')
    turn = payload.get('turn_id')
    tool = payload.get('tool_name')
    if not isinstance(session, str) or not session:
        return
    if event not in {'PermissionRequest', 'PreToolUse', 'PostToolUse',
                     'Stop', 'Interrupt', 'SessionEnd', 'UserPromptSubmit'}:
        return
    if event == 'PreToolUse' and tool != 'request_user_input':
        return
    if event not in {'SessionEnd', 'UserPromptSubmit'} and (not isinstance(turn, str) or not turn):
        return
    flags = root / 'codex_awaiting_input'
    flags.mkdir(parents=True, exist_ok=True)
    # Serialize concurrent hook processes, including cleanup vs a new request.
    with (flags / '.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        update_turn(root, session, turn, event, payload.get('transcript_path'))
        prefix = digest(session) + '.'
        if event == 'UserPromptSubmit':
            # CLI async questions are answered through normal chat input.
            # A reply may start a new turn; leave separate tool approvals alone.
            for path in flags.glob(prefix + 'async.*'):
                path.unlink(missing_ok=True)
                (flags / ('.owner.' + path.name)).unlink(missing_ok=True)
            # A new main turn supersedes approval cohorts left by lost
            # Stop/PostToolUse hooks, but preserve the cohort for this turn.
            # A missing turn_id cannot establish ordering, so it clears none.
            current_prefix = prefix + digest(turn) + '.' if isinstance(turn, str) and turn else None
            current_ledger = ('.permissions.' + current_prefix.rstrip('.')
                              if current_prefix else None)
            if current_prefix:
                for path in flags.glob(prefix + '*'):
                    if '.async.' not in path.name and not path.name.startswith(current_prefix):
                        path.unlink(missing_ok=True)
                        (flags / ('.owner.' + path.name)).unlink(missing_ok=True)
                for path in flags.glob('.permissions.' + prefix.rstrip('.') + '.*'):
                    if path.name != current_ledger:
                        path.unlink(missing_ok=True)
            return
        if event == 'PostToolUse' and tool == 'request_user_input_async':
            response = payload.get('tool_response')
            if isinstance(response, str):
                try:
                    response = json.loads(response)
                except ValueError:
                    return
            call_id = payload.get('tool_use_id')
            if (isinstance(response, dict) and response.get('accepted') is True
                    and isinstance(call_id, str) and call_id):
                path = flags / (prefix + 'async.' + digest(call_id))
                record_owner(flags, path.name)
                temp = flags / ('.' + path.name)
                temp.write_text('1')
                temp.replace(path)
            return
        if event != 'SessionEnd':
            prefix += digest(turn) + '.'
        if event in {'Stop', 'Interrupt', 'SessionEnd'}:
            for path in flags.glob(prefix + '*'):
                path.unlink(missing_ok=True)
            for path in flags.glob('.owner.' + prefix + '*'):
                path.unlink(missing_ok=True)
            for path in flags.glob('.permissions.' + prefix.rstrip('.') + '*'):
                path.unlink(missing_ok=True)
            return
        if not isinstance(tool, str) or not tool:
            return
        tool_input = payload.get('tool_input', {})
        # PermissionRequest can add a description; Bash/apply_patch hooks
        # consistently expose the command across pre/permission/post events.
        if tool in {'Bash', 'apply_patch'} and isinstance(tool_input, dict):
            tool_input = tool_input.get('command')
            if not isinstance(tool_input, str):
                return
        key = digest([tool, tool_input])
        path = flags / (prefix + key)
        if event == 'PermissionRequest' and tool == 'Bash':
            record_permission(flags, prefix, key)
        try:
            count = int(path.read_text())
        except (OSError, ValueError):
            count = 0
        if event in {'PermissionRequest', 'PreToolUse'}:
            record_owner(flags, path.name)
            count += 1
        else:
            count -= 1
        if count <= 0:
            path.unlink(missing_ok=True)
            (flags / ('.owner.' + path.name)).unlink(missing_ok=True)
            ledger = flags / ('.permissions.' + prefix.rstrip('.'))
            if not any(flags.glob(prefix + '*')):
                ledger.unlink(missing_ok=True)
        else:
            # Atomic publication: the watcher never sees a partial flag.
            temp = flags / ('.' + path.name)
            temp.write_text(str(count))
            temp.replace(path)


def main() -> None:
    try:
        payload = json.load(sys.stdin)
        if isinstance(payload, dict):
            if payload.get('hook_event_name') == 'Stop':
                print('{}')  # successful Stop hooks require a JSON object
            handle(payload, Path(os.environ.get('SQUID_PET_HOME',
                                                str(Path.home() / '.squid-pet'))))
    except (OSError, ValueError, TypeError):
        # A pet integration must never block/approve/deny the agent's operation.
        pass


if __name__ == '__main__':
    main()
