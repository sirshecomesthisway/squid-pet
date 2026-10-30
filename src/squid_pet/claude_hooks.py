"""Install and inspect Claude's user hooks. Never called by the 1 Hz watcher.

Configuration proves registration, not delivery: project/managed settings and
the session's execution environment can still prevent a registered hook running.
Only explicit setup/install/update/uninstall commands write Claude settings.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import sys
import tempfile
from pathlib import Path

EVENTS = (
    "Notification", "PermissionRequest", "PermissionDenied", "UserPromptSubmit",
    "PreToolUse", "PostToolUse", "PostToolUseFailure", "Stop", "StopFailure",
    "SubagentStop", "PreCompact", "PostCompact", "SessionEnd",
)
QUESTION_MATCHER = "^(AskUserQuestion|ExitPlanMode)$"
COVERAGE = (
    "Local Claude CLI and Desktop Code use user hooks. Registration does not "
    "prove delivery: check /hooks in Claude and test a fresh question. "
    "Project/session settings and managed policy can disable hooks. "
    "Cowork's separate configuration is not wired by this setup; its local "
    "transcripts provide activity only, not question/approval alerts. "
    "Remote, VM and cloud sessions are not covered by local hooks."
)


def settings_paths() -> list[Path]:
    """Always include Desktop's default, plus an explicitly configured CLI home."""
    paths = [Path.home() / ".claude" / "settings.json"]
    custom = os.environ.get("CLAUDE_CONFIG_DIR")
    if custom:
        path = Path(custom).expanduser().absolute() / "settings.json"
        if path.resolve() != paths[0].resolve():
            paths.append(path)
    return paths


def _tokens(handler: dict) -> list[str]:
    command = handler.get("command")
    if handler.get("type") != "command" or not isinstance(command, str):
        return []
    try:
        return shlex.split(command)
    except ValueError:
        return []


def is_ours(handler: dict) -> bool:
    """Recognize direct legacy hooks and Python invocations, including old clones.

    Do not match arbitrary shell snippets merely mentioning the receiver.
    """
    tokens = _tokens(handler)
    return bool(tokens and Path(tokens[-1]).name == "claude_pet_hook.py" and (
        len(tokens) == 1 or
        (len(tokens) == 2 and re.fullmatch(r"python(?:\d+(?:\.\d+)*)?",
                                         Path(tokens[0]).name))))


def _read(path: Path) -> tuple[str | None, dict]:
    try:
        original = path.read_text()
    except FileNotFoundError:
        return None, {}
    data = json.loads(original)
    if not isinstance(data, dict) or not isinstance(data.get("hooks", {}), dict):
        raise ValueError("settings and hooks must be JSON objects")
    for groups in data.get("hooks", {}).values():
        if not isinstance(groups, list):
            raise ValueError("hook events must contain lists of groups")
        for group in groups:
            if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
                raise ValueError("hook groups must contain a hooks list")
            if not all(isinstance(h, dict) for h in group["hooks"]):
                raise ValueError("hook handlers must be objects")
    return original, data


def configure(path: Path, *, remove: bool = False,
              script: Path | None = None) -> bool:
    """Merge only Squid handlers; back up once, then atomically replace the file.

    Resolve a dotfile symlink so installation preserves the symlink itself.
    Invalid settings are never overwritten. A second identical run is a no-op.
    """
    path = path.expanduser().resolve()
    original, data = _read(path)
    if original is None and remove:
        return False
    if remove and not any(is_ours(h) for groups in data.get("hooks", {}).values()
                          for group in groups for h in group["hooks"]):
        return False
    hooks = data.setdefault("hooks", {})
    for event, groups in list(hooks.items()):
        kept = []
        for group in groups:
            remaining = [h for h in group["hooks"] if not is_ours(h)]
            if remaining == group["hooks"]:
                kept.append(group)
            elif remaining:
                kept.append({**group, "hooks": remaining})
        if kept or not groups:
            hooks[event] = kept
        else:
            del hooks[event]
    if not remove:
        script = script or Path(__file__).resolve().parents[2] / "scripts" / "claude_pet_hook.py"
        if not script.is_file():
            raise ValueError("Claude hook receiver is missing; run setup from the Squid checkout")
        command = shlex.join([sys.executable, str(script.resolve())])
        for event in EVENTS:
            hooks.setdefault(event, []).append({
                "matcher": QUESTION_MATCHER if event == "PreToolUse" else "",
                "hooks": [{"type": "command", "command": command, "timeout": 5}],
            })
    elif not hooks:
        data.pop("hooks")
    # Comparing parsed data avoids touching formatting/mtime on a no-op.
    if original is not None and json.loads(original) == data:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = path.stat().st_mode & 0o777 if original is not None else 0o600
    if original is not None:
        backup = path.with_name(path.name + ".squid-backup")
        try:
            fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        except FileExistsError:
            pass
        else:
            with os.fdopen(fd, "w") as stream:
                stream.write(original)
    fd, name = tempfile.mkstemp(prefix=".squid-hooks-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            os.fchmod(stream.fileno(), mode)
            json.dump(data, stream, indent=2, ensure_ascii=False)
            stream.write("\n")
        # A concurrent settings edit must not be silently discarded.
        current = path.read_text() if path.exists() else None
        if current != original:
            raise ValueError("settings changed during setup; retry")
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)
    return True


def _matches(event: str, group: dict) -> bool:
    matcher = group.get("matcher", "")
    if matcher in ("", "*"):
        return True
    if not isinstance(matcher, str):
        return False
    names = {"PreToolUse": ("AskUserQuestion", "ExitPlanMode"),
             "Notification": ("permission_prompt",)}.get(event)
    if names is None:
        # Clearing/attribution hooks must cover all tools/owners.
        return False
    try:
        return all(re.search(matcher, name) is not None for name in names)
    except re.error:
        return False


def inspect_settings(path: Path) -> dict:
    """Static, read-only health report; no tool or message content is returned."""
    path = path.expanduser()
    report: dict = {"settings_file": str(path), "status": "missing",
              "missing_events": list(EVENTS), "broken_events": [],
              "fix": "squid setup-claude"}
    try:
        original, data = _read(path)
        if original is None:
            return report
        missing, broken = [], []
        for event in EVENTS:
            handlers = [h for g in data.get("hooks", {}).get(event, []) if _matches(event, g)
                        for h in g["hooks"] if is_ours(h)]
            if not handlers:
                missing.append(event)
            elif not any(all(Path(t).is_absolute() and Path(t).is_file() for t in _tokens(h))
                         and os.access(_tokens(h)[0], os.X_OK)
                         for h in handlers):
                broken.append(event)
        report.update(missing_events=missing, broken_events=broken)
        report["status"] = ("disabled" if data.get("disableAllHooks") is True else
                            "partial" if missing else "broken" if broken else "registered")
    except (OSError, ValueError, TypeError):
        report["status"] = "invalid"
    return report


def integration_report() -> dict:
    configs = [inspect_settings(p) for p in settings_paths()]
    return {"registered": all(c["status"] == "registered" for c in configs),
            "configs": configs, "coverage": COVERAGE}


def report_lines(report: dict) -> list[str]:
    lines = []
    for config in report["configs"]:
        status = config["status"]
        lines.append(f"{config['settings_file']}: {status}")
        if status != "registered":
            lines.append(f"WARN: Claude question/approval signals may be unavailable; "
                         f"run {config['fix']} (disabled hooks/policy need your review)")
    lines.append(report["coverage"])
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Register/remove Squid's Claude user hooks")
    parser.add_argument("--settings-file", type=Path, help="Target one explicit settings file")
    parser.add_argument("--remove", action="store_true", help="Remove only Squid's handlers")
    parser.add_argument("--check", action="store_true", help="Inspect registration without writing")
    args = parser.parse_args(argv)
    if args.remove and args.check:
        parser.error("--remove and --check cannot be combined")
    paths = [args.settings_file] if args.settings_file else settings_paths()
    failed = False
    for path in paths:
        try:
            if not args.check:
                changed = configure(path, remove=args.remove)
                print(f"{path}: {'updated' if changed else 'unchanged'}")
            if not args.remove:
                config = inspect_settings(path)
                print(f"{path}: {config['status']}")
                if config["status"] != "registered":
                    failed = True
        except (OSError, ValueError) as e:
            print(f"{path}: setup failed ({type(e).__name__}); settings preserved. "
                  "Check JSON/permissions and retry squid setup-claude.", file=sys.stderr)
            failed = True
    if not args.remove:
        print(COVERAGE)
        print("A prompt already on screen is not replayed. Test a fresh question; "
              "reopen the Claude session if hooks have not loaded.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
