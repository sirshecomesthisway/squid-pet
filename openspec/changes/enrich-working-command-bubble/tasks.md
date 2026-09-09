## 1. watcher.shell_child_activity — stop discarding the wrapper cmdline

- [x] 1.1 When walking a process's shell children, remember the last-seen
      wrapper child's cmdline (`SHELL_WRAPPER_NAMES` match with a non-empty
      cmdline) as a fallback
- [x] 1.2 Still return a non-wrapper child's cmdline immediately when found
      (preserves existing grandchild-caught behavior)
- [x] 1.3 When only wrapper(s) matched, return `(True, <wrapper cmdline>)`
      instead of `(True, None)`
- [x] 1.4 Preserve `(False, None)` when no shell child matched at all

## 2. observer — recover the command from the wrapper

- [x] 2.1 Add `_unwrap_eval_payload(cmdline) -> str | None`: for a `*sh -c`
      wrapper, extract the `eval '<CMD>' < /dev/null` payload (regex anchor),
      decode `\012` → newline and `'\''` → `'`; for a non-wrapper cmdline,
      return the args joined; return None when a wrapper has no eval anchor
- [x] 2.2 Add `_parse_command(payload) -> tuple[str | None, str | None]`:
      split on `&& || | ; \n`, skip noise-leader segments
      (`cd pushd export source . set setopt unset unalias builtin eval`),
      return `(what, where)` where `what` includes the subcommand for
      two-word tools and `where` is the first path-like arg (skips flags and
      message-flag values `-m/--message/-c/-C`)
- [x] 2.3 Add `_looks_like_path(tok)` helper (`/` present or `\.\w{1,5}$`)

## 3. observer._shell_cmd_bubble — new shape

- [x] 3.1 Rebuild `_shell_cmd_bubble` on the helpers above to emit
      `runs {what}[ in {where}]`, agent name dropped
- [x] 3.2 Graceful target truncation: full path → basename → parent dir → drop
- [x] 3.3 Return None (→ generic fallback) for empty cmdline, missing eval
      anchor, or noise-only payload
- [x] 3.4 Update the stale comment block above `_shell_cmd_bubble`
      (the "window.py always passes shell_cmdline=None" note is obsolete)

## 4. Tests

- [x] 4.1 Update existing assertions from `running X` to the new shape
      (`test_observer.py`, `test_shell_cmdline.py`, `test_detectors_claude_code.py`;
      `test_working_reannounce.py` uses mocked returns → no change needed)
- [x] 4.2 New: `_unwrap_eval_payload` on a real captured wrapper string
      (simple, chained-`&&`, piped, multi-line-`\012`)
- [x] 4.3 New: `_parse_command` path-like filter (commit message / grep
      pattern / package name excluded; real path included)
- [x] 4.4 New: noise-leader skipping (`cd src && ruff …`)
- [x] 4.5 New: truncation ladder (long path → basename → parent → dropped)
- [x] 4.6 New: `shell_child_activity` returns the wrapper cmdline when only
      wrappers matched, still returns the tool cmdline when a grandchild is caught
- [x] 4.7 New: unparseable wrapper → `_shell_cmd_bubble` returns None →
      `on_state_change` falls back to the reason line
- [x] 4.8 Full suite green (`pytest -q`)
