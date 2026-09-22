# Native macOS CI

## Audit and design

At the start of this change, `mac-verify.yml` did not exist on main or the
verification branch. `.github/workflows/ci.yml` ran Python/static checks on
macOS, without launching Squid. The new workflow adds a separate native job;
existing CI/test check identities remain unchanged.

The real `install.sh` clones a repository, creates a venv, installs locked
dependencies, writes defaults, copies `bin/squid`, renders the LaunchAgent,
and bootstraps it in `gui/$UID`. Its `verify_alive` failure is intentionally
nonfatal, so installer exit 0 is insufficient evidence. `squid status` can
also exit 0 while printing STALE or NO STATE FILE. Doctor checks process,
state freshness, launchd, visible window, wedge position, and startup logs;
its window fallback can accept a menu-bar window. The harness independently
requires a sprite-sized window for the installed PID using Quartz.

The file `~/.squid-pet/force_state` overrides watcher state. It differs from
the menu's in-memory presentation override. Watcher state does not prove
that WebKit displayed a particular frame; mood and routine overlays can
still own the image. Screenshots are diagnostic evidence, not DOM assertions.

## Automatically exercised

`mac-verify.yml` runs three independent `macos-15` jobs per PR or main push.
Each starts with an empty application install and uv dependency cache. A
local bare repository serves the exact candidate commit to the unmodified
installer; this avoids testing upstream main accidentally. GitHub HTTPS
cloning/authentication and Homebrew's uv bootstrap are not exercised.

The harness then checks:

- Real dependency installation and Cocoa/Quartz/WebKit imports.
- Plist syntax and actual LaunchAgent bootstrap in the GUI domain.
- Matching launchd/PID-file identity, fresh watcher state, visible native
  sprite window, all six `squid doctor --json` checks, and `squid status`
  reporting RUNNING/TICKING for that PID.
- The same process surviving 15 seconds (beyond the 10-second startup
  watchdog), continuously fresh state, and at least ten distinct ticks.
- Two new watcher ticks for each forced idle, thinking, working,
  approval_needed, celebrating, and sleeping state; force removal returns
  to detector output. Native window presence is checked for each state.
- `squid restart` yields a new healthy PID and the old PID is gone.
- `squid stop` removes the job, exits its process, makes status fail, and
  leaves watcher data unchanged over three seconds.
- `squid start` after stop repeats the full health test, then final shutdown.

Readiness uses bounded condition polling. Failed boots are not retried;
there are no unconditional startup sleeps or continue-on-error health gates.
A missing GUI domain fails with diagnostics rather than silently replacing
native coverage with unit tests. Each run uploads artifacts, even on failure.

## Artifacts

`native-macos-<matrix attempt>-<workflow attempt>` contains command output
and exit codes, `events.jsonl`, `summary.md`, traceback on failure, application
logs, settings/config/state snapshots, rendered plist, process list, launchd
state, and window-specific PNG captures where screen recording permits.
Only the disposable runner is inspected; no developer desktop is captured.
The JSON event stream links commands to their numbered log files and records
PID/tick/window evidence. Download artifacts before the 14-day retention ends.

Screenshots retain real animations and routine/mood overlays. Their timing
is tied to new watcher ticks, not confirmed frontend frame acknowledgement.
Do not use these as golden images or infer visual correctness from their
presence. Screen recording restrictions are reported as missing captures;
all native health checks still gate the job.

## Safety and local verification

The installer and CLI use the fixed `com.pink.squid-pet` label and `/tmp`
log names. Therefore `tools/native_smoke.py` refuses personal/self-hosted
machines and any pre-existing installation. Do not bypass these guards or
point the installer at your usual Squid checkout to reproduce CI.

The regular Ruff, mypy, pytest, sprite and Cocoa checks remain in
[VERIFICATION.md](VERIFICATION.md). Pure harness regression tests run there;
they check stale/future state rejection, window identity, bounded timeout
evidence, and runner guards without changing an installed pet.

Human/local Mac release checks still include login persistence, actual TCC
permission prompts, screen lock/unlock and sleep/wake, multiple monitors and
Spaces, Retina scaling/transparency, interaction/click-through/drag/menus,
animation fidelity, real agent approvals/completions/concurrent sessions,
long-duration behavior and macOS 12/Intel compatibility. Native CI verifies
startup/lifecycle mechanics on its runner image, not those experiences.

## Validation record

Hosted runs and any observed limitations are recorded in the PR. No product
behavior has been changed by this harness.
