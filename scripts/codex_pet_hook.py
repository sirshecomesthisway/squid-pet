#!/usr/bin/env python3
"""Advisory Codex hooks: signal human waits without making approval decisions.

Install with install_codex_hooks.py, then review/trust through Codex /hooks.
Only opaque request keys and reference counts are persisted. No prompt, command,
answer, or tool output is logged. PermissionRequest lacks a tool_use_id, so it
is paired with PostToolUse by session, turn, tool, and normalized input.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import sys
from pathlib import Path


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


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
        prefix = digest(session) + '.'
        if event == 'UserPromptSubmit':
            # CLI async questions are answered through normal chat input.
            # A reply may start a new turn; leave separate tool approvals alone.
            for path in flags.glob(prefix + 'async.*'):
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
                temp = flags / ('.' + path.name)
                temp.write_text('1')
                temp.replace(path)
            return
        if event != 'SessionEnd':
            prefix += digest(turn) + '.'
        if event in {'Stop', 'Interrupt', 'SessionEnd'}:
            for path in flags.glob(prefix + '*'):
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
        try:
            count = int(path.read_text())
        except (OSError, ValueError):
            count = 0
        if event in {'PermissionRequest', 'PreToolUse'}:
            count += 1
        else:
            count -= 1
        if count <= 0:
            path.unlink(missing_ok=True)
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
