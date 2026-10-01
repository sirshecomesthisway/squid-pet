# squid-pet Privacy

Squid is a desktop pet that watches what you're doing so she can react.
This page tells you EXACTLY what she looks at, what she does NOT look at,
and how to turn any of it off.

## TL;DR

* Squid scans **filesystem metadata** (mtimes), **running process
  names**, and **CPU percentages**.
* Claude detection also reads a bounded transcript tail (up to 64 KiB)
  for record types and timestamps, to ignore background artifact ledger writes.
  This buffer can contain message text, but that text is not used, retained,
  or logged; only the resulting activity timestamp is cached.
* Squid never sends data anywhere, and
  never writes anything outside `~/.squid-pet/`.
* All scanning is **local-only**. No network calls. No telemetry.
* Every detector is **individually toggleable** via
  `~/.squid-pet/settings.json`.

## What each detector observes

Squid started life watching a third-party CLI coding agent — process
CPU, subagent files, `errors.log` content, shell children, and a private
sitecustomize.py-driven approval-alert signal. All of that was removed
2026-08-27: that agent was never actually installed/run in this
environment, so none of it ever fired anything in practice. Nothing
described below reads any of its files.

### Claude Code's flag wave — `scripts/claude_pet_hook.py`

(2026-08-26) The approval-needed "flag wave" alert, via Claude Code's
OFFICIAL hook system rather than process/file scanning. This is a
**separate execution path** from the detectors below: Claude Code
itself invokes `scripts/claude_pet_hook.py` as a subprocess, once per
registered event (`Notification`, `PermissionRequest`, `PermissionDenied`,
`PreToolUse`, `PostToolUse`, `PostToolUseFailure`, `UserPromptSubmit`, `Stop`, `StopFailure`,
`SubagentStop`, `PreCompact`, `PostCompact`, `SessionEnd`), per the `hooks`
block registered in `~/.claude/settings.json` (your own user-level
config, outside this repo).

| Reads (via stdin, from Claude Code itself) | What for |
|-------|----------|
| `session_id` | names the flag file; the only identifier Claude Code's hook payload provides (no PID) |
| `hook_event_name` | branches behavior: raise the wave on `Notification` (`permission_prompt` -- a prompt is actually on screen) and on `PreToolUse` for `AskUserQuestion`/`ExitPlanMode`; record a short-lived attribution hint on `PermissionRequest` (never a wave: it also fires for calls auto mode resolves itself); resolve the wave on `PostToolUse`/`PostToolUseFailure`/`PermissionDenied`/`Stop`/`StopFailure`/`UserPromptSubmit`/`SubagentStop`/`SessionEnd`; write the finished/failed/recap/turn flags on `Stop`/`StopFailure`/`PreCompact`/`UserPromptSubmit` |
| `tool_name` (PreToolUse events only) | only `AskUserQuestion`/`ExitPlanMode` raise the wave (they block on you without a permission prompt); every other tool name is ignored. Never the tool's arguments |
| `tool_use_id` (PermissionRequest events only, if present) | an opaque tool-call id, stored with that request's attribution hint (below) only if it is a plain id; not user content |
| `notification_type` (Notification events only) | only `permission_prompt` creates a flag; every other value, `idle_prompt` included, is ignored |
| `error_type` (StopFailure events only) | the error CATEGORY (e.g. `rate_limit`, `billing_error`) that ended the turn -- drives the concerned/warning sprite. A bounded enum, never message text |
| `agent_id` (any event) | an opaque subagent (Task-tool helper) identifier -- not user content. Tells a helper's events apart from the parent session's (main-thread events never carry one); written into the awaiting-input flag as that helper's owner line (below) so that its own later clearing event resolves only its own prompt. Used only if it is a plain id (letters, digits, `-`, `_`); anything else -- including an empty one -- is ignored and logged as `BAD_AGENT_ID` without the value |

`PermissionRequest` payloads also carry `tool_name`, `tool_input` and
`permission_suggestions`; the hook reads none of them -- only the fields in
the table above.

| Writes | What for |
|--------|----------|
| `~/.squid-pet/claude_awaiting_input/<session_id>` (content: the notification_type or tool name on the first line, then one owner line per prompt still pending in that session -- `parent` for the main session, `agent:<agent_id>` for a subagent -- each followed by the time it was added) | direct signal: this Claude Code session is waiting on you right now. Each prompt adds its own owner line, and each owner's own follow-up event (tool ran, request auto-denied, turn ended, you replied; `SubagentStop` for a subagent) removes only its own line, so one prompt resolving never clears another that is still waiting. Owners older than 2h are dropped on every update, and the file's modification time is its newest owner's. The flag is removed when its last owner resolves, on `SessionEnd`, or by the 2h stale sweep. The watcher's self-heal (only when a single Claude Code session is running, and only for a flag holding a single pending prompt) may also clear it when that session looks busy again -- so it can still clear one prompt that is in fact pending; it never clears a flag with two or more pending prompts. The hook, the self-heal and the 2h stale sweep serialize their updates with a lock file (`.lock`) in the same folder (it also guards the pending hints below). Owner lines hold only the opaque ids above and times -- never prompt or message text |
| `~/.squid-pet/claude_permission_pending/<session_id>` (one JSON line per recent `PermissionRequest`: the `agent_id` or null, the call's `tool_use_id` if the payload carries a plain one, and the time) | attribution hints, never a wave -- kept in their own folder so the watcher can never mistake one for a pending prompt. A real `Notification` carries no `agent_id`, so it uses the hints younger than 10s to decide whose prompt is up: every such hint is used up and adds its own owner line (the subagent's, or the main session's), so when several could be the one on screen, each keeps the prompt up until its own tool runs or its own turn ends -- nothing is guessed; with none, the prompt counts as the main session's. Accepted limitation: a prompt that fires no `PermissionRequest` of its own (e.g. a sandbox network prompt) while exactly one unrelated hint is live is attributed to that hint's owner. Closing it would mean never trusting a single hint, which would stop a subagent's own prompt from clearing on the subagent's own resolution; reviewers disagreed on its severity (one rated it an accepted, non-regressing edge case, another a serious bug), and it is kept deliberately. At most 8 per session; a hint is removed when used, when its caller's tool runs or fails or its request is auto-denied, when its caller's turn ends, on `SessionEnd`, and by the watcher's sweep of anything untouched for 5 minutes (a session that ended without `SessionEnd`). Opaque ids and times only -- never the tool name, its input, or any prompt text |
| `~/.squid-pet/claude_failed/<session_id>` (content: the error_type category) | direct signal: this session's turn ended on an API error (usage limit, overload, auth, billing, ...) -- shows "concerned". Never contains message text |
| `~/.squid-pet/claude_session_tty/<session_id>` (content: a `/dev/ttysNNN` string) | lets "take me there" raise the exact terminal/tab a signal came from, rather than guessing by working directory (which cannot tell apart two sessions in the same folder). A terminal device number only -- not the working directory, project name, or any content. Removed on `SessionEnd`, and swept after 2h as crash-safety (like the other flag dirs) so a session that died without firing `SessionEnd` cannot leak a stale entry |
| `~/.squid-pet/claude_hook.log` (one line per hook invocation, auto-truncated past 200KB) | lets you verify the hook is actually firing -- `tail -f` it while using Claude Code |

Does NOT read: the `message` field's human-readable text, `transcript_path`,
`cwd`, `prompt_id`, `last_assistant_message`, or any other field Claude
Code's hook payload includes beyond the fields above; does not read
transcript file contents,
prompt/response text, or tool call arguments/results. Never imports the
`squid_pet` package and has
zero dependencies beyond the Python stdlib, so a bug in squid-pet proper
can't affect it (or vice versa) -- it's wired up and torn down entirely
through `~/.claude/settings.json`.

To fill `claude_session_tty/` the hook runs `ps -Ao pid=,ppid=,tty=` once
per turn (on `UserPromptSubmit` only -- a session's terminal cannot change
without a new prompt) and walks the parent chain up to its own `claude`
process to read
that session's terminal (Claude Code spawns hooks without a controlling
terminal of their own). It requests only three process-table columns --
pid, parent pid, and tty -- never command lines, arguments, environments, or
any other process's details beyond those numbers.

Two self-heal reads in `watcher.py` back this mechanism, both metadata-only:
`StateMachine.compute()` deletes a flag outright the moment its own
independently-verified activity signal (real shell/file/streaming
evidence — no new external read, just its own already-computed state) sees
the session genuinely active again, covering the case where Claude
resumes on its own without a fresh `UserPromptSubmit` ever firing; and
(2026-08-27) when the OS notification fires, `psutil` walks the parent-
process chain of any running `claude` process (process names only — e.g.
`Terminal`, `iTerm2` — no cmdline args, no window titles) to find which
terminal app is hosting it, so `terminal-notifier`'s `-activate` can bring
the right app to the front on click instead of a generic/unhelpful target.

### ClaudeCodeDetector — observes the Claude Code CLI

| Reads | What for |
|-------|----------|
| `psutil.process_iter()` cmdline (basename `claude`) | finds the Claude Code CLI process — `Process.name()` was found unreliable for this binary on macOS, so matching goes through cmdline instead |
| CPU% of that process | diagnostic only (`squid why`) — not used to decide state |
| Non-shell descendant processes of `claude` (shared tool-name allowlist, also used by CodexDetector) | detects a live tool call (e.g. a Bash-tool command) → "working" |
| File mtimes under `project_dirs` (default `~/Projects`), same scan as IDEDetector | detects a very recent write (in-process tools like Edit/Write don't spawn a subprocess, so this catches what shell-child detection misses) → "working" |
| `~/.claude/projects/*/*.jsonl` and `~/.claude/projects/*/*/subagents/agent-*.jsonl` mtime plus up to 64 KiB of tail record types/timestamps | detects a recent transcript write → "thinking" (proxy for the LLM generating or a tool call resolving). The subagent pattern lets a Task-tool helper's own transcript keep her out of idle while it works; same content-blind tail read, never the `.meta.json` sidecar. For subagent transcripts the same tail read also reads each record's `type`, `message.stop_reason` and the *type* of its last content block only (never any text): a helper whose last record is a tool result, a tool call or a thinking block (not a final text block) is treated as still working for up to 25 minutes of silence, but only while its parent session's turn is in flight (the `claude_turn_active` flag) or the helper wrote after the parent's last Stop (the `claude_finished` flag's mtime, one stat); a last record too large for the 64 KiB tail counts as still working under the same limits |

Known risk: a helper killed by Esc or a crash leaves a transcript with no final
record. The pet releases it once the parent's turn closes with a Stop after the
helper's last write. StopFailure and SessionEnd write no Stop flag, so after
those a helper that last wrote after an earlier Stop stays held, up to the
25-minute limit. Claude Code appears not to fire Stop on a user interrupt (the
hook log shows turns reopened without a close), so after Esc the turn flag, and
with it the hold, can last until the next prompt or the session ends.

Transcript tails are read only when recently modified, and cached until
mtime/size changes. Background `artifact-autoreact-ledger` records do not
refresh activity; a preceding timestamp determines recency instead. Message
text in the temporary buffer is not used, retained, or logged. Unknown or
oversized records that cannot be decoded fall back to file mtime. A bounded
tail consisting entirely of ledger records contributes no activity.

Does NOT read: `~/.claude/` settings or credentials, or the contents of
any file under `project_dirs`.

Caching: the list of transcript files is cached for 60 seconds (same
pattern as GitDetector's repo-discovery cache); files untouched for
15+ minutes are dropped from the cache to keep it small over time.

### Codex approval hooks — `scripts/codex_pet_hook.py`

Codex invokes this advisory script with a JSON payload on stdin. It uses
`hook_event_name`, `session_id`, `turn_id`, `tool_name`, and `tool_input` to
match requests with their results. Shell/patch inputs are reduced to the
command before hashing; other tool inputs are hashed as JSON. Inputs can
contain command or question text, but none is logged or retained. The script
never reads the transcript, credentials, or files named by tool arguments.

Only SHA-256 request identifiers, reference counts, and permission-cohort
creation timestamps are persisted in `~/.squid-pet/codex_awaiting_input/`,
plus a lock for concurrent hook processes. Hidden `.permissions.*` files
count shell approval requests per session and turn. Squid reads the bounded
`ExecApproval` metadata prefix from `$CODEX_HOME/logs_2.sqlite` read-only
(default `~/.codex/logs_2.sqlite`): session/turn/request IDs, decision category,
and timestamp. The SQL projection truncates before policy/command payloads;
no tool output, transcript, prompt, or command content is fetched.

When all shell requests in that cohort have received decisions, Squid stops
waving on the next watcher tick, without waiting for commands to finish.
Other sessions/turns and input questions remain separate. Marker files stay
until completion to preserve concurrent request counts; terminal/turn events
remove them and stale markers expire. No generic shell activity clears a wait.
Missing/locked databases, unknown formats, incomplete log scans, and older
markers without cohort data conservatively retain completion-based behavior.
This adapter is verified against Codex 0.153.4's local log format, which is
not a stable public API. Non-shell approvals retain the hook-based fallback;
subagent hooks whose session IDs differ from decision-log IDs also fall back.
The frontend's polling interval adds to the normal ~1s watcher latency.
Auto-approved requests that emit no decision record can keep a mixed cohort
visible until its completion hooks arrive. The script emits no approval decision
or model instructions and sends nothing over the network.

The explicit setup command `scripts/install_codex_hooks.py` merges hook
configuration into `$CODEX_HOME/hooks.json` (default `~/.codex/hooks.json`) and
backs up the original as `hooks.json.squid-backup`. This setup operation is an
exception to the runtime's Squid-directory-only writes. Codex's `/hooks`
review/trust step is required; the installer never grants hook trust.

### CodexDetector — observes the Codex CLI

Same process/file signals as ClaudeCodeDetector, adapted to Codex's
on-disk layout. Codex transcripts remain mtime-only; no tail is read:

| Reads | What for |
|-------|----------|
| `psutil.process_iter()` cmdline (basename `codex` or `codex-tui`) | finds the native Codex binary — Codex's npm distribution runs a JS shim that spawns this as a child process; the shim itself is not matched |
| CPU% of that process | diagnostic only (`squid why`) — not used to decide state |
| Non-shell descendant processes of `codex`/`codex-tui` | detects a live tool call → "working" |
| File mtimes under `project_dirs`, same scan as IDEDetector | detects a very recent write (catches apply_patch-style edits that don't spawn a subprocess) → "working" |
| `~/.codex/sessions/**/*.jsonl` mtime (youngest across all sessions, nested by date) | detects a recent transcript write → "thinking" |

Does NOT read: transcript file contents, prompt/response text, tool call
arguments or results, `~/.codex/history/` prompt-recall content,
`~/.codex/` auth tokens or config, or the contents of any file under
`project_dirs`.

### Codex failed-turn signal — `watcher.codex_freshest_failure()`

When the Codex detector is enabled, Squid queries
`$CODEX_HOME/thread_history_1.sqlite` (default `~/.codex/thread_history_1.sqlite`)
using a read-only SQLite URI. This is a direct failure signal, never a guess
from a silent or stalled turn. No new hooks or network calls are involved.

| Fields examined inside SQLite | What for |
|-------|----------|
| `thread_turns.status` | requires exactly `failed`; running, interrupted, and completed turns cannot trigger concern |
| `thread_id`, `turn_id`, `started_at` | suppress a failure when another turn has started in the same thread; identifiers are not returned or persisted |
| `completed_at` | Unix seconds; select the freshest failure within five minutes, excluding future timestamps |
| `error_json` → `$.codexErrorInfo` | SQL maps a string enum or a known tagged-object key to a fixed, bounded category constant |

Only the normalized category leaves SQLite. Squid never selects the raw
`error_json`, `error.message`, `additionalDetails`, tagged-object payloads,
`thread_items`, or transcript contents. Unknown categories on an explicit
failed row yield a generic concern; malformed JSON or an incompatible schema
is a safe no-op. SQLite necessarily processes database pages and the JSON
container to extract the category; the application never receives that
container or its free text. The category determines the existing concerned
reason/severity presentation. Usage exhaustion is `hard`; rate limits,
server overload, and connection failures are `transient`. Pending approval
still takes priority, and the existing Claude failure path is preserved.

The connection uses `mode=ro`, zero busy timeout, a bounded query instruction
budget, and closes each tick. It performs no writes, migrations, or explicit
locking. SQLite's normal short read transaction sees live WAL rows;
`immutable=1` is deliberately avoided because it would miss those rows.
Any open/query failure returns no signal. Squid does not query `goals_1.sqlite`:
a goal status alone is not proof that a turn failed.

Validated against installed codex-cli **0.153.4** and the real `~/.codex`
schema on 2026-09-16. A throwaway invalid-model `codex exec` attempt timed
out after 40 seconds without a terminal row. A subsequent invalid-provider-URL
request produced genuine `failed` rows with `codexErrorInfo: "other"` from
both `codex exec` and standalone `codex app-server`; no model generation or
quota exhaustion was needed. Their `started_at`/`completed_at` values were
Unix seconds. Validation used an isolated temporary Codex home and working
directory, not project or existing conversation state.

Terminal exec persistence is verified. IDE coverage applies to app-server
clients writing this same local database; an actual IDE UI was not exercised.
Remote, ephemeral, custom database-location, and other-version coverage is
not guaranteed. This versioned schema is internal and may change; unsupported
schemas fail closed. Extremely large histories can exceed the query budget
and produce no signal; the real schema has no status/time index. Coverage
includes a synthetic 70,000-turn history. Synthetic-row tests exercise usage limits and other
categories without requiring live account failures.

### GitDetector — observes git activity

| Reads | What for |
|-------|----------|
| Walks `~/Projects/` (and any custom `project_dirs`) up to depth 4 | finds `.git/` directories |
| `.git/HEAD`, the current branch's ref file and `.git/packed-refs` mtimes | notices that the branch may have moved |
| `.git/HEAD` content (first 256 bytes), only when an mtime above changed | which branch is checked out |
| That branch's ref file (65 bytes), or its line in `packed-refs` (capped at 1 MiB) | its tip sha; a new sha on the same branch → celebrating (a new commit). Switching branches or a detached HEAD does not celebrate |
| `.git/index` mtime | detects active staging → busy |

The ref name and sha are held in memory only, never logged or written to
`state.json`; the celebrate reason never includes a branch name.

Does NOT read: commit messages, diffs, remote URLs, `.gitconfig`,
anything inside the working tree.

Caching: the list of `.git/` directories is cached for 60 seconds.
Hard caps: max 50 repos watched, max depth 4 from each project root,
prunes `node_modules/`, `.venv/`, `__pycache__/`, `dist/`, `build/`.

### TerminalDetector — observes shell activity

| Reads | What for |
|-------|----------|
| `psutil.process_iter()` for `zsh`, `bash`, `fish`, `sh` | finds open shells |
| `.children()` of each shell | detects non-shell children running >3s |

Does NOT read: command history, shell aliases, environment variables,
running command arguments, file paths being touched. Only names &
creation times.

The 3-second threshold prevents the shell prompt itself (which is a
brief child) from triggering false-positive busy states.

### IDEDetector — observes editor activity

| Reads | What for |
|-------|----------|
| `psutil.process_iter()` for `Code`, `Cursor`, JetBrains (`idea`, `pycharm`, `webstorm`, `rubymine`, `goland`, `clion`) | finds your editor |
| CPU% of those processes | aggregates editor load |
| File mtimes in `project_dirs` (default `~/Projects`) | detects recent edits / autosaves / grooving bursts |

Does NOT read: file contents, document text, open tabs list, IDE
settings, extension data, language-server traffic.

Walks at most depth 5 per project root and caps at 200 recent files
per scan to stay cheap.

## What's written to disk

Squid writes ONLY to `~/.squid-pet/`:

* `state.json` — current PetState snapshot (state, message, idle_seconds,
  timestamps). Overwritten ~1×/second. **Never contains file paths,
  commit hashes, or process arguments.**
* `settings.json` — your own preferences (stroll_mode, triggers.*).
* `logs/squid.log` — startup + lifecycle log.
* `pid` — the running daemon's PID (for the singleton lock).
* `lock` — file used by `fcntl.flock()` to prevent two Squids from
  running simultaneously. Empty.

That's it. Nothing else is created, modified, or read outside this
directory or the read-only directories listed above per-detector.

## What's sent over the network

**Nothing.** Squid has zero network code. She does not phone home.
She does not check for updates. She does not load images from URLs.
The window/wanderer load static SVGs bundled inside the package.

If you ever see Squid making a network connection, that's a bug —
please file an issue.

## Turning detectors off

Edit `~/.squid-pet/settings.json`:

```json
{
  "stroll_mode": "edges",
  "triggers": {
    "claude_code": true,
    "codex": true,
    "git": true,
    "terminal": false,
    "ide": true,
    "project_dirs": ["~/Projects", "~/work/repos"],
    "ide_processes": ["Code", "Cursor"]
  }
}
```

The removed agent's trigger is gone from this list too (its detector went
with it) -- the flag-wave alert is a separate mechanism with its own
on/off switch, `approval_alert_enabled` in `~/.squid-pet/config.json`
(default `true`), independent of the `triggers` block above.

Set any detector to `false` to disable it entirely (no scans, no
process iteration, no fs walks). Customize `project_dirs` if your
code lives somewhere other than `~/Projects`. Add/remove
`ide_processes` to match your editor.

## How to verify

Run `python -m squid_pet --why` (or `python -m squid_pet --why-json`)
to see exactly what each detector observed on the current tick and
what fired. The JSON output is suitable for piping into `jq` or saving
for later inspection.

If a detector is reporting something you don't expect, the verdict
line at the bottom of `--why` will tell you which signal fired.

## Questions?

Open an issue at https://github.com/sirshecomesthisway/squid-pet/issues.


### Codex silent-turn tracking

`UserPromptSubmit` records an opaque session/turn hash in
`~/.squid-pet/codex_turn_active/`. Each marker contains only the owning Codex
process ID, process creation time, and last lifecycle update timestamp.
The hook walks process ancestors and reads executable paths to find Codex;
it does not read ancestor command arguments. `PostToolUse` refreshes existing
markers; it cannot reopen a finished turn. `Stop` and `Interrupt` clear only
the matching turn; `SessionEnd` clears that session. A live marker supplies
`thinking` when shell/file/transcript activity is silent, below approval and
actual tool activity in priority, and prevents inactivity sleep.

Markers require a live matching process identity and expire after one hour
without a lifecycle update. PID reuse cannot revive them. Unavailable psutil,
inaccessible ancestry, or missing turn IDs disables this backstop while leaving
approval hooks operational. The documented Codex UserPromptSubmit payload has
a turn_id, but older/other hosts may differ. Hooks installed partway through a
turn cannot reconstruct its opening event; tracking starts with the next
UserPromptSubmit. A lost Stop while its owner stays alive can retain thinking
until the one-hour expiry; very long silent reasoning can exceed that bound.
Real UI approval latency, session/subagent lifecycle delivery, and hook
continuations after Stop remain local-Mac validation cases.


### Notification delivery

Queued approval notifications re-read the originating request/session markers
(and Codex decision metadata) immediately before dispatch, including before an
AppleScript fallback after a notifier error. A new approval episode cannot
revive the previous episode's queued notification. macOS controls delivery
once a notification has been submitted: AppleScript provides no request ID
with which Squid can retract its already-submitted banner. OS notification
queueing, Focus settings and audible/banner timing still need a real Mac check.


### Codex approval click targeting

Pending Codex requests now have an opaque `.owner.<request-hash>` companion
containing only the owning PID and process creation time. Completion removes it
with the final reference; turn/session cleanup removes matching companions.
Click targeting validates that identity, reads its live controlling TTY and
walks its executable/app ancestry. It does not store window titles or terminal
contents. Requests created before this update can use their exact turn marker
as identity evidence; there is no fallback to an arbitrary agent process.

While Codex requests are pending, approval double-clicks target the newest
Codex request. Terminal.app must expose an exact matching TTY tab before Squid
activates/selects anything. Unknown/exited owners, missing tabs and unsupported
hosts return `none` rather than activating an unrelated window. Other terminal
hosts need a future exact-tab adapter; Claude-only routing remains as before.
A resolved wave with no remaining Claude or Codex request also opens nothing.
Real multi-window Terminal focus and OS Automation permissions need a local
Mac check; fixtures verify the selected identity and generated AppleScript.

### Active-state click provenance

Each state snapshot carries an in-memory `focus_target` describing the
evidence that won that tick. For Codex shell work it contains only the owning
process ID and creation time; for a silent Codex turn it uses the same
process-bound turn marker, and transcript activity is attributed only when its
opaque transcript-path hash matches exactly one live turn marker. Project-file
writes and ambiguous shared transcript activity deliberately carry no target.

`take_me_there` consumes that snapshot rather than rescanning all live agents
after the click. Codex targets validate the process identity again and select
a matching Terminal.app TTY tab; Claude targets retain the existing app/tab
resolver for compatibility with Cursor, iTerm and other hosts. If provenance is absent, the owner has exited,
the PID was reused, or the host cannot provide exact tab selection, it returns
`none`; it never substitutes an unrelated Claude or Codex session. Generic
Git/IDE celebration and unsupported failure states likewise have no target.
