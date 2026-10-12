# squid-pet — notes for coding agents

Read by any contributor's coding agent (Claude Code, Codex, others). Repo
facts only; the code is the source of truth. Human-oriented docs live in
`CONTRIBUTING.md` and `docs/`.

## Setup and checks

```bash
uv sync --dev --frozen --python 3.13
uv run --frozen pytest
uv run --frozen ruff check .
uv run --frozen mypy --config-file pyproject.toml
uv run --frozen python tools/verify_sprites.py
uv run --frozen python scripts/check_cocoa_main_thread.py src/squid_pet/*.py
```

These mirror CI's Python 3.13 leg (`.github/workflows/ci.yml`). CI also runs 3.11, the
lowest supported version: to check it, use `--python 3.11` in place of `--python 3.13`
above. All tests are pytest and must
not read the real `~/.claude/`, `~/.codex/` or `~/.squid-pet/` — inject I/O
and use temp dirs.

## Where things live

| Path | What |
|---|---|
| `src/squid_pet/watcher.py` | `StateMachine.compute()` — the 1 Hz state cascade; writes `~/.squid-pet/state.json` |
| `src/squid_pet/detectors.py` | Per-agent detectors (`Detector` protocol, `build_detectors()`) |
| `src/squid_pet/codex_approvals.py`, `codex_turns.py` | Codex approval / turn evidence |
| `src/squid_pet/window.py`, `passthrough.py`, `menu.py` | Cocoa window, click passthrough, menus |
| `src/squid_pet/frontend/index.html` | Single-file frontend (sprites, mood machine) |
| `scripts/claude_pet_hook.py`, `scripts/codex_pet_hook.py` | Agent hooks that write flag files |
| `scripts/squid_task_complete.py` | Explicit "task done" marker (below) |
| `bin/squid` | CLI: `start`/`stop`/`restart`/`status`/`logs`, `why` (explains the current state), `doctor` (end-to-end self-test), `update` |
| `openspec/` | Change proposals and specs |

## Gotchas

- **No hot reload.** The daemon is a long-lived launchd process; edited code
  does nothing until it restarts: `squid restart` (or
  `launchctl kickstart -k gui/$(id -u)/com.pink.squid-pet`). Only restart
  when you are verifying live behavior — it visibly kills and respawns the pet.
- **Check overrides before blaming detection.** `~/.squid-pet/force_state`
  pins the displayed state, has no expiry, and beats every detector. If the
  state looks stuck, check that file and `state_reason` in `state.json`
  first (`force_state override (...)` means it is not a bug).
- **Sleep/drowsy cannot be verified from inside an agent session.** Every
  command you run is a child of the agent, which keeps the pet "working" and
  resets its quiet clock. Test that logic with unit tests.
- **No JS test harness.** To test a decision table in `index.html`, slice
  the function out and run it under node with stubs that mirror the real
  guards (the mood-enter functions are idempotent; unguarded stubs give
  false failures). `tests/test_window_constants_agree.py` pins
  `watcher.IDLE_THRESHOLD_SEC` against the frontend's `DROWSY_IDLE_SEC` /
  `SLEEPING_IDLE_SEC` — change them together.
- **1 Hz performance budget.** The detector ticks once a second. Do not add
  a per-tick subprocess fork, an extra full `psutil.process_iter`, or another
  project-tree walk — flag the cost instead. Idle time comes from Quartz,
  not an `ioreg` fork. Process scans go through `watcher.iter_processes()`.
- **Background Cocoa loops leak without a pool.** Any worker-thread loop
  touching PyObjC must wrap each iteration in `objc.autorelease_pool()`
  (see `passthrough.py`, `window.py` and their `*_autorelease_pool` tests).
- **Codex fires no approval-grant hook.** Its hooks report the permission
  request and the command's completion, not the moment the user approves.
  `codex_approvals.py` reads the approval decision record from Codex's own
  log database (read-only, metadata only); if that format is unknown, the
  wave falls back to clearing at command completion. The hook also clears
  stale approvals when a new turn starts.
- **Four Claude Code surfaces.** The hook records each session's surface in
  `~/.squid-pet/claude_session_surface/<session_id>` from the transcript's
  top-level `entrypoint`; `state.json` (`claude_surfaces`) and `squid why`
  show it, and `squid doctor` reports the CLI version on each.
  - Terminal CLI (`entrypoint: "cli"`): fully watched; take-me-there raises
    the exact tab via the session's tty.
  - Desktop app Code tab (`"local-agent"`): the app runs its own bundled CLI
    (`~/Library/Application Support/Claude/claude-code/<ver>/`) with
    `CLAUDE_CONFIG_DIR` stripped, so it uses the real `~/.claude`, fires
    these hooks and writes a normal transcript — detection works as for the
    CLI. It has no tty: take-me-there activates the app
    (`com.anthropic.claudefordesktop`) and cannot pick a Code tab. Its
    bundled CLI lags the terminal CLI by many versions.
  - Desktop VM sandbox (`claude-code-vm`) and cloud / Code on the web:
    nothing local to see; out of scope, and squid makes no network calls.
  - `claude -p` writes `"sdk-cli"`. It and any unknown value take the
    terminal path.
- **Privacy.** Detectors read metadata only (mtimes, process names, CPU,
  transcript record `type` / `stop_reason` / content-block `type`, and the
  hook reads the top-level `entrypoint`; GitDetector also a ref name and sha,
  in memory only) — no message text or file contents, no network. See
  `docs/PRIVACY.md`.

## Task-complete marker

When a whole task (not just one turn) is done, run:

```bash
python3 scripts/squid_task_complete.py
```

It writes a marker under `~/.squid-pet/claude_task_complete/` that the
watcher treats as a real celebration trigger
(`claude_task_marked_complete_recently`), instead of guessing from silence.
Do not run it after intermediate turns.

## Commits

- Stage by pathspec only: `git commit -m "..." -- <files>`. Never
  `git add .` / `git add -A` — the tree may hold unrelated staged changes.
- Branch off `main`; push a new branch with `git push -u origin <branch>`
  right after its first commit.
- Do not bypass git hooks.
- Never add `Co-Authored-By` or "Generated with" attribution lines to commit
  messages or PR descriptions.
