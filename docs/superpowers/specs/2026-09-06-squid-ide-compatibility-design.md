# Squid IDE compatibility — design

Date: 2026-09-06
Status: approved for implementation (Phase 1); Phase 2 is a researched direction

## Problem

Squid's action triggers — capturing work, showing/switching status, and
raising approval requests — are built almost entirely around the **Claude
Code (and Codex) CLI**. The rich state cascade
(`watcher.py` `_compute_inner`) only enters its working/thinking branch when
`any_agent_running` is true (`watcher.py:1598`), and approval is driven
solely by the CLI hook protocol (`scripts/claude_pet_hook.py` →
`~/.squid-pet/claude_awaiting_input/`). Users driving work through an IDE —
Cursor, VS Code, JetBrains, Zed — get a much flatter, sometimes wrong,
experience, and in some setups squid does nothing at all.

This matters for the public launch: IDE users are a large share of the
audience, and squid should behave **honestly** for them (never imply agent
states it cannot see) even where it cannot achieve full parity.

## What Squid can and cannot monitor (capability matrix)

This is the load-bearing section: the honest boundary of the tool. "Can" =
squid observes it and reflects it in state. "Partial" = it can infer
*something* but not the rich/exact signal. "Cannot" = no signal exists.

### By work source

| Work source | Capture work | Rich status (working/thinking/celebrating) | Approval request | "Take me there" focus |
|---|---|---|---|---|
| **Claude Code CLI** — Terminal.app | Can | Can | Can | Can (exact tab) |
| **Claude Code CLI** — in Cursor/VS Code/other integrated terminal | Can (hooks are global) | Can | Can | Partial (app-activate only; no tab/window precision) |
| **Codex CLI** | Can | Can (via CodexDetector) | Cannot (approval hook is Claude-only today) | Partial (app-activate) |
| **Claude Code IDE extension** (VS Code/Cursor/JetBrains) | Can (hooks fire in the extension — see Phase 2 note) | Can | Can | Partial (activates the IDE) |
| **Cursor Composer / native agent** | Partial (file writes only) | Cannot | Cannot | Partial (activate Cursor) |
| **VS Code Copilot / Cline / Continue / other agents** | Partial (file writes only) | Cannot | Cannot | Partial (activate app) |
| **Manual coding, no agent** | Partial (file writes only) | Cannot (only generic "busy") | n/a | Partial |
| **Remote-SSH / dev container / WSL / cloud IDE** | Cannot (no local process tree, file writes not local) | Cannot | Cannot | Cannot |

### Why the "Partial"/"Cannot" cells exist (root technical reasons)

- **File-write capture is IDE-agnostic but narrow.** `IDEDetector.is_busy`
  reduces to "a file under a `project_dirs` root changed in the last 5s"
  (`detectors.py:886` — the CPU/`ide_processes` term is a tautology). Any
  editor that writes files is captured; work that does not write into a
  watched root (reading, browsing, editing elsewhere, remote FS) is not.
- **Rich status needs a CLI agent.** The working/thinking/celebrating/
  recapping cascade is gated on `any_agent_running` (`watcher.py:1598`) plus
  the CLI hook flags. No hook → at most generic `grooving`
  (>5 files/30s) or a generic "busy → thinking" fallback
  (`watcher.py:1643`).
- **Approval is CLI-hook-exclusive.** `approval_needed` comes only from
  `permission_prompt` Notifications and the `PreToolUse`
  (`AskUserQuestion`/`ExitPlanMode`) hook, filtered on a live `claude`
  process. No IDE-native agent exposes an approval signal squid can read.
- **Focus is host-app + tty based.** Exact tab-raise is Terminal.app only
  (AppleScript tab addressing); every other host is app-activate only. A
  host missing from `_TERMINAL_APP_BUNDLE_IDS` (`watcher.py:374`) raises
  nothing even when a valid tty is known.
- **Everything assumes local execution.** Process-chain walking, tty
  resolution, and file-mtime scanning are all local-machine; remote/
  container/cloud setups break all three.

Phase 1 does not change these boundaries — it makes squid **behave
honestly within them** and stops two outright breakages (focus gaps, dead
CPU logic). Phase 2 (B) extends the "Can" column to the Claude Code IDE
extension. Broadening to third-party IDE agents is explicitly future work
(Phase 3 / approaches A/C), not covered here.

## Phase 1 — pre-launch triage

Scope: no new integration surface; no behavior regression to the CLI
experience; each item independently shippable and tested.

### 1.1 Host-app resolution for *any* app Claude runs under  — DONE 2026-09-06
- **Approach (refined from the original "grow the hardcoded list"):**
  resolve the host app's bundle id **dynamically**. `find_terminal_app_
  bundle_for_claude_code` (`watcher.py`) walks Claude's parent chain and,
  for each ancestor not in the `_TERMINAL_APP_BUNDLE_IDS` fast-path, reads
  the real `CFBundleIdentifier` from the enclosing `.app`'s Info.plist via
  the new `_bundle_id_from_exe_path()`. Any host (Terminal, iTerm, Cursor,
  VS Code, JetBrains, Zed, Windsurf, …) is now focusable with **no guessed
  bundle ids and no per-app list growth**.
- **Why dynamic beats the list:** the original plan required hardcoding
  bundle ids for apps not installed here (unverifiable, drift-prone).
  Reading the app's own Info.plist is always correct. The hardcoded map is
  kept only as a fast-path and as the Terminal.app exact-tab anchor.
- **Tests:** unlisted host (JetBrains) resolved via a real tmp `.app`
  Info.plist; outermost-`.app` selection for nested Helper.app exes;
  None for non-app exes. The old "detection↔map parity" guardrail is moot —
  resolution no longer depends on the map.
- **Note on precision:** non-Terminal hosts remain app-activate only
  ("app-only"); tab/window precision stays Terminal.app-only by design.

### 1.2 Make IDE detection honest (kill the dead CPU gate)
- **Change:** `IDEDetector.is_busy` (`detectors.py:886`) is
  `has_recent_file and (cpu_busy or cpu < threshold)` — the second clause is
  always true. Remove the CPU/`ide_processes` machinery from the busy
  decision (keep `cpu_percent` in `diagnostic()` only if useful, else drop),
  and rewrite the class docstring's "D3" rules to state the real behavior:
  **busy = a project file changed within `RECENT_FILE_WINDOW_SEC`.**
- **Rationale:** the current list/threshold imply IDE-process awareness the
  code does not have; removing them prevents wrong expectations and dead
  code. Behavior is unchanged (the decision already equals recent-file).

### 1.3 Configurable, multi-root project dirs — DONE 2026-09-06 (core)
- **Finding:** multi-root is **already supported end-to-end** —
  `project_dirs` is a list threaded through `build_detectors` →
  `IDEDetector` → `_scan_recent_file_ages` (which walks every root). Pinned
  with a test (a recent write in the SECOND configured root fires busy).
- **DECISION PENDING — broaden the default?** Default stays single
  `~/Projects` (`detectors.py`). Broadening it (e.g. also `~/dev`, `~/code`,
  `~/src` when they exist) would capture more IDE users out-of-the-box, but
  each extra root is another per-tick tree walk — a real risk to the idle
  CPU metric (1.7%) just landed. Recommendation: keep single-root default,
  document the setting, and let users opt in. See the "Open decisions"
  section.
- **Deferred:** auto-detecting an IDE's open-workspace path (per-IDE, and
  reading it cheaply is hard) → Phase 2/future.

### 1.4 Honest IDE-only degradation — findings 2026-09-06
- **Messaging is already honest (no change needed).** The generic non-agent
  busy fallback (`watcher.py`) sets `state_reason="non-agent detector busy"`,
  which `observer.py` maps to the chatter **"something's busy"** — honest,
  agent-neutral. And agent-specific states (`approval_needed`, `celebrating`
  from a task-complete marker) require agent signals, so they never fire for
  an IDE-only user. That string is also a load-bearing key in the observer
  map + a test, so it is deliberately left unchanged.
- **DECISION PENDING — the sleep model.** `sleeping` is driven by *agent*
  quiet, **by explicit prior design** (code comments, 2026-09-04: "she now
  dozes on the agents' own quiet", after deciding user-presence was the
  wrong signal). So squid **does** doze during manual IDE coding when no
  agent is running. Making her stay awake on IDE activity turns her from an
  *agent-watcher* into a *work-watcher* — a real product shift, AND it costs
  idle CPU (the sleep gate would have to run the IDE file scan every quiet
  tick). This reverses a deliberate decision, so it is NOT changed
  unilaterally. See "Open decisions".

## Phase 2 — Claude Code IDE-extension parity (approach B) — RESOLVED 2026-09-06

- **Finding (the spike's answer):** the Claude Code IDE extension fires the
  **same hook events** as the terminal CLI. Per the official docs, "Claude
  Code fires the same hook events wherever it runs: sessions in the
  terminal, IDE extensions, the Desktop app, and Claude Code on the web."
  The VS Code / JetBrains extension runs a local MCP server that the **CLI
  connects to**, so a `claude` process still exists (found by
  `find_claude_code_processes`) and `PreToolUse` fires even for the
  extension's MCP tools.
- **Consequence:** **no bridge is needed.** squid's existing hook pipeline
  (status, approval via permission_prompt + our PreToolUse capture,
  celebrate) already drives the rich states when Claude runs as an IDE
  extension. Focus already resolves the host IDE via §1.1's dynamic bundle
  lookup (app-activate; no tty → graceful app-only). So the extension row
  in the capability matrix is Can/Can/Can/Partial with no new code.
- **Residual (not blocking):** live confirmation with the extension
  actually installed — the docs are authoritative but we have not watched
  `claude_hook.log` from an extension session here (all local sessions are
  the CLI). Worth a one-time live check when the extension is available.
- **Still out of scope (future):** third-party IDE agents (Cursor
  Composer, Copilot, Cline) — no readable approval/agent signal exists
  (future approach A: a squid IDE extension, or C: OS-level heuristics).

Sources: [Hooks reference](https://docs.claude.com/en/docs/claude-code/hooks),
[Use Claude Code in VS Code](https://docs.claude.com/en/docs/claude-code/ide-integrations),
[JetBrains IDEs](https://docs.claude.com/en/docs/claude-code/jetbrains).

## Open decisions (surfaced 2026-09-06; both touch the idle-CPU metric)

1. **Broaden the default `project_dirs`?** Single `~/Projects` today.
   Broadening to conventional roots that exist captures more IDE users
   out-of-the-box but adds a per-tick tree walk per root. *Recommendation:*
   keep single-root default + document the setting; opt-in only.
2. **Agent-watcher vs work-watcher sleep?** Squid deliberately sleeps on
   *agent* quiet, so she dozes during manual IDE coding. Keeping her awake
   on IDE activity reverses a deliberate 2026-09-04 decision and adds an IDE
   scan to every quiet tick. *Recommendation:* keep agent-watcher sleep, or
   gate a "work-watcher" mode behind a setting (default off) so the idle-CPU
   metric is protected.

## Non-goals
- Tab/window-level focus precision for non-Terminal hosts.
- Capturing third-party (non-Claude) IDE agents' status or approvals.
- Remote-SSH / dev-container / WSL / cloud-IDE support.
- Any change that regresses the Claude Code CLI experience.

## Testing
- Unit tests per Phase 1 item, TDD (each written and watched fail first),
  matching existing patterns (`tests/test_find_claude_code_processes.py`,
  `tests/test_detectors_ide.py`, `tests/test_claude_pet_hook_script.py`).
- The 1.1 guardrail test (detection↔focus parity) is the anti-regression
  keystone.
- No live-IDE automation; where a real app is needed, assert on the pure
  functions (bundle resolution, `focus_for_state` with an injected runner,
  `_scan_recent_file_ages` over a tmp tree) as the suite already does.

## Risks / open questions
- Bundle ids are hardcoded and can drift (Cursor's ToDesktop id, Warp's
  `-Stable`). The parity test catches *missing* entries, not *wrong* ones;
  accept manual verification per app.
- 1.3 stretch (auto-detect IDE workspace root) may prove per-IDE-hard and
  slip to Phase 2.
- Phase 2 (B) hinges on an unknown: does the Claude Code IDE extension run
  user hooks? First task of Phase 2 is to answer that.
