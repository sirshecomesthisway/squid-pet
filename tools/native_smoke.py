#!/usr/bin/env python3
"""Exercise the unmodified installer/app on a disposable GitHub macOS runner.

Never run this against a personal account: the product has a fixed launchd label.
Uses only stdlib until --windows invokes Quartz through the installed interpreter.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import platform
import re
import shutil
import subprocess
import sys
import time
import traceback
from pathlib import Path
from typing import Any, Callable

LABEL = "com.pink.squid-pet"


def require_hosted_macos(system: str, env: dict[str, str]) -> None:
    if (system != "Darwin" or env.get("GITHUB_ACTIONS") != "true"
            or env.get("RUNNER_ENVIRONMENT") != "github-hosted"):
        raise RuntimeError("Native smoke requires a disposable GitHub-hosted macOS runner")


def fresh_state(state: dict, now: float, after: float, expected: str | None = None) -> bool:
    stamp = state.get("timestamp")
    return (isinstance(stamp, (int, float)) and not isinstance(stamp, bool) and math.isfinite(stamp)
            and after < stamp <= now and now - stamp <= 5
            and (expected is None or state.get("state") == expected))


def sprite_window(entries: list[dict], pid: int, width: int, height: int) -> dict | None:
    return next((w for w in entries if w["pid"] == pid and w["onscreen"]
                 and w["alpha"] > 0 and w["width"] == width and w["height"] == height), None)


def verify_launchd(text: str, pid: int, runs: int) -> None:
    actual_pid = re.search(r"^\s*pid = (\d+)$", text, re.MULTILINE)
    actual_runs = re.search(r"^\s*runs = (\d+)$", text, re.MULTILINE)
    if actual_pid is None or int(actual_pid[1]) != pid:
        raise RuntimeError(f"launchd does not own expected pid {pid}")
    if actual_runs is None or int(actual_runs[1]) != runs:
        raise RuntimeError(f"unexpected launch count (expected {runs}); possible crash/restart: {text}")


def native_healthy(window: dict, returncode: int, doctor: dict) -> bool:
    return bool(window.get("sprite") and returncode == 0 and doctor.get("healthy") is True)


def wait_for(label: str, probe: Callable[[], tuple[bool, Any]], timeout: float = 30) -> Any:
    deadline = time.monotonic() + timeout
    while True:
        ready, evidence = probe()
        if ready:
            return evidence
        if time.monotonic() >= deadline:
            raise TimeoutError(f"{label}: last observation: {evidence!r}")
        time.sleep(0.25)  # bounded polling, not a presumed readiness delay


def read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def native_windows(pid: int) -> dict:
    from Quartz import CGWindowListCopyWindowInfo, kCGNullWindowID, kCGWindowListOptionAll

    from squid_pet.window import WINDOW_HEIGHT, WINDOW_WIDTH
    entries = []
    for w in CGWindowListCopyWindowInfo(kCGWindowListOptionAll, kCGNullWindowID):
        if w.get("kCGWindowOwnerPID") != pid:
            continue
        b = w.get("kCGWindowBounds", {})
        entries.append({"pid": pid, "id": w.get("kCGWindowNumber"),
                        "width": b.get("Width"), "height": b.get("Height"),
                        "x": b.get("X"), "y": b.get("Y"),
                        "alpha": w.get("kCGWindowAlpha", 0),
                        "onscreen": bool(w.get("kCGWindowIsOnscreen", False))})
    return {"windows": entries, "sprite": sprite_window(entries, pid, WINDOW_WIDTH, WINDOW_HEIGHT)}


class Smoke:
    def __init__(self, checkout: Path, artifacts: Path, scratch: Path):
        self.checkout, self.artifacts, self.scratch = checkout, artifacts, scratch
        self.project = scratch / "installed"
        self.state_dir = Path.home() / ".squid-pet"
        self.plist = Path.home() / "Library/LaunchAgents" / f"{LABEL}.plist"
        self.cli = Path.home() / ".local/bin/squid"
        self.domain = f"gui/{os.getuid()}/{LABEL}"
        self.env = dict(os.environ, UV_CACHE_DIR=str(scratch / "uv-cache"), UV_PYTHON="3.13")
        self.stage = "preflight"
        self.owns_install = False
        self.sequence = 0
        self.events: list[dict] = []

    def note(self, message: str, **evidence: Any) -> None:
        event = {"time": time.time(), "stage": self.stage, "message": message, **evidence}
        self.events.append(event)
        print(json.dumps(event), flush=True)
        with (self.artifacts / "events.jsonl").open("a") as f:
            f.write(json.dumps(event) + "\n")

    def command(self, *args: str, check: bool = True, timeout: float = 30,
                cwd: Path | None = None) -> subprocess.CompletedProcess:
        self.sequence += 1
        log = self.artifacts / f"{self.sequence:03d}-{Path(args[0]).name}.log"
        with log.open("w") as f:
            f.write(f"command: {args!r}\n")
            f.flush()
            try:
                result = subprocess.run(args, cwd=cwd or self.scratch, env=self.env,
                                        stdout=f, stderr=subprocess.STDOUT, timeout=timeout)
            except subprocess.TimeoutExpired:
                self.note("command timed out", command=args, log=log.name)
                raise
        output = log.read_text().split("\n", 1)[1]
        self.note("command", command=args, returncode=result.returncode, log=log.name)
        if check and result.returncode:
            raise RuntimeError(f"{args!r} exited {result.returncode}; see {log.name}: {output[-2000:]}")
        return subprocess.CompletedProcess(args, result.returncode, output)

    def pid(self) -> int:
        try:
            return int((self.state_dir / "pid").read_text())
        except (OSError, ValueError):
            return 0

    def assert_alive(self, pid: int) -> None:
        if pid <= 0 or self.pid() != pid:
            raise RuntimeError(f"process replaced/disappeared: expected {pid}, pid file {self.pid()}")
        try:
            os.kill(pid, 0)
        except ProcessLookupError as e:
            raise RuntimeError(f"process {pid} died") from e

    def windows(self, pid: int) -> dict:
        result = self.command(str(self.project / ".venv/bin/python"), str(Path(__file__).resolve()),
                              "--windows", str(pid))
        return json.loads(result.stdout)

    def startup(self, after: float, old_pid: int = 0, runs: int = 1) -> int:
        def ready():
            pid = self.pid()
            state = read_json(self.state_dir / "state.json")
            return (pid > 0 and pid != old_pid and fresh_state(state, time.time(), after),
                    {"pid": pid, "state": state})
        evidence = wait_for("new process and fresh state", ready)
        pid = evidence["pid"]
        self.assert_alive(pid)
        loaded = self.command("launchctl", "print", self.domain).stdout
        verify_launchd(loaded, pid, runs)
        def native_ready():
            self.assert_alive(pid)
            win = self.windows(pid)
            doctor = self.command(str(self.cli), "doctor", "--doctor-json", check=False)
            data = json.loads(doctor.stdout)
            return native_healthy(win, doctor.returncode, data), {"window": win, "doctor": data}
        wait_for("native window and doctor", native_ready)
        self.health(pid)
        healthy, evidence = native_ready()
        if not healthy:
            raise RuntimeError(f"native health lost during survival interval: {evidence}")
        verify_launchd(self.command("launchctl", "print", self.domain).stdout, pid, runs)
        status = self.command(str(self.cli), "status").stdout
        if "RUNNING" not in status or "TICKING" not in status or f"pid {pid}" not in status:
            raise RuntimeError(f"status did not report this healthy process: {status}")
        return pid

    def health(self, pid: int) -> None:
        # Exceeds the app's 10s startup watchdog. Never restart/retry a failed boot.
        deadline = time.monotonic() + 15
        stamps: set[float] = set()
        while True:
            self.assert_alive(pid)
            state = read_json(self.state_dir / "state.json")
            if not fresh_state(state, time.time(), 0):
                raise RuntimeError(f"watcher stopped ticking during health interval: {state}")
            stamps.add(state["timestamp"])
            if time.monotonic() >= deadline:
                break
            time.sleep(0.25)
        if len(stamps) < 10:
            raise RuntimeError(f"only {len(stamps)} distinct watcher ticks over 15s")
        self.note("healthy for 15 seconds", pid=pid, distinct_ticks=len(stamps))

    def force(self, pid: int, state: str) -> None:
        self.stage = f"force-{state}"
        after = time.time()
        (self.state_dir / "force_state").write_text(state)
        def applied():
            self.assert_alive(pid)
            value = read_json(self.state_dir / "state.json")
            return fresh_state(value, time.time(), after, state), value
        first = wait_for("forced watcher state", applied)
        after = first["timestamp"]
        second = wait_for("second forced watcher tick", applied)
        (self.artifacts / f"state-{state}.json").write_text(json.dumps(second, indent=2))
        win = self.windows(pid)["sprite"]
        if not win:
            raise RuntimeError(f"no native sprite window in {state}")
        self.screenshot(state, win)

    def screenshot(self, state: str, win: dict) -> None:
        # Diagnostic snapshots only: animations/mood overlays are intentionally live.
        target = self.artifacts / f"{state}.png"
        try:
            capture = self.command("/usr/sbin/screencapture", "-x", "-o", f"-l{win['id']}",
                                   str(target), check=False, timeout=10)
            captured = capture.returncode == 0 and target.exists()
            reason = "captured" if captured else f"screencapture exit {capture.returncode}"
        except subprocess.TimeoutExpired:
            captured, reason = False, "screencapture timed out"
        self.note("screenshot", state=state, captured=captured, reason=reason,
                  window=win, path=target.name)

    def stopped(self, pid: int) -> None:
        def gone():
            try:
                os.kill(pid, 0)
                return False, f"pid {pid} still alive"
            except ProcessLookupError:
                return True, f"pid {pid} exited"
        wait_for("shutdown", gone)
        result = self.command("launchctl", "print", self.domain, check=False)
        if result.returncode == 0:
            raise RuntimeError("LaunchAgent still registered after stop")
        if self.command(str(self.cli), "status", check=False).returncode == 0:
            raise RuntimeError("status reported success after stop")
        # Check no delayed watcher writes or KeepAlive resurrection across >2 ticks.
        before = (self.state_dir / "state.json").read_bytes()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if (self.state_dir / "state.json").read_bytes() != before:
                raise RuntimeError("watcher wrote state after stop")
            time.sleep(0.25)

    def run(self) -> None:
        require_hosted_macos(platform.system(), dict(os.environ))
        for path in (self.state_dir, self.plist, self.cli, Path.home() / ".indigo-pet",
                     Path("/tmp/squid-pet.out.log"), Path("/tmp/squid-pet.err.log")):
            if path.exists():
                raise RuntimeError(f"refusing to replace pre-existing installation: {path}")
        if self.command("launchctl", "print", self.domain, check=False).returncode == 0:
            raise RuntimeError("refusing to touch an existing Squid LaunchAgent")
        self.command("launchctl", "print", f"gui/{os.getuid()}")
        self.command("sw_vers")
        self.command("uname", "-m")
        self.stage = "clean-install"
        seed = self.scratch / "seed.git"
        # Installer clones the candidate commit, never upstream main or a previous PR.
        self.command("git", "init", "--bare", "--initial-branch=main", str(seed))
        self.command("git", "push", str(seed), "HEAD:refs/heads/main", cwd=self.checkout)
        expected = self.command("git", "rev-parse", "HEAD", cwd=self.checkout).stdout.strip()
        self.env.update(SQUID_PROJECT=str(self.project), SQUID_REPO=str(seed))
        self.owns_install = True
        after = time.time()
        self.command("bash", str(self.checkout / "install.sh"), "--non-interactive", timeout=300)
        actual = self.command("git", "rev-parse", "HEAD", cwd=self.project).stdout.strip()
        if actual != expected:
            raise RuntimeError(f"installer tested wrong commit: {actual} != {expected}")
        self.command("plutil", "-lint", str(self.plist))
        self.command(str(self.project / ".venv/bin/python"), "-c",
                     "import webview, AppKit, Quartz, WebKit; print('native imports OK')")
        pid = self.startup(after)
        # Keep screenshot runs quiet without disabling any native subsystem.
        (self.state_dir / "config.json").write_text(json.dumps({"muted": True, "approval_alert_enabled": False}))
        for state in ("idle", "thinking", "working", "approval_needed", "celebrating", "sleeping"):
            self.force(pid, state)
        (self.state_dir / "force_state").unlink()
        after = time.time()
        wait_for("return to detector state", lambda: (
            fresh_state(s := read_json(self.state_dir / "state.json"), time.time(), after)
            and not s.get("state_reason", "").startswith("force_state"), s))
        self.stage = "restart"
        after = time.time()
        self.command(str(self.cli), "restart")
        new_pid = self.startup(after, pid, runs=2)
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            pass
        else:
            raise RuntimeError("old process survived restart")
        self.stage = "stop"
        self.command(str(self.cli), "stop")
        self.stopped(new_pid)
        self.stage = "start-after-stop"
        after = time.time()
        self.command(str(self.cli), "start")
        pid = self.startup(after, new_pid)
        self.stage = "final-stop"
        self.command(str(self.cli), "stop")
        self.stopped(pid)

    def diagnostics(self) -> None:
        if not self.owns_install:
            return
        self.command("launchctl", "print", self.domain, check=False)
        self.command("ps", "-axo", "pid,ppid,etime,command", check=False)
        for path in (self.plist, *self.state_dir.glob("*.json"), self.state_dir / "force_state",
                     self.state_dir / "squid.log", self.state_dir / "logs/install-history.log",
                     Path("/tmp/squid-pet.out.log"), Path("/tmp/squid-pet.err.log")):
            if path.is_file():
                shutil.copy2(path, self.artifacts / path.name)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--windows", type=int)
    parser.add_argument("--artifacts", type=Path, default=Path("native-artifacts"))
    parser.add_argument("--scratch", type=Path)
    args = parser.parse_args()
    if args.windows is not None:
        print(json.dumps(native_windows(args.windows)))
        return 0
    # Guard before creating directories or collecting personal diagnostics.
    require_hosted_macos(platform.system(), dict(os.environ))
    args.artifacts.mkdir(parents=True, exist_ok=True)
    if args.scratch is None:
        parser.error("--scratch is required and must be a new directory")
    args.scratch.mkdir(parents=True, exist_ok=False)
    smoke = Smoke(Path(__file__).resolve().parents[1], args.artifacts.resolve(), args.scratch.resolve())
    error = None
    try:
        smoke.run()
    except Exception:
        error = traceback.format_exc()
        (smoke.artifacts / "failure.txt").write_text(error)
    finally:
        try:
            smoke.diagnostics()
        except Exception:
            (smoke.artifacts / "diagnostics-error.txt").write_text(traceback.format_exc())
        if smoke.owns_install:
            try:
                smoke.command("launchctl", "bootout", smoke.domain, check=False)
            except Exception:
                (smoke.artifacts / "cleanup-error.txt").write_text(traceback.format_exc())
    summary = f"Native smoke {'FAILED' if error else 'PASSED'} at {smoke.stage}\n"
    if error:
        summary += error
    (smoke.artifacts / "summary.md").write_text(summary)
    print(summary, flush=True)
    return int(error is not None)


if __name__ == "__main__":
    sys.exit(main())
