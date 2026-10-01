# Detector/state pipeline audit

Baseline: `origin/main` at `5ef116a`. This map was written before implementation.
It describes executable behavior; older comments/specs sometimes disagree.
The verification harness is a separate PR (#4), not included in this diff.

## Inputs and dependencies

| Input | Acquisition / ownership | Freshness and limits | Baseline effect |
|---|---|---|---|
| Claude CLI presence | psutil argv[0] basename `claude`; process name is unreliable on macOS | Every tick; argv cache keyed by PID/create time after 5s; cache evicts unseen PIDs | Enables rich agent cascade |
| Codex CLI presence | argv[0] `codex`/`codex-tui`; excludes headless subcommand only at argv[1] | Same cache; node shim is not the agent | Enables rich agent cascade |
| Agent child process | Recursive psutil descendants; fixed tool/runtime/shell name allowlist | Any matching live-looking child; no CPU/age gate; shell wrapper is command fallback | Working; command narration |
| Agent CPU | psutil nonblocking CPU percentage | First sample zero; exceptions usually ignored | Diagnostic only |
| Claude transcript | `~/.claude/projects/*/*.jsonl`, stat; bounded 64KiB tail ignores trailing artifact-autoreact-ledger records | Candidate age <=900s; nonempty discovery cache 60s; streaming age <20s; unchanged tails cached by mtime_ns/size | Thinking, working-hold continuation, turn stall backstop |
| Codex transcript | `~/.codex/sessions/**/*.jsonl`, stat only | Same 900/60/20s windows; no session/process association | Thinking/working hold; no completion edge |
| Project file writes | os.walk configured roots (default ~/Projects), mtimes only | Shared per-tick scan of <=30s ages; depth 5, first 200 matches; ignores .git, build/dist, venv, node_modules, caches, .remember | Agents: <=10s with process present -> working; IDE: <5s -> busy, >=5 files <=30s -> groove |
| Git metadata | Discover .git directories at depth <=4, max 50 repos, cache 60s; stat HEAD/index/refs/heads directory | <5s; celebration extended to now+configured 20s on each fresh observation | HEAD/refs -> celebrate; index -> busy; cannot prove commit vs checkout/worktree creation; worktree .git files ignored |
| Terminal child | psutil shells zsh/bash/fish/sh, direct non-shell child | Age >=3s; enabled false by default | Generic busy; background servers can satisfy it |
| IDE presence/CPU | psutil editor names (Code/Cursor/JetBrains) | Once per tick | Diagnostic only: edits count even without an IDE |
| macOS HID idle | Quartz CGEventSourceSecondsSinceLastEventType, fallback `ioreg` timeout 2s | Every tick; failures ->0 | Exposed diagnostic; sleep uses agent-idle instead |
| Claude permission/question | claude_pet_hook.py Notification(permission_prompt), PreToolUse(AskUserQuestion/ExitPlanMode) -> session-named flag | 2h disk expiry; 120s observation-based snooze | Approval override; baseline heuristic deletes after >=3s if aggregate working/thinking and <=1 Claude process |
| Claude wait clear | Same hook: UserPromptSubmit, PostToolUse, Stop, SessionEnd unlink session flag | Next tick | Ends approval; PostToolUse has only session-level correlation |
| Claude turn open/close | UserPromptSubmit writes claude_turn_active; Stop/SessionEnd remove | 1h stale cleanup; turn fallback requires newest transcript age <=180s | Thinking fallback, Git deferral, celebration/groove latches |
| Claude Stop | Writes claude_finished/session | <=celebrate_hold_sec (20s default), prune >2h | Groove only; no inferred task completion |
| Claude compaction | PreCompact writes claude_recapping, PostCompact/SessionEnd remove | <=120s, prune >2h | Thinking with recap reason |
| Explicit completion | squid_task_complete.py writes claude_task_complete/session (or unknown) | <=celebrate_hold_sec; prune >2h | Celebrate regardless of agent presence |
| Codex wait hook | PermissionRequest / PreToolUse(request_user_input), hashed session+turn+tool/input key with reference count | flock serialization and atomic replacement; prune >2h, snooze 120s | Approval; baseline shell-active heuristic deletes ALL aged waits |
| Codex async question | PostToolUse(request_user_input_async) accepted result -> hashed session/async/call-id marker | UserPromptSubmit or SessionEnd clears; Stop intentionally preserves async markers | Approval independent of synchronous turn |
| Codex clear | PostToolUse decrements matching count; Stop/Interrupt remove turn-prefix; SessionEnd session-prefix | No approval-granted event; tool-completion can lag approval | Ends approval when correlated lifecycle arrives |
| Settings | ~/.squid-pet/settings.json mtime hot reload; defaults Claude/Codex/Git/IDE on, terminal off | Rebuilds detector objects and currently resets state-machine latches | Enable/disable detectors, roots/editor names; explicit detector list never reloads |
| Config | ~/.squid-pet/config.json cached by mtime under lock | working hold 25s, celebrate hold 20s, turn stall 180s; alert enabled/sound/text | Timing and approval behavior; tool_active_window_sec currently unused by this pipeline |
| Wake / force | PetApi hold_awake_until max deadline; ~/.squid-pet/force_state file | Poke/sprint hold; periodic wake 900s cadence/180s duration; force has no expiry/validation | Wake suppresses sleep; force is final override |

All time arithmetic above uses wall-clock epoch seconds. Clock jumps and future
mtimes matter. Signal scans are best-effort, not atomic snapshots of all inputs.
No network calls are part of detection. OS dependencies: psutil/macOS process
permissions, Quartz/PyObjC, ioreg; notifications use terminal-notifier/osascript.
Hooks depend on installed/trusted user-level agent configuration and event
schemas. Claude metadata-tail parsing intentionally reads no message semantics.

## Baseline transition/priority map

Every ~1s plus computation time: hot reload -> `_compute_inner` -> agent-idle
tracking -> heuristic wait cleanup -> approval override -> force override.
There is no exhaustive transition table: each tick reevaluates ordered guards.
Any previous state can enter any output state when its guard becomes highest.

1. **Sleeping**: agent quiet >=315s, no merged shell/file/transcript signal,
   no explicit completion/Codex celebrate/manual celebrate hold, awake hold over.
   This early return currently ignores turn/recap and all generic detectors.
2. Track aggregate Claude turn edges: rising clears celebrated/pending latches;
   falling releases deferred generic celebration for configured hold duration.
3. **Celebrating**: active celebrate deadline, explicit completion, Codex
   `is_celebrating` (actually always false), or generic detector celebration.
   Generic celebration defers while ANY Claude turn is open. Latch suppresses
   later groove until next observed aggregate turn opening.
4. **Grooving**: fresh Claude Stop with no Claude shell/file activity, or generic
   groove, unless already celebrated this turn. It currently precedes Codex work
   and new Claude turn evidence.
5. **Thinking/recapping**: fresh recap marker when Claude detector enabled.
6. If either agent process present: **working** for shell/file (arms 25s hold),
   **working** during hold only with continuing streaming, **thinking** for
   streaming, **thinking** for open Claude turn with transcript age <=180s.
7. **Thinking** if any enabled generic detector busy, else **idle**.
8. **Approval_needed** overrides any cascade state for eligible direct waits;
   one notification per uninterrupted approval episode. Alert switch is separate
   from detector switches. Snooze hides the wave without answering the request.
9. Debug force overrides state and reason even over approval. `concerned` has no
   natural detector in this baseline (contrary to another local feature branch).

`_track_agent_idle` sees the cascade before approval/force: active states
thinking/working/grooving/celebrating/concerned reset quiet; other states start or
continue it. Awaken holds do not reset agent-idle. Production watcher catches
failed ticks and leaves the previous state visible; JSON writes use temp+replace.
GUI watcher additionally calls PetApi.update. Frontend polls ~800ms, maps approval
to attention artwork, and layers drowsy at 300s, sleeping at 315s, and stretch
on wake. Working artwork switches to pancake frames after continuous 3600s by
default. Observer speech is driven by transitions, with working reannouncement
15s/new-command paths. These presentation layers do not resolve signal identity.

## Risk inventory and investigation plan

| Scenario | Baseline risk | Audit disposition |
|---|---|---|
| Claude waits while Codex/editor works, or fresh transcript persists after prompt | Cleanup deletes a valid wait at 3s | Reproduce with real flags and real cascade |
| Codex A waits while B runs, or async question overlaps a tool in same session | All requests deleted by any shell child | Reproduce and prioritize explicit request lifecycle over aggregate inference |
| Process exits; its fresh transcript remains while other agent stays open | Departed agent's streaming leaks through merged signals | Reproduce exit boundary |
| Sleep followed by editor/Git activity, compaction, or a silent active turn | Early sleep guard hides valid inputs | Reproduce each omitted signal and exact threshold |
| Claude Stop from A while Codex B runs or new Claude turn starts | Groove outranks actual work | Reproduce concurrent and rapid-turn cases |
| New/revived transcript not in a nonempty cached candidate set | Up to 60s blind spot; short turn can be missed entirely | Investigate bounded cache invalidation without a full refactor |
| Expired/snoozed wait recreated between 1Hz samples under same name | No disappearance observed; new request remains snoozed | Reproduce marker-generation boundary |
| Child disappears between name/cmdline, process tree lookup fails, zombie remains | False working, or later session's child never examined | Reproduce process-race cases |
| Future-dated flag/transcript/project file | Arbitrarily extended freshness | Reproduce; ignore future evidence without deleting it |
| Readable JSON settings with wrong top-level/trigger types | Exception drops each tick or startup | Reproduce malformed configuration boundary |
| Git checkout/worktree reports fresh commit | mtimes cannot distinguish commit operation | Keep detection contract; make speech truthful, document ambiguity |
| Multiple Claude turns overlap | Global turn/celebration latches cannot attribute individual turn ends | Document attribution limit; no session-manager rewrite in this patch |
| Persistent MCP servers/build daemons, detached tools, fast tools <poll interval | Allowlist ancestry has FP/FN tradeoffs | Document real-Mac follow-up; avoid CPU guessing |
| File autosaves/build output outside ignored dirs; 200-file cap | Writer attribution impossible; recent evidence may be missed | Keep neutral reason and documented limits |
| Hook delivery races, missing SessionEnd after force-kill, denied process access | Missing/stale signals and partial process scans | Preserve explicit waits until lifecycle/snooze/expiry; document limits |

Regression results, selected fixes, final verification and remaining assumptions
are recorded below after reproductions; hypotheses above are not claims of bugs
fixed or real-world verification performed.

## Confirmed gaps and implemented corrections

`tests/test_pipeline_regressions.py` exercises the real detector/cascade with
controlled clocks, process seams, temporary marker files and the real Codex hook.
The first run failed 27 of 28 cases. Two further batches reproduced 3 child/bubble
failures and 3 Git clock-skew failures before their respective fixes. Boundary
and lifecycle controls bring the audit file to 55 cases, including the independent reviewer's
killed-Claude/open-turn reproduction and two first-transcript timing cases.

| Bug / observable failure | Smallest correction | Regression coverage |
|---|---|---|
| Any Claude/Codex work or stale streaming erased a still-pending wait | Remove aggregate-activity cleanup; keep correlated hook cleanup, snooze and expiry | Claude/Codex cross-agent and parallel work; 4s-old prompts; real async Codex hook -> Stop -> reply |
| Fresh transcript or leftover turn of an exited agent borrowed another agent's process presence | Require that detector's own process presence for streaming and Claude turn fallback | Both directions of Claude/Codex mixture; killed Claude with a turn marker |
| New prompt with absent/old transcript reported idle immediately | Let recent UserPromptSubmit count toward the turn-stall window | No transcript / 600s-old transcript, followed through stall expiry |
| New agent session hidden behind old transcript candidates | Invalidate candidate list when observed process PID set changes | Both agent types, real temporary transcripts |
| Early sleeping return swallowed generic activity, open turn, recap and Stop | Evaluate quiet/sleep fallback after all activity branches | Six signal sources at the 315s boundary |
| Claude Stop groove masked active Codex tools or the next Claude turn | Suppress Stop groove during a live turn and any agent's hard work; Codex streaming also counts as resumed work | Concurrent Stop + Codex shell, rapid new turn |
| Future timestamps stayed fresh until clock caught up; directories counted as waits | Ignore negative ages and nonregular signal entries; retain future flags on disk | Both transcript formats, project files, flags, HEAD/index/refs |
| Snoozed request removed/recreated entirely between samples never rearmed | Compare marker inode + nanosecond mtime as well as name | Both Claude and Codex marker families |
| Broken first process tree hid a later session's real tool | Catch C-layer SystemError per parent | First parent fails, second has pytest child |
| Child exited after name lookup but still latched work; zombies looked active | Latch after successful argv probe, skip zombies; retain name evidence for AccessDenied | NoSuchProcess race, zombie; existing denial tests retained |
| Native versioned Python process missed allowlist | Normalize numeric Python version suffixes | python3.13 child |
| Nonobject JSON settings/triggers raised instead of falling back | Validate object shape in detector factory | List/string/null settings or triggers |
| Metadata-only Git detection claimed an actual commit | Bubble now says `git activity!`; trigger semantics unchanged | Real GitDetector -> cascade -> Observer with HEAD touch only |

Established tests that required the unsafe cleanup were changed to require wait
preservation. Existing observer tests now assert the non-overclaiming bubble.
No common detector base class, session-manager rewrite, broad refactor, new
runtime dependencies, or new transcript-content reading was introduced.

Final priority differences: activity is evaluated before sleep; Stop groove
cannot override another agent's hard work or a new open turn. Explicit completion
and deferred generic celebration retain their previous priority. Direct approval
still overrides the cascade, and debug force still overrides approval.

## Tradeoffs and remaining assumptions

- **Approval lifecycle latency:** Codex has no grant-time hook in this integration.
  A granted long-running tool can keep its marker until PostToolUse/turn end;
  after 120s the wave snoozes. This is preferable to destroying an unrelated
  pending request. A future per-request execution-start signal could shorten it.
- **Claude hook granularity:** Claude Notification waits are session-keyed;
  PostToolUse clears the session marker. Parallel tools within ONE Claude session
  can still make that hook ambiguous. Cross-session activity no longer clears
  anything in the watcher. Full per-request Claude correlation needs an upstream
  request identity not currently supplied by this Notification integration.
- **Transcript attribution/cache:** processes and transcripts are not linked by
  session ID. A live idle CLI plus another session's recently touched transcript
  may still look active. New processes invalidate discovery, but a new transcript
  within the same process or revived old session can still wait for the 60s
  discovery window. PID reuse without an observed gap shares that limitation.
- **Process heuristics:** long-lived node/MCP/build children can look like tools;
  detached/reparented tools and calls shorter than the poll interval can be missed.
  Permanent argv caching assumes no late exec after the 5s settlement window;
  access-denied/partial process snapshots can miss activity. Codex headless
  detection still assumes its subcommand is argv[1], so global options before a
  headless subcommand need separate command-line parsing work.
- **Global turn state:** overlapping Claude turns share the turn-edge and
  celebration latches. Git's deferred beat releases when the last open turn ends,
  not necessarily when the session that caused it ends. Process termination has
  no proven task-success signal. Codex completion remains unimplemented, not
  inferred from silence.
- **Git/files:** commits are not proven by metadata. Linked worktrees (.git file),
  packed/nested refs, autosaves and generated files remain heuristic limitations.
  File scanning has depth and 200-match caps; no attribution of a write to an
  agent is claimed. No content/hash/ref-reading redesign is part of this patch.
- **Timing:** exact <20s transcript, <=20s completion, <=180s silence since the latest transcript or turn start,
  >120s snooze and >=315s sleep boundaries are pinned. Timers remain wall-clock
  based; backward jumps can extend in-memory holds. Future disk evidence is
  ignored until valid, so clock-skewed real activity can be missed temporarily.
  Inode+mtime cannot distinguish every same-timestamp rewrite and a reference-
  count rewrite can rearm a snooze; there is no persisted request generation ID.
- **Reload/debug:** settings rebuild still resets in-memory holds/latches; scalar
  shape validation does not validate every config value. Invalid force_state is
  still accepted. Notification latch remains once per uninterrupted approval
  episode, not one alert for every concurrent/new request. Notification subprocess
  timeouts are 3s each on a daemon thread; missed OS notification delivery does
  not remove the on-screen approval state.

## Verification and real-world macOS checklist

Baseline macOS/Python 3.13 suite: **931 passed** (84.33s), before new tests or code.
Run full pytest outside the restricted sandbox because existing `--why` tests
call CoreGraphics. Targeted regression runs use isolated state and no real prompts.
Final local verification: **986 passed in 58.08s**, including all **55** new
cases; focused detector/hook/cascade/observer suite **516 passed**. Ruff, mypy
(7 configured core modules), Cocoa audit and `git diff --check` pass. An
independent reviewer reproduced one additional killed-Claude/open-turn gap;
its regression and fix passed re-review with no remaining P1/P2 findings.
A controlled OS probe on **macOS 12.7.6 Intel / Python 3.13.13** successfully
read the macOS idle API, detected a real `python3.13` child and confirmed activity
cleared after that child exited. No user agent was stopped or signaled. The Cocoa
main-thread static audit passes. Live desktop/agent-session validation below is
still separate from these controlled checks.

Before release, use actual simultaneous Claude and Codex sessions on a logged-in
Mac and record macOS/chip/agent versions plus `python -m squid_pet --why-json` around each transition:

- Keep A waiting >3s while B runs tools, writes files, streams, finishes and exits;
  verify A keeps waving, matching reply clears only A, and Calm Squid never
  answers a prompt. Include async questions and two identical Codex requests.
- Approve/deny real commands with short and long runtimes; verify the documented
  grant-to-PostToolUse delay and the 120s snooze tradeoff are acceptable. Exercise
  macOS notification permission disabled/enabled, foreground switching and focus.
- Run long thinking (>20s), usage-limit silence (>180s), rapid Stop/new prompt,
  compaction, killed agent/terminal, a new session after a cached discovery and
  a session resumed in the same process. Compare pet state against agent UI.
- Leave Squid quiet for 315s, then trigger Git/editor work, compaction and a new
  agent turn. Confirm wake, state reason, drowsy/stretch artwork and no false
  success claim after checkout/worktree creation.
- Exercise direct Python, shell-wrapped tools, background MCP servers, detached
  children and process-access denial. Check CPU/latency under many sessions/repos.
- Repeat on supported Intel/Apple Silicon and older macOS versions, across sleep/
  wake and clock adjustment. Hosted CI cannot establish these UI/lifecycle facts.

The running user's pet has not been restarted or reconfigured by this audit.
These live-agent/UI observations remain unperformed unless explicitly recorded
in the PR; automated checks and controlled OS probes are not substitutes.
