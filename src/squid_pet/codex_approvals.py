"""Read-only Codex approval-decision evidence, separate from tool completion.

Codex 0.153 logs ExecApproval submissions as soon as the human answers, but
exposes no corresponding hook. Only that narrow metadata record is parsed;
commands, tool outputs, transcript contents and permission policy are not read.
Unknown/missing runtime formats retain the hook's completion-based fallback.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from contextlib import closing
from pathlib import Path
from time import time_ns as clock_ns

LOG_DB = Path(os.environ.get('CODEX_HOME', str(Path.home() / '.codex'))) / 'logs_2.sqlite'
TARGET = 'codex_core::session::handlers'
DECISION = re.compile(
    r'^session_loop\{thread_id=([\w-]+)\}: Submission sub=Submission \{ id: "[\w-]+", '
    r'op: ExecApproval \{ id: "([\w-]+)", turn_id: Some\("([\w-]+)"\), '
    r'decision: (?:Approved|ApprovedForSession|ApprovedExecpolicyAmendment|Denied|Abort)(?=[\s,({}])'
)


def digest(value: str) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def filter_resolved_requests(directory: Path, names: list[str]) -> list[str]:
    """Hide a turn's permission markers only when ALL its requests were answered.

    Keep marker files until PostToolUse: deleting them at approval would make a
    later identical concurrent call's completion incorrectly consume a new wait.
    Questions and other turns/sessions are never included in permission cohorts.
    """
    now = clock_ns()
    cohorts = []
    try:
        for path in directory.glob('.permissions.*'):
            if path.stat().st_size > 32768:
                continue
            data = json.loads(path.read_text())
            prefix = path.name.removeprefix('.permissions.') + '.'
            if not re.fullmatch(r'[0-9a-f]{64}\.[0-9a-f]{64}\.', prefix):
                continue
            if not isinstance(data, dict):
                continue
            since, total, keys = data.get('since_ns'), data.get('total'), data.get('keys')
            if (type(since) is not int or not 0 < since <= now or type(total) is not int
                    or total < 1 or not isinstance(keys, list) or not 1 <= len(keys) <= 128
                    or not all(isinstance(k, str) and k.startswith(prefix)
                               and re.fullmatch(r'[0-9a-f]{64}', k[len(prefix):]) for k in keys)):
                continue
            if set(keys).intersection(names):
                cohorts.append((prefix, since, total, set(keys)))
    except (OSError, ValueError):
        return names
    if not cohorts:
        return names
    try:
        since = min(c[1] for c in cohorts)
        with closing(sqlite3.connect(LOG_DB.as_uri() + '?mode=ro', uri=True, timeout=0.05)) as conn:
            conn.execute('PRAGMA query_only=ON')
            # Bound both work and result size. An incomplete scan fails conservative.
            conn.set_progress_handler(lambda: 1, 100_000)
            rows = conn.execute(
                # Truncate after the decision variant, before any policy/command payload.
                "SELECT ts, ts_nanos, substr(feedback_log_body, 1, "
                "instr(feedback_log_body, 'decision: ') + 37) FROM logs "
                "WHERE ts >= ? AND ts <= ? AND target = ? "
                "AND feedback_log_body LIKE 'session_loop{thread_id=%}: Submission sub=Submission { id: %, op: ExecApproval {%' "
                "ORDER BY id DESC LIMIT 2048", (since // 1_000_000_000, now // 1_000_000_000, TARGET),
            ).fetchall()
        answered: dict[str, dict[str, int]] = {}
        for seconds, nanos, body in rows:
            if type(seconds) is not int or type(nanos) is not int or not 0 <= nanos < 1_000_000_000:
                continue
            stamp = seconds * 1_000_000_000 + nanos
            if not since <= stamp <= now or not isinstance(body, str):
                continue
            match = DECISION.match(body)
            if match is None:
                continue
            session, request, turn = match.groups()
            prefix = digest(session) + '.' + digest(turn) + '.'
            bucket = answered.setdefault(prefix, {})
            # Duplicated records cannot count as a second answered request.
            bucket[request] = min(stamp, bucket.get(request, stamp))
        hidden = set()
        for prefix, since, total, keys in cohorts:
            count = sum(stamp >= since for stamp in answered.get(prefix, {}).values())
            if count >= total:
                hidden.update(keys)
        return [name for name in names if name not in hidden]
    except (OSError, ValueError, sqlite3.Error):
        return names
