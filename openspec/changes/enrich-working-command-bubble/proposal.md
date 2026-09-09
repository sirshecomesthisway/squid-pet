# Proposal: Enrich the working-state bubble with the real command + target

## Why

The working-state speech bubble is supposed to say *what* Squid's agent is
doing. In practice it almost always shows the generic fallback **"claude ran
a command"** — which carries no more information than the sprite's working
animation already does. Pink's report: *"it is ambiguous and not giving me
any insight. Even 'edited a file' is better."*

The concrete-command path (`_shell_cmd_bubble` → "running pytest") already
exists but is **starved of input**, for two independent reasons discovered
during investigation:

1. **`watcher.shell_child_activity` discards the wrapper cmdline.** Claude
   Code runs every Bash-tool call as
   `zsh -c 'source <snapshot> … && … && eval '\''<REAL CMD>'\'' < /dev/null …'`.
   The activity walker latches `shell_active = True` on that `zsh` wrapper
   (so the state becomes `working`) but then, because `zsh` is in
   `SHELL_WRAPPER_NAMES`, returns `(True, None)` unless it *also* catches the
   real tool (`pytest`, `git`) as a separate live grandchild at that exact
   1 Hz scan tick. That grandchild is short-lived and usually raced away, so
   the cmdline is almost always `None`.

2. **Even given the wrapper cmdline, the parser would extract the wrong
   word.** `_shell_cmd_bubble`'s `sh -c` unwrap takes `split()[0]` of the
   script, which for Claude's wrapper is `source` (the snapshot preamble),
   not the real command.

The key realization: the `zsh -c` **wrapper is a direct child of `claude`
and lives for the entire command duration** (confirmed by walking this
session's own process ancestry: `zsh → claude`). Reading the wrapper's
cmdline is *reliable* exactly where catching the tool grandchild is not. So
recovering the command from the wrapper both (a) adds the "what/where" Pink
wants and (b) is *why the bubble will finally populate* instead of falling
to the generic line.

## What changes

Recover the real command from the wrapper and render a richer, shorter
bubble. The agent name is dropped (it is almost always "claude" and the
sprite already implies it), which is what buys the character budget for a
target.

**New bubble shape (working state, shell command):**

```
runs {what}[ in {where}]
```

- `{what}` = the command verb, plus the subcommand for known two-word tools
  (`git push`, `brew install`, `npm run`, `uv run`).
- `{where}` = the first **path-like** argument (contains `/` or has a file
  extension), or omitted entirely. This filter is deliberate: it keeps
  commit messages (`git commit -m "…"`), grep patterns (`grep foo src/`),
  and package names (`brew install ripgrep`) *out* of the "where" slot,
  where a naive "first argument" would print nonsense.

Rendered examples (all verified ≤ 32 chars against the real preamble):

| Real command | Bubble |
|---|---|
| `pytest tests/test_observer.py -v` | `runs pytest in test_observer.py` |
| `git push origin main` | `runs git push` |
| `git commit -m "fix drowsy beat"` | `runs git commit` |
| `grep -rn shell_cmdline src/squid_pet/` | `runs grep in src/squid_pet/` |
| `cd src && ruff check squid_pet/observer.py` | `runs ruff in observer.py` |
| `brew install ripgrep` | `runs brew install` |
| `rm -rf dist/` | `runs rm in dist/` |

Mechanism:

1. **`watcher.shell_child_activity`** — when only wrapper shells matched
   (no reportable non-wrapper child), return the wrapper's cmdline instead
   of `None`, so the presentation layer receives something to unwrap.
2. **`observer` (new helpers)** — `_unwrap_eval_payload(cmdline)` extracts
   the `eval '<CMD>' < /dev/null` payload from a `zsh -c` wrapper (decoding
   `\012` newlines and `'\''` quotes); `_parse_command(payload)` skips
   leading noise leaders (`cd`, `export`, `source`, …) and returns
   `(what, where)`.
3. **`observer._shell_cmd_bubble`** — rebuilt on top of the two helpers to
   emit `runs {what}[ in {where}]` with graceful truncation (full path →
   basename → parent dir → drop the "where"). Used by both the working
   transition (`on_state_change`) and the reannounce (`on_still_working`),
   so the two stay consistent.

**Defensive fallbacks (no regressions when parsing fails):**

- A non-wrapper cmdline (the grandchild-caught case, and every existing
  test fixture) is parsed directly — behavior for those inputs is preserved
  except for the intended `running X` → `runs X[ in Y]` wording.
- If the `eval` anchor is absent (a future Claude Code version changes the
  preamble), `_unwrap_eval_payload` returns `None` and the bubble falls back
  to the existing `_explain_reason` line rather than emitting garbage.
- If the payload's only command is a noise leader, `_shell_cmd_bubble`
  returns `None` → generic fallback.

**Explicitly out of scope:**

- **Codex** is left on its current generic line. Its shell-invocation
  wrapper format has not been captured (no Codex process was running during
  investigation); guessing it risks emitting nonsense. A follow-up change
  gates the Codex branch on a captured real wrapper.
- **The Edit/Write "edited a file" bubble** is unchanged. The edited file's
  real path is not available from the mtime-only, multi-project file scan
  (`_scan_recent_file_ages` returns ages, not paths, and cannot attribute an
  edit to a specific session), and reading it from the transcript violates
  the standing "mtime only, never read transcript content" design rule.

## Impact

Affects the `observer-mode` capability:

- **MODIFY** "Enriched working bubble reports the live shell command" — the
  bubble now recovers the command from the `zsh -c` wrapper, drops the agent
  name, and appends a path-like target.

Code: `src/squid_pet/watcher.py` (`shell_child_activity`),
`src/squid_pet/observer.py` (`_shell_cmd_bubble` + two new helpers).
Tests: existing shell-bubble assertions update from `running X` to the new
shape; new tests cover wrapper unwrapping, the path-like filter, noise-leader
skipping, and truncation.
