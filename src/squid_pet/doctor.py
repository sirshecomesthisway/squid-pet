"""
squid doctor -- six-check end-to-end self-test.

Why this exists
---------------
The 2026-06-16 wedge proved that "process exists" is not sufficient
proof that Squid is healthy. The window can be invisibly stuck at
pywebview default (100, 100) while every process check passes. This
module verifies the actual user-visible contract: a process is
running, its state is fresh, its launchd job is registered, and most
importantly its window is actually rendered on screen at the
expected corner.

Invocation:
    squid-pet --doctor              # human-readable output, exit 0/N
    squid-pet --doctor --json       # machine-readable JSON

Exit codes:
    0 = all checks PASS
    N = check N failed (1..6; matches CHECK_ORDER ordering)

Design: each check function returns a ``CheckResult`` with passed=bool,
diagnostic text, suggested-fix hint, and a reference back to the design
doc / kennel drawer that explains the failure mode.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

from .claude_hooks import integration_report, report_lines

# ----------------------------------------------------------------------
# Filesystem paths (single source of truth -- mirrors window.py / watcher.py)
# ----------------------------------------------------------------------
STATE_DIR = Path.home() / ".squid-pet"
STATE_FILE = STATE_DIR / "state.json"
PID_FILE = STATE_DIR / "pid"
POSITION_FILE = STATE_DIR / "position.json"
# Written by window.py's _apply_hide_state() while the "Hide Squid" menu
# toggle is active; cleared on unhide and on every fresh boot. Presence
# means "no visible window" is expected and healthy, not a wedge.
HIDDEN_FLAG = STATE_DIR / "hidden"
STDOUT_LOG = Path("/tmp/squid-pet.out.log")

LAUNCHD_LABEL = "com.pink.squid-pet"

STATE_FRESHNESS_MAX_SEC = 5.0
WINDOW_CORNER_TOLERANCE_PX = 50.0


# ----------------------------------------------------------------------
# Result dataclass
# ----------------------------------------------------------------------
@dataclass
class CheckResult:
    name: str
    passed: bool
    diagnostic: str
    suggested_fix: str = ""
    drawer_ref: str = ""

    def as_line(self, index: int) -> str:
        status = "PASS" if self.passed else "FAIL"
        line = f"[{index}/6] {self.name:<26} ... {status}  {self.diagnostic}"
        if not self.passed and self.suggested_fix:
            line += f"\n         fix: {self.suggested_fix}"
        return line


# ----------------------------------------------------------------------
# Individual checks
# ----------------------------------------------------------------------
def check_process_running(pid_path: Path = PID_FILE) -> CheckResult:
    """Check 1: a process with the saved pid is alive."""
    name = "process running"
    if not pid_path.exists():
        return CheckResult(
            name=name, passed=False,
            diagnostic=f"no pid file at {pid_path}",
            suggested_fix="start Squid: 'python -m squid_pet' or relaunch via menu",
            drawer_ref="design.md D5",
        )
    try:
        pid = int(pid_path.read_text().strip())
    except (ValueError, OSError) as e:
        return CheckResult(
            name=name, passed=False,
            diagnostic=f"unreadable pid file: {e}",
            suggested_fix=f"rm {pid_path}; then relaunch",
            drawer_ref="design.md D5",
        )
    try:
        os.kill(pid, 0)  # signal 0 = existence probe, no actual signal
    except (ProcessLookupError, PermissionError):
        return CheckResult(
            name=name, passed=False,
            diagnostic=f"pid {pid} from pid file is not alive",
            suggested_fix=f"rm {pid_path}; then relaunch Squid",
            drawer_ref="design.md D5",
        )
    return CheckResult(
        name=name, passed=True,
        diagnostic=f"pid {pid} alive",
    )


def check_state_json_fresh(state_path: Path = STATE_FILE,
                           max_age_sec: float = STATE_FRESHNESS_MAX_SEC,
                           now_fn=None) -> CheckResult:
    """Check 2: state.json mtime is recent (watcher loop is alive)."""
    name = "state.json fresh"
    now = (now_fn or time.time)()
    if not state_path.exists():
        return CheckResult(
            name=name, passed=False,
            diagnostic=f"no state file at {state_path}",
            suggested_fix="watcher thread did not start -- relaunch Squid",
            drawer_ref="design.md D5",
        )
    age = now - state_path.stat().st_mtime
    if age > max_age_sec:
        return CheckResult(
            name=name, passed=False,
            diagnostic=f"state.json last updated {age:.1f}s ago (max {max_age_sec:.0f}s)",
            suggested_fix="watcher thread may be stalled -- relaunch Squid",
            drawer_ref="design.md D5",
        )
    return CheckResult(
        name=name, passed=True,
        diagnostic=f"updated {age:.1f}s ago",
    )


def check_launchd_loaded(label: str = LAUNCHD_LABEL) -> CheckResult:
    """Check 3: launchd has our job registered."""
    name = "launchd job loaded"
    try:
        result = subprocess.run(
            ["launchctl", "list", label],
            capture_output=True, text=True, timeout=3.0,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        return CheckResult(
            name=name, passed=False,
            diagnostic=f"launchctl call failed: {e}",
            suggested_fix="install LaunchAgent: see docs/STARTUP_SAFETY.md",
            drawer_ref="design.md D6",
        )
    if result.returncode != 0:
        return CheckResult(
            name=name, passed=False,
            diagnostic=f"launchctl list {label} -> rc={result.returncode}",
            suggested_fix=f"launchctl bootstrap gui/$UID ~/Library/LaunchAgents/{label}.plist",
            drawer_ref="design.md D6",
        )
    return CheckResult(
        name=name, passed=True,
        diagnostic=f"job {label} loaded",
    )


def _pick_pet_cgwindow(entries: list) -> Optional[dict]:
    """Choose the sprite's window from the CGWindowList entries our pid owns.

    Every window the process owns appears there, and the menu-bar
    NSStatusItem is on-screen with alpha 1.0 exactly like the sprite --
    so "first match wins" made this report the 38x24 menu-bar item after
    a restart (observed 2026-09-03), which sends anyone reading a bug
    report chasing the wrong thing. Prefer an exact sprite-sized match;
    otherwise take the largest, since the status item is tiny either way.
    """
    if not entries:
        return None
    from .window import WINDOW_HEIGHT, WINDOW_WIDTH
    for e in entries:
        if e["w"] == float(WINDOW_WIDTH) and e["h"] == float(WINDOW_HEIGHT):
            return e
    return max(entries, key=lambda e: e["w"] * e["h"])


def _get_visible_window_for_pid(pid: int) -> Optional[dict]:
    """Return CGWindowList entry for the given pid where alpha>0 and on-screen,
    or None. Returns None gracefully on non-Mac / Quartz import failure."""
    try:
        from Quartz import (
            CGWindowListCopyWindowInfo,
            kCGNullWindowID,
            kCGWindowAlpha,
            kCGWindowBounds,
            kCGWindowIsOnscreen,
            kCGWindowListOptionAll,
            kCGWindowOwnerPID,
        )
    except ImportError:
        return None
    info = CGWindowListCopyWindowInfo(kCGWindowListOptionAll, kCGNullWindowID)
    candidates = []
    for w in info:
        if w.get(kCGWindowOwnerPID) != pid:
            continue
        if not w.get(kCGWindowIsOnscreen, False):
            continue
        if float(w.get(kCGWindowAlpha, 0)) <= 0.0:
            continue
        bounds = w.get(kCGWindowBounds)
        if bounds is None:
            continue
        candidates.append({
            "x": float(bounds.get("X", 0)),
            "y": float(bounds.get("Y", 0)),
            "w": float(bounds.get("Width", 0)),
            "h": float(bounds.get("Height", 0)),
            "alpha": float(w.get(kCGWindowAlpha, 0)),
        })
    return _pick_pet_cgwindow(candidates)
    return None


def check_window_visible(pid_path: Path = PID_FILE,
                          window_lookup=None,
                          hidden_flag_path: Path = HIDDEN_FLAG) -> CheckResult:
    """Check 4: CGWindowList shows a visible window for our pid.

    This is the check that would have caught the 2026-06-16 wedge:
    the pywebview default (100,100) window was technically rendered
    but at the wrong position with alpha=1.0; the issue was that
    every other layer assumed visible = correct.

    Exception: the "Hide Squid" menu toggle is a real, supported
    feature that sets window alpha=0 on purpose (window.py's
    _apply_hide_state). Without checking hidden_flag_path first, a
    healthily-hidden Squid looks identical to a wedged one and this
    check would false-positive FAIL every time a user hides her.
    """
    name = "window visible"
    if hidden_flag_path.exists():
        return CheckResult(
            name=name, passed=True,
            diagnostic="Squid is intentionally hidden (Hide Squid menu toggle) "
                       "-- no window expected",
        )
    lookup = window_lookup or _get_visible_window_for_pid
    if not pid_path.exists():
        return CheckResult(
            name=name, passed=False,
            diagnostic="no pid file -- cannot determine which window to check",
            suggested_fix="relaunch Squid",
            drawer_ref="design.md D1",
        )
    try:
        pid = int(pid_path.read_text().strip())
    except (ValueError, OSError) as e:
        return CheckResult(
            name=name, passed=False,
            diagnostic=f"unreadable pid file: {e}",
            suggested_fix=f"rm {pid_path}; then relaunch",
            drawer_ref="design.md D1",
        )
    win = lookup(pid)
    if win is None:
        return CheckResult(
            name=name, passed=False,
            diagnostic=f"no visible window found for pid {pid} via CGWindowList",
            suggested_fix=("the window may be wedged off-screen. "
                          "Relaunch Squid. If repeated: thread-safety regression -- "
                          "audit recent commits for NSWindow setters without "
                          "@cocoa_main_thread"),
            drawer_ref="kennel drawer 239 / commit 0d21f15",
        )
    return CheckResult(
        name=name, passed=True,
        diagnostic=f"pid {pid} window at ({win['x']:.0f},{win['y']:.0f}) "
                   f"size {win['w']:.0f}x{win['h']:.0f} alpha={win['alpha']:.2f}",
    )


def check_window_in_expected_corner(
    pid_path: Path = PID_FILE,
    position_path: Path = POSITION_FILE,
    tolerance_px: float = WINDOW_CORNER_TOLERANCE_PX,
    window_lookup=None,
    corner_origin_fn=None,
    hidden_flag_path: Path = HIDDEN_FLAG,
) -> CheckResult:
    """Check 5: window is NOT at the pywebview-default wedge position.

    The 2026-06-16 wedge left the window stuck at pywebview's default
    (100, 100) origin because move_to_corner silently failed when called
    from a WebKit thread. After Squid is healthy, the window is either
    at a chosen corner OR being moved by the wanderer -- but never at
    (100, 100) for more than a fraction of a second.

    We deliberately do NOT compare against position.json's saved corner
    because the wanderer legitimately moves the window all over the
    screen during routine. Comparing to that would false-positive
    constantly. Instead we check for the specific bug signature.

    Same hidden-flag exception as check_window_visible: an intentionally
    hidden window has no CGWindowList entry to check a position on.
    """
    name = "window not wedged"
    if hidden_flag_path.exists():
        return CheckResult(
            name=name, passed=True,
            diagnostic="Squid is intentionally hidden -- position check skipped",
        )
    lookup = window_lookup or _get_visible_window_for_pid
    if not pid_path.exists():
        return CheckResult(
            name=name, passed=False, diagnostic="no pid file",
            suggested_fix="relaunch Squid", drawer_ref="design.md D1",
        )
    try:
        pid = int(pid_path.read_text().strip())
    except (ValueError, OSError):
        return CheckResult(
            name=name, passed=False, diagnostic="unreadable pid file",
            suggested_fix=f"rm {pid_path}; relaunch", drawer_ref="design.md D1",
        )
    win = lookup(pid)
    if win is None:
        return CheckResult(
            name=name, passed=False,
            diagnostic="no visible window (check 4 would have caught this)",
            drawer_ref="kennel drawer 239",
        )
    PYWEBVIEW_DEFAULT_X = 100.0
    PYWEBVIEW_DEFAULT_Y = 100.0
    WEDGE_TOLERANCE_PX = 10.0
    dx = abs(win["x"] - PYWEBVIEW_DEFAULT_X)
    dy = abs(win["y"] - PYWEBVIEW_DEFAULT_Y)
    if dx <= WEDGE_TOLERANCE_PX and dy <= WEDGE_TOLERANCE_PX:
        return CheckResult(
            name=name, passed=False,
            diagnostic=(f"window at ({win['x']:.0f},{win['y']:.0f}) -- "
                       f"matches pywebview default wedge position. "
                       f"move_to_corner did not run (likely thread-safety bug)."),
            suggested_fix=("audit recent NSWindow setters for missing "
                          "@cocoa_main_thread. See kennel drawer 239 and "
                          "commit 0d21f15 for the canonical instance."),
            drawer_ref="kennel drawer 239 / commit 0d21f15",
        )
    return CheckResult(
        name=name, passed=True,
        diagnostic=(f"at ({win['x']:.0f},{win['y']:.0f}) -- not at "
                   f"pywebview default (100,100)"),
    )

REQUIRED_STARTUP_MARKERS = (
    "watcher thread started",
    "passthrough loop started",
    "routine thread started",
    "context menu ready",
    "startup complete",
)


def check_startup_log_complete(log_path: Path = STDOUT_LOG,
                                tail_bytes: int = 4_000_000) -> CheckResult:
    """Check 6: the MOST RECENT boot logged every startup marker.

    This used to read the log's HEAD, on the assumption that "startup
    markers are at the top". launchd APPENDS across restarts, so the top
    is the first boot ever recorded -- stale, and often partial. On
    Pink's machine (2026-09-03) that made this check report FAIL
    permanently: a 6.3MB log whose first 16KB began mid-session, while
    all five markers were in fact present 70 times over. The same bug
    ran the other way too -- a boot that died halfway still PASSED,
    because the old complete sequence was still sitting up top.

    So grade the newest boot: find the last occurrence of the first
    marker and require the rest to follow it.
    """
    name = "startup log markers"
    if not log_path.exists():
        return CheckResult(
            name=name, passed=False,
            diagnostic=f"no log file at {log_path}",
            suggested_fix="logs missing -- launchd plist may not be wiring stdout",
            drawer_ref="design.md D6",
        )
    try:
        with log_path.open("rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - tail_bytes))
            tail_text = f.read().decode("utf-8", errors="replace")
    except OSError as e:
        return CheckResult(
            name=name, passed=False,
            diagnostic=f"could not read log: {e}",
            drawer_ref="design.md D6",
        )
    first_marker = REQUIRED_STARTUP_MARKERS[0]
    boot_at = tail_text.rfind(first_marker)
    if boot_at < 0:
        return CheckResult(
            name=name, passed=False,
            diagnostic=(f"no startup sequence in the last {tail_bytes} bytes "
                        f"of log (never saw {first_marker!r})"),
            suggested_fix=("Squid may not have reached init. Check the end of "
                           "the log for an exception."),
            drawer_ref="design.md D5",
        )
    latest_boot = tail_text[boot_at:]
    missing = [m for m in REQUIRED_STARTUP_MARKERS if m not in latest_boot]
    if missing:
        return CheckResult(
            name=name, passed=False,
            diagnostic=("missing startup markers in the most recent boot: "
                        f"{', '.join(missing)}"),
            suggested_fix=("Squid started but did not complete init. "
                          "Check log around the missing markers for an exception."),
            drawer_ref="design.md D5",
        )
    return CheckResult(
        name=name, passed=True,
        diagnostic=f"all {len(REQUIRED_STARTUP_MARKERS)} startup markers present",
    )

# ----------------------------------------------------------------------
# Claude Code surfaces (informational -- never a check, never an exit code)
# ----------------------------------------------------------------------
# 2026-10-12: Claude Code reaches this machine on several surfaces. The
# terminal CLI and the desktop app's Code tab are both watched (the desktop app
# spawns its OWN bundled copy of the CLI, which fires the same hooks); the
# desktop VM sandbox and cloud sessions leave nothing local to watch. The
# bundled copy updates on the app's schedule, not the terminal CLI's, so the
# two can run far-apart versions -- report both and warn on a wide skew.
CLAUDE_DESKTOP_BUNDLE_ID = "com.anthropic.claudefordesktop"
CLAUDE_DESKTOP_APP_PATHS = (
    Path("/Applications/Claude.app"),
    Path.home() / "Applications" / "Claude.app",
)
CLAUDE_DESKTOP_BUNDLED_CLI_ROOT = (
    Path.home() / "Library" / "Application Support" / "Claude" / "claude-code")
# Warn when major.minor differ, or the patch numbers are at least this far
# apart. Claude Code ships patch releases often; a gap this wide means hook
# payloads and transcript fields may differ between the two surfaces.
CLI_VERSION_SKEW_WARN_PATCHES = 50


@dataclass
class SurfaceReport:
    desktop_app_version: Optional[str]
    desktop_cli_version: Optional[str]
    terminal_cli_version: Optional[str]
    warnings: list

    def lines(self) -> list:
        term = self.terminal_cli_version or "not found on PATH"
        if self.desktop_app_version is None:
            desktop = "not installed (normal -- only needed for its Code tab)"
        else:
            bundled = self.desktop_cli_version or "not downloaded yet"
            desktop = (f"{self.desktop_app_version}, bundled Claude Code CLI "
                       f"{bundled}")
        out = [f"terminal CLI      {term}",
               f"desktop app       {desktop}"]
        out += [f"WARN: {w}" for w in self.warnings]
        return out


def _version_tuple(text: Optional[str]) -> Optional[tuple]:
    import re
    m = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", text or "")
    return tuple(int(g) for g in m.groups()) if m else None


def _desktop_app_version(app_paths) -> Optional[str]:
    import plistlib
    for app in app_paths:
        try:
            with open(Path(app) / "Contents" / "Info.plist", "rb") as f:
                info = plistlib.load(f)
        except Exception:
            continue
        if info.get("CFBundleIdentifier") == CLAUDE_DESKTOP_BUNDLE_ID:
            version = info.get("CFBundleShortVersionString")
            return str(version) if version else "unknown version"
    return None


def _desktop_bundled_cli_version(root: Path) -> Optional[str]:
    """Newest <root>/<x.y.z>/ that actually holds the bundled CLI binary."""
    try:
        names = os.listdir(root)
    except OSError:
        return None
    best: Optional[tuple] = None
    for name in names:
        vt = _version_tuple(name)
        if vt is None:
            continue
        # <ver>/claude.app (older layout) or <ver>/<hash>/claude.app (3.1623+).
        tail = Path("claude.app") / "Contents" / "MacOS" / "claude"
        ver_dir = Path(root) / name
        try:
            if not ((ver_dir / tail).is_file() or any(ver_dir.glob(f"*/{tail}"))):
                continue
        except OSError:
            continue
        if best is None or vt > best[0]:
            best = (vt, name)
    return best[1] if best else None


def terminal_claude_version(which=None, run=None) -> Optional[str]:
    """`claude --version` of the CLI on PATH (one fork -- fine on demand,
    never on the 1 Hz path). None if absent, failing, or unparseable."""
    import re
    import shutil
    which = which or shutil.which
    run = run or subprocess.run
    try:
        exe = which("claude")
        if not exe:
            return None
        r = run([exe, "--version"], capture_output=True, text=True, timeout=5.0)
        m = re.search(r"\b(\d+\.\d+\.\d+)\b", r.stdout or "")
        return m.group(1) if m else None
    except Exception:
        return None


def claude_surface_report(app_paths=None, bundled_cli_root=None,
                          terminal_version_fn=None) -> SurfaceReport:
    """Which Claude Code surfaces this machine has, and their CLI versions."""
    app_version = _desktop_app_version(
        CLAUDE_DESKTOP_APP_PATHS if app_paths is None else app_paths)
    cli_version = None
    if app_version is not None:
        cli_version = _desktop_bundled_cli_version(
            CLAUDE_DESKTOP_BUNDLED_CLI_ROOT if bundled_cli_root is None
            else bundled_cli_root)
    term_version = (terminal_version_fn or terminal_claude_version)()
    warnings = []
    d, t = _version_tuple(cli_version), _version_tuple(term_version)
    if d is not None and t is not None and (
            d[:2] != t[:2] or abs(d[2] - t[2]) >= CLI_VERSION_SKEW_WARN_PATCHES):
        warnings.append(
            f"the desktop app's bundled Claude Code CLI ({cli_version}) and the "
            f"terminal CLI ({term_version}) are far apart -- hook payloads and "
            "transcript fields can differ between the two surfaces")
    return SurfaceReport(app_version, cli_version, term_version, warnings)


# ----------------------------------------------------------------------
# Orchestrator
# ----------------------------------------------------------------------
CHECK_ORDER = (
    ("process running", check_process_running),
    ("state.json fresh", check_state_json_fresh),
    ("launchd job loaded", check_launchd_loaded),
    ("window visible", check_window_visible),
    ("window not wedged", check_window_in_expected_corner),
    ("startup log markers", check_startup_log_complete),
)


def run_all_checks() -> list[CheckResult]:
    return [fn() for _name, fn in CHECK_ORDER]


def _safe_surface_report() -> Optional[SurfaceReport]:
    """The surface report must never break doctor."""
    try:
        return claude_surface_report()
    except Exception:
        return None


def run_doctor(json_output: bool = False) -> int:
    """Run all 6 checks. Print results. Return exit code (0 = healthy).

    Claude surfaces and hook registration are reported separately from daemon
    health; neither changes the six-check exit code."""
    results = run_all_checks()
    surfaces = _safe_surface_report()
    hooks = integration_report()
    if json_output:
        payload = {
            "healthy": all(r.passed for r in results),
            "checks": [asdict(r) for r in results],
            "claude_surfaces": asdict(surfaces) if surfaces else None,
            "claude_hooks": hooks,
        }
        print(json.dumps(payload, indent=2))
    else:
        print("squid doctor -- end-to-end self-test")
        print("-" * 70)
        for i, r in enumerate(results, start=1):
            print(r.as_line(i))
        print("-" * 70)
        passed = sum(1 for r in results if r.passed)
        print(f"{passed}/{len(results)} checks passed")
        print()
        print("Claude hooks (registration only; separate from daemon health):")
        for line in report_lines(hooks):
            print(f"  {line}")
        if surfaces is not None:
            print()
            print("Claude Code surfaces (info only -- never fails doctor):")
            for line in surfaces.lines():
                print(f"  {line}")
    # Exit code: 0 if all pass, otherwise the 1-based index of first failure.
    for i, r in enumerate(results, start=1):
        if not r.passed:
            return i
    return 0
