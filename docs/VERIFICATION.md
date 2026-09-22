# Verification harness

Run from the repository root on macOS with Python 3.11 or 3.13 and uv:

```bash
uv sync --dev --frozen --python 3.13
uv run --frozen ruff check .
uv run --frozen mypy --config-file pyproject.toml
uv run --frozen pytest
uv run --frozen python tools/verify_sprites.py
uv run --frozen python scripts/check_cocoa_main_thread.py src/squid_pet/*.py
```

This setup does not install a LaunchAgent or start/restart Squid. The lockfile
supplies the development tool versions. GitHub Actions runs the same checks on
`macos-latest` for both Python versions on pull requests and pushes to `main`.
The workflow has read-only repository permissions and a 15-minute job limit.

Run the full suite in a normal macOS shell. Existing `test_why_cli.py` smoke
tests invoke the real diagnostic CLI and macOS idle-time APIs; a restricted
sandbox can block CoreGraphics and cause hangs or subprocess timeouts. These
smoke tests can inspect live system state, so the suite is not wholly hermetic.
The settings-reload dispatch test isolates its idle probe, detectors and
approval signals to avoid those dependencies.

## What the checks cover

- **Ruff:** all repository Python files, using the existing F/E9/I rules for
  defects and import hygiene. This is not a formatting gate.
- **mypy:** the seven existing core modules named in `pyproject.toml`, plus the
  sprite verifier. Dynamic Cocoa/PyObjC UI modules remain outside the explicit
  target list; untyped third-party imports are still allowed. This is an
  incremental type-checking baseline, not a claim of full application coverage.
- **pytest:** the complete `tests/` suite; unknown markers and invalid pytest
  configuration are errors. Tests include synthetic PNG failure cases and the
  verifier's command-line exit status, including invocation outside the repo.
- **Sprites:** exactly 44 top-level runtime PNG names, including all four
  attention frames and all 24 pancake frames. Validate PNG format, chunk
  integrity, pixel decoding, expected canvas dimensions, an explicit alpha
  channel, fully transparent background pixels, and some visible pixels.
  Most canvases are 1254×1254; `heart.png` is 670×612, `idle_menubar.png` is
  710×725, and `sleeping_menubar.png` is 753×756. Source artwork under
  `_originals_with_bg/` and documentation are deliberately excluded. When
  intentionally changing the runtime inventory, update `EXPECTED_SIZES` in
  `tools/verify_sprites.py` alongside the frontend and artwork. An optional
  directory argument validates another inventory using this same contract.
- **Cocoa audit:** the existing AST check scans all application Python modules
  for unguarded window setters. This static heuristic does not prove runtime
  thread safety.

The verifier is read-only: it does not repair, recolor, or rewrite assets.
Pixel checks cannot judge visual quality, alignment, animation timing, or
whether a visible sprite is the intended drawing.

## Native smoke verification

The separate [native macOS loop](NATIVE_MACOS_VERIFICATION.md) exercises the
real installer, LaunchAgent, application window, doctor/status, forced watcher
states, and shutdown/restart on disposable hosted runners. See that document
for the exact gates, artifacts and visual-verification limitations.

## Manual macOS verification still required

CI and unit tests do not replace a logged-in macOS desktop session. Before a
release, record the macOS version, chip, Python version, and results for:

- Start and quit Squid; run `squid doctor` against the installed copy. Confirm
  one pet/window and no startup error in the log. Exercise LaunchAgent install,
  restart, login launch, upgrade, and uninstall separately on a test account.
- Check transparent edges and pose alignment on light/dark backgrounds at
  Retina and non-Retina scaling. Inspect attention waving, pancake flipping,
  sleep/wake, heart reactions, and the two menu-bar poses for clipping/flicker.
- Exercise drag, hover fade, click-through, menu actions, saved placement,
  menu-bar overlap, multiple displays, Spaces, and full-screen applications.
- Exercise actual Claude Code/Codex approval, completion and failure signals,
  IDE/shell activity, focus navigation, permissions prompts, and concurrent
  sessions. Confirm the bubble text agrees with the actual event.
- Check Git celebration against both a real commit and branch/worktree setup:
  the current detector uses HEAD/ref modification times, so Git metadata
  changes can produce a commit-like celebration without a new commit.
- Leave the pet idle, wake the machine from sleep, and inspect CPU/memory use
  and logs for timer, polling, or Cocoa main-thread errors.
- Test the oldest supported macOS (12), Intel hardware, and Apple Silicon.
  A `macos-latest` runner does not establish compatibility with all three.

These interactive checks were **not performed as part of adding this harness**.
The harness changes no application behavior and does not resolve existing UI
or detection issues exposed by those checks.
