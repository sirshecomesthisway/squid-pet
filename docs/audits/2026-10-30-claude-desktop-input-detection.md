# Claude Desktop question detection, 2026-10-30

## Finding

The reported Desktop multiple-choice prompt corresponds to the existing
`AskUserQuestion` signal. The receiver already raises an awaiting-input flag
on `PreToolUse` and clears it on the matching owner lifecycle. On the affected
machine, the default Claude user settings had no hooks, there was no hook log
or awaiting-input flag, and no `force_state` override was present. Installing
the documented handlers repairs registration. It does not prove that a fresh
Desktop question was delivered; that still requires a real-session check.

The distribution gap was reproducible from the repository: `install.sh` did
not register Claude hooks, `squid update` did not repair them, and doctor only
checked daemon/window health. Manual JSON in a collapsed README section was
the only Claude setup path. A user could therefore install a healthy pet
without any direct Claude question/approval signal.

## Implemented solution

- One registration implementation, used by `scripts/install_claude_hooks.py`,
  `squid setup-claude`, install, update (including a no-op source update),
  and uninstall.
- Refresh the copied launcher on future updates. Already-installed old
  launchers cannot acquire new update behavior retroactively: rerun
  `./install.sh` once or invoke the Python setup script from the checkout.
- Configure the Desktop default and an explicit CLI `CLAUDE_CONFIG_DIR`.
  Merge only Squid handlers; repair old checkout paths; quote absolute
  Python/script paths; preserve other hooks, settings, permissions, and
  dotfile symlinks; back up once and atomically write. Invalid settings
  remain untouched. Never override `disableAllHooks` or managed policy.
- Read-only registration diagnostics in why/doctor and `--check`, with
  missing/partial/disabled/invalid/broken status and a repair command.
  Registration is separate from daemon health and delivery verification.
- No new per-tick work, transcript inference, network calls, or changes to
  the existing awaiting-input lifecycle.

## Coverage and remaining requirements

[Claude's Desktop documentation](https://code.claude.com/docs/en/desktop#shared-configuration)
states that CLI and Desktop Code share settings and hooks.
[The settings reference](https://code.claude.com/docs/en/settings)
documents user settings and `CLAUDE_CONFIG_DIR`.
[The hooks reference](https://code.claude.com/docs/en/hooks)
documents registration, `disableAllHooks`, and hook lifecycle events.
Registration alone cannot evaluate every project, session, or managed policy.
Use `/hooks` in the target session to confirm what actually loaded.

Cowork is distinct. Squid's existing local Cowork transcript discovery
can observe activity in legacy local sessions but does not deliver pending
question events. The observed local Cowork launcher uses a separate
configuration, and `entrypoint: local-agent` alone cannot distinguish it
from Code. Writing user settings or guessing from transcript silence does
not solve that gap.

Current [Cowork plugin documentation](https://support.claude.com/en/articles/13837440-use-plugins-in-claude)
describes plugin hooks, while [Cowork platform documentation](https://support.claude.com/en/articles/15520349-use-claude-cowork-on-web-desktop-and-mobile)
describes cloud execution and legacy local tasks. A hook in another runtime
does not automatically reach the host Mac. Full Cowork/remote support needs a
supported event transport back to Squid, with opaque session/request IDs,
explicit question-start/resolution/end events, host/session routing, reconnect
cleanup, and delivery tests on that surface. The current local receiver is
not a network endpoint; no such bridge is implemented by this change.

## Validation

Automated tests use temporary settings and signals, including old-path
migration, shared hook groups, repeated install/remove, malformed settings,
disabled hooks, missing receiver, symlinked dotfiles, and a checkout path
containing spaces. A generated command is executed outside the checkout
with Desktop `local-agent` metadata: `PreToolUse(AskUserQuestion)` emits a
flag/surface record, `PostToolUse` clears it, and permission/session-end
events exercise their existing lifecycle. Diagnostics and no-op update
repair are covered separately.

Release verification still needs a fresh local Desktop Code question:
confirm `/hooks`, submit a request that asks a choice, observe Squid wave,
answer, and confirm the wave clears. Repeat with a terminal CLI session,
an actual permission prompt, and two concurrent sessions. An already-visible
prompt is not replayed after installation. These live checks must be labeled
as pending until performed; synthetic lifecycle tests do not establish them.
