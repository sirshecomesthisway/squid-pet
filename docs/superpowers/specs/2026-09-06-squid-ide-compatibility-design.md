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
| **Claude Code IDE extension** (VS Code/Cursor) | **Phase 2 (B): verify** | Phase 2 (B) | Phase 2 (B) | Partial |
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

### 1.1 Focus-map ↔ detection-map parity
- **Change:** extend `_TERMINAL_APP_BUNDLE_IDS` (`watcher.py:374`) so every
  IDE/terminal squid can *detect* (`DEFAULT_IDE_PROCESSES`) or *host* a CLI
  in is app-activatable: add JetBrains family
  (IntelliJ IDEA/PyCharm/WebStorm/GoLand/CLion/RubyMine — process name +
  bundle id per app), Zed, Windsurf, and any terminal forks in scope
  (iTerm already present; add Ghostty if desired). Cursor already added
  (commit `5f73bba`).
- **Guardrail test:** assert that **every** name in `DEFAULT_IDE_PROCESSES`
  resolves to a bundle in `_TERMINAL_APP_BUNDLE_IDS` (or an explicit
  "detect-only, no focus" allowlist), so detection/focus can never drift
  apart again silently.
- **Note on precision:** these remain app-activate only ("app-only");
  tab/window precision is out of scope (Terminal.app-only by design).

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

### 1.3 Configurable, multi-root project dirs
- **Change:** `project_dirs` defaults to a single `~/Projects`
  (`detectors.py:789`). Support multiple roots via `settings.json`
  (`build_detectors` already threads `project_dirs`; confirm list handling
  end-to-end and document it).
- **Stretch:** auto-include the workspace/project root of a detected IDE so
  capture isn't blind to projects outside `~/Projects`. Kept as a stretch
  because reliably reading an IDE's open-workspace path is per-IDE and may
  slip to Phase 2.

### 1.4 Honest IDE-only degradation
- **Change:** when an IDE is active but no CLI agent is running, status and
  messaging must be truthful — adjust the wording/`state_reason` of the
  **existing** generic busy/grooving/idle states (e.g. the
  `watcher.py:1643` fallback currently reads "🤔 working") so they don't
  imply agent thinking/approval squid cannot observe. **No new sprite or
  cascade state** is introduced in Phase 1 (that would be a larger change);
  this is messaging + the sleeping fix below only.
- **Sleeping fix:** `sleeping` is driven by *agent* quiet
  (`_agent_idle_since`, `watcher.py:1447`); ensure genuine manual IDE
  activity (recent file writes) prevents dozing so squid doesn't sleep
  through active work. Reuse existing signals; no new detector.
- **Acceptance:** with only an IDE running and files changing, squid shows a
  coherent non-agent "busy/creative" state and never `approval_needed` or
  agent-specific celebrate; with nothing happening it idles/sleeps as today.

## Phase 2 — Claude Code IDE-extension parity (approach B; researched direction)

Not built in this plan; recorded so Phase 1 does not paint us into a corner.

- **Goal:** when a user runs the **Claude Code IDE extension** (VS Code /
  Cursor) rather than the terminal CLI, drive the same rich states +
  approval that the CLI hooks provide.
- **Approach B:** determine whether the Claude Code IDE extension already
  fires the `~/.claude/settings.json` hooks (Notification / PreToolUse /
  Stop / UserPromptSubmit …). If it does, Phase 2 is mostly *verification +
  documentation + focus-gap coverage*. If it does not, define the smallest
  bridge that lets the extension write the existing `~/.squid-pet` flag
  protocol — reusing the entire watcher/state/focus pipeline unchanged.
- **Explicitly out of scope here:** third-party IDE agents (Cursor
  Composer, Copilot, Cline) — no readable approval/agent signal exists
  (would be a future approach A: a squid IDE extension, or C: OS-level
  heuristics). Phase 2 gets its own brainstorm + spec.

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
