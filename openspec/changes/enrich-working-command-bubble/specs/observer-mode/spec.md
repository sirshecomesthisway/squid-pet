# observer-mode Specification (delta)

## MODIFIED Requirements

### Requirement: Enriched working bubble reports the live shell command

When the pet enters (or is reannounced in) the `working` state on the
strength of a live shell child, the Observer SHALL prefer a concrete
bubble naming the command over a generic reaction. The bubble SHALL be
derived from the shell child's cmdline, SHALL omit the agent name, and
SHALL fit within the 32-character ceiling. When no meaningful command can
be derived, the Observer SHALL fall back to the state-reason explanation
line rather than emit a partial or internal string.

The command cmdline MAY arrive either as a bare tool invocation
(e.g. `["pytest", "tests/", "-v"]`) or as Claude Code's shell wrapper
(`["/bin/zsh", "-c", "source <snapshot> … && eval '<CMD>' < /dev/null …"]`).
The Observer SHALL recover the real command from the wrapper before
formatting.

#### Scenario: Command recovered from the zsh wrapper
- **WHEN** the shell cmdline is a `zsh -c` (or `bash`/`sh -c`) wrapper whose
  script contains `eval '<CMD>' < /dev/null`
- **THEN** the Observer extracts `<CMD>` as the command to report
- **AND** decodes `\012` escapes to newlines and `'\''` sequences to a single quote
- **AND** the resulting bubble names the recovered command, not `source` or `eval`

#### Scenario: Bubble names verb and, for two-word tools, the subcommand
- **WHEN** the recovered command is a known two-word tool (e.g. `git push origin main`)
- **THEN** the bubble is `runs git push`
- **WHEN** the recovered command is a single-verb tool (e.g. `pytest tests/ -v`)
- **THEN** the bubble names that verb (`runs pytest …`)

#### Scenario: A path-like argument is appended as the target
- **WHEN** the recovered command has an argument containing `/` or a file extension
  (e.g. `pytest tests/test_observer.py -v`)
- **THEN** the bubble appends it as ` in <target>` (e.g. `runs pytest in test_observer.py`)
- **AND** the target is shortened (full path → basename → parent dir) as needed to fit 32 chars
- **AND** if no shortening fits, the ` in <target>` clause is dropped

#### Scenario: Non-path arguments are NOT used as a target
- **WHEN** the recovered command's arguments are a commit message, a search
  pattern, a package name, or a subcommand keyword
  (e.g. `git commit -m "msg"`, `grep foo src/`, `brew install ripgrep`, `npm run build`)
- **THEN** the message/pattern/package/keyword is NOT placed in the target slot
- **AND** a genuine path argument, if present, is used instead
  (e.g. `grep foo src/` → `runs grep in src/`)

#### Scenario: Redirects and comments are not treated as command or target
- **WHEN** the recovered command contains a shell redirect
  (e.g. `git status --short >/dev/null 2>&1`)
- **THEN** the redirect operator and its target are NOT placed in the target slot
  (the bubble is `runs git status`, never `runs git status in >/dev/null`)
- **WHEN** a segment begins with a `#` comment token
- **THEN** that segment is skipped rather than reported as the command

#### Scenario: Leading shell-noise commands are skipped
- **WHEN** the recovered command begins with a noise leader such as
  `cd`, `export`, or `source` followed by the real command
  (e.g. `cd src && ruff check squid_pet/observer.py`)
- **THEN** the noise leader is skipped and the bubble names the real command
  (`runs ruff in observer.py`)

#### Scenario: Unparseable wrapper falls back, never leaks internals
- **WHEN** the cmdline is a wrapper with no `eval '…' < /dev/null` anchor,
  or the only command is a noise leader
- **THEN** the concrete-bubble helper returns None
- **AND** the Observer falls back to the state-reason explanation line
- **AND** no snapshot path, preamble fragment, or `eval`/`source` token is shown

#### Scenario: Codex activity is unchanged by this requirement
- **WHEN** the working state is driven by Codex rather than Claude Code
- **THEN** the enriched wrapper-recovery path does not apply and the existing
  bubble behavior is preserved
