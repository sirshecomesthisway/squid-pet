# Design: Recover the command from the wrapper, render a short target-aware bubble

## The real wrapper format (captured live)

Walking this session's own Bash-tool process ancestry produced the exact
cmdline of the `zsh` wrapper (a direct child of `claude`):

```
/bin/zsh -c  source /Users/…/.claude/shell-snapshots/snapshot-zsh-….sh 2>/dev/null || true
             && setopt NO_EXTENDED_GLOB NO_BARE_GLOB_QUAL 2>/dev/null || true
             && { \builtin unalias -- 'unsetenv'; \builtin unset -f -- 'unsetenv'; } >/dev/null 2>&1 || true
             && eval '<REAL COMMAND>' < /dev/null
             && pwd -P >| /tmp/claude-XXXX-cwd
```

Two structural facts drive the design:

1. The real command is wrapped in **`eval '<CMD>' < /dev/null`**. This is a
   far more robust anchor than splitting on `&&`/`||` — the preamble is full
   of those, and the command itself may contain them too. We match between
   `eval '` and the terminating `' < /dev/null`.
2. Inside the payload, newlines are octal-escaped as `\012` and embedded
   single quotes as `'\''` (standard shell quoting). Both are decoded.

## Why the wrapper, not the tool grandchild

`shell_child_activity` currently returns the first *non-wrapper* child's
cmdline (the resolved `pytest` process) and only falls back to `(True, None)`
when it sees wrappers alone. The grandchild is a race: most Bash calls
finish in well under the 1 Hz scan interval, so it is usually gone. The
`zsh -c` wrapper, by contrast, is alive for the whole command. So:

- Keep returning a non-wrapper child cmdline when one is caught (preserves
  existing behavior and the fixtures that assert it).
- When only wrappers matched, return the **last-seen wrapper cmdline**
  instead of `None`. The presentation layer unwraps it.

The observer therefore receives one of two shapes and must handle both:

- a bare tool cmdline — `["pytest", "tests/", "-v"]`
- a wrapper cmdline — `["/bin/zsh", "-c", "source … eval '…' < /dev/null …"]`

`_unwrap_eval_payload` normalizes: for a `*sh -c` wrapper it returns the
eval payload; for anything else it returns the args joined as-is.

## Parsing: `_parse_command(payload) -> (what, where)`

Split the payload on shell separators (`&&`, `||`, `|`, `;`, newline) into
segments. Walk segments and skip any whose leading token is a **noise
leader** — `cd`, `pushd`, `export`, `source`, `.`, `set`, `setopt`, `unset`,
`unalias`, `builtin`, `eval`. The first non-noise segment defines the
command.

- `what` = leading token's basename; for a token in the two-word set
  (`git`, `brew`, `uv`, `pip`, `npm`, `yarn`, `pnpm`, `docker`, `kubectl`,
  `gcloud`, `aws`, `az`, `gh`, `go`, `cargo`) append the first non-flag
  argument as the subcommand.
- `where` = the first remaining argument that is **path-like** — contains
  `/` or matches `\.\w{1,5}$` (a file extension). Flags (`-x`) are skipped,
  and the value immediately following a message flag (`-m`, `--message`,
  `-c`, `-C`) is skipped so a commit message never lands in the slot.

### Why "path-like", not "first argument"

A generic "first argument after the verb" produces nonsense for the
majority of real commands (`in ripgrep`, `in fix drowsy beat`, `in build`,
`in view`). CLI argument grammar is per-tool; there is no universal "where"
slot. Requiring a `/` or a file extension is a cheap, tool-agnostic proxy
for "this token names a location," and it degrades safely: when no argument
qualifies, the bubble is just `runs {what}` (still an improvement over
"ran a command").

## Rendering: graceful truncation under the 32-char cap

`MAX_BUBBLE_CHARS = 32` is a hard layout constraint (beyond it the bubble
wraps to two lines and breaks the sprite). With the name dropped, the
base `runs {what}` is short; the target is fitted by trying, in order:

1. the full path token,
2. its basename,
3. its parent directory + `/`,
4. drop the `in {where}` entirely.

The first candidate that fits within the cap wins. This is why
`pytest tests/test_observer.py` renders as `runs pytest in test_observer.py`
(31) while `python3 scripts/squid_task_complete.py` degrades to
`runs python3 in scripts/` (24).

## Fallback chain (no new failure modes)

`on_state_change` keeps its existing order:

1. working + shell_cmdline → `_shell_cmd_bubble` → return if non-None and
   ≤ cap.
2. otherwise → `_explain_reason(state_reason)` → the existing reason line
   ("claude ran a command", "claude edited a file", …).

`_shell_cmd_bubble` returns `None` (→ step 2) whenever it cannot produce a
meaningful bubble: empty cmdline, missing `eval` anchor, or a payload whose
only command is a noise leader. So a preamble change in a future Claude Code
release degrades to today's behavior instead of leaking internals — the same
defensive posture already documented for `_explain_reason`.

## Alternatives considered

- **Parse the shell snapshot / read the transcript for the exact command.**
  Rejected: violates the mtime-only design rule and is far more work for no
  extra reliability — the wrapper cmdline already *is* the exact command.
- **Keep the agent name, abbreviate elsewhere.** Rejected: the name costs
  ~7–11 chars, and it is the least informative part (it is almost always
  "claude"); dropping it is what makes a target fit at all.
- **Show "where" for any first argument.** Rejected: semantic nonsense for
  most commands (see above).
