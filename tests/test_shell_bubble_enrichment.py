"""Tests for the enriched working-state bubble (enrich-working-command-bubble).

Covers recovering the real command from Claude Code's `zsh -c` wrapper and
rendering "runs {what}[ in {where}]" under the 32-char cap. The wrapper
fixtures use the REAL preamble captured live from a Bash-tool invocation:

    /bin/zsh -c source <snapshot>.sh 2>/dev/null || true
      && setopt NO_EXTENDED_GLOB NO_BARE_GLOB_QUAL 2>/dev/null || true
      && { \\builtin unalias ...; } >/dev/null 2>&1 || true
      && eval '<REAL COMMAND>' < /dev/null
      && pwd -P >| /tmp/claude-XXXX-cwd
"""
from __future__ import annotations

import pytest

from squid_pet.observer import (
    Observer,
    _shell_cmd_bubble,
    _unwrap_eval_payload,
    _parse_command,
    _looks_like_path,
    MAX_BUBBLE_CHARS,
)

# Real preamble (captured live), parameterized by the embedded command.
_SNAP = "/Users/x/.claude/shell-snapshots/snapshot-zsh-1788729410756-e0a6uk.sh"
_PREAMBLE = (
    f"source {_SNAP} 2>/dev/null || true && "
    "setopt NO_EXTENDED_GLOB NO_BARE_GLOB_QUAL 2>/dev/null || true && "
    "{ \\builtin unalias -- 'unsetenv'; \\builtin unset -f -- 'unsetenv'; } "
    ">/dev/null 2>&1 || true && "
)
_TRAILER = " < /dev/null && pwd -P >| /tmp/claude-f880-cwd"


def wrap(cmd: str) -> list[str]:
    """Build a realistic zsh -c wrapper cmdline for a real command."""
    return ["/bin/zsh", "-c", f"{_PREAMBLE}eval '{cmd}'{_TRAILER}"]


# ---------------------------------------------------------------------------
# _unwrap_eval_payload
# ---------------------------------------------------------------------------

def test_unwrap_extracts_eval_payload_from_real_wrapper():
    assert _unwrap_eval_payload(wrap("pytest tests/ -v")) == "pytest tests/ -v"


def test_unwrap_decodes_octal_newlines_and_escaped_quotes():
    # Claude encodes newlines as \012 and embedded single quotes as '\''.
    cmd_on_wire = "echo hi\\012git commit -m '\\''x'\\''"
    assert _unwrap_eval_payload(wrap(cmd_on_wire)) == "echo hi\ngit commit -m 'x'"


def test_unwrap_plain_sh_dash_c_without_eval_uses_the_script():
    assert _unwrap_eval_payload(
        ["/bin/sh", "-c", "brew install ripgrep && echo done"]
    ) == "brew install ripgrep && echo done"


def test_unwrap_non_wrapper_joins_the_args():
    assert _unwrap_eval_payload(["pytest", "-v", "tests/"]) == "pytest -v tests/"


def test_unwrap_empty_or_empty_script_is_none():
    assert _unwrap_eval_payload([]) is None
    assert _unwrap_eval_payload(["/bin/zsh", "-c", ""]) is None


# ---------------------------------------------------------------------------
# _looks_like_path / _parse_command
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("tok,expected", [
    ("tests/", True),
    ("src/squid_pet/observer.py", True),
    ("README.md", True),
    ("ripgrep", False),      # package name
    ("build", False),        # npm script
    ("origin", False),       # git remote
    ("fix", False),          # commit message word
])
def test_looks_like_path(tok, expected):
    assert _looks_like_path(tok) is expected


def test_parse_two_word_tool_keeps_subcommand():
    assert _parse_command("git push origin main") == ("git push", None)


def test_parse_path_arg_becomes_where():
    assert _parse_command("pytest tests/ -v") == ("pytest", "tests/")


def test_parse_commit_message_is_not_a_where():
    what, where = _parse_command('git commit -m "fix wake-from-sleep beat"')
    assert what == "git commit"
    assert where is None


def test_parse_grep_pattern_skipped_but_path_used():
    assert _parse_command("grep -rn shell_cmdline src/squid_pet/") == (
        "grep", "src/squid_pet/")


def test_parse_skips_leading_cd_noise():
    assert _parse_command("cd src && ruff check squid_pet/observer.py") == (
        "ruff", "squid_pet/observer.py")


def test_parse_noise_only_payload_is_none():
    assert _parse_command("cd /tmp") == (None, None)


def test_parse_skips_leading_comment_line():
    # A leading `# comment` line must not become the command ("runs #").
    assert _parse_command("# do the thing\npytest tests/ -v") == ("pytest", "tests/")
    assert _parse_command("# just a comment") == (None, None)


@pytest.mark.parametrize("payload,expected", [
    # Redirect targets must never be taken as a location (caught live:
    # `git status >/dev/null 2>&1` was rendering "in >/dev/null").
    ("git status --short >/dev/null 2>&1", ("git status", None)),
    ("cat log.txt > out.txt",              ("cat", "log.txt")),  # source, not the > target
    ("grep foo src/ > /tmp/hits",          ("grep", "src/")),    # separated > target skipped
    ("python app.py 2>&1",                 ("python", "app.py")),
])
def test_parse_ignores_redirects(payload, expected):
    assert _parse_command(payload) == expected


# ---------------------------------------------------------------------------
# _shell_cmd_bubble -- end-to-end rendering (name dropped, cap enforced)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("cmd,expected", [
    ("pytest tests/ -v",                         "runs pytest in tests/"),
    ("pytest tests/test_observer.py -v",         "runs pytest in test_observer.py"),
    ("git push origin main",                     "runs git push"),
    ('git commit -m "fix wake-from-sleep beat"', "runs git commit"),
    ("grep -rn shell_cmdline src/squid_pet/",    "runs grep in src/squid_pet/"),
    ("cd src && ruff check squid_pet/observer.py", "runs ruff in observer.py"),
    ("brew install ripgrep",                     "runs brew install"),
    ("rm -rf dist/",                             "runs rm in dist/"),
    ("cat README.md",                            "runs cat in README.md"),
    ("npm run build",                            "runs npm run"),
])
def test_bubble_from_real_wrapper(cmd, expected):
    assert _shell_cmd_bubble(wrap(cmd)) == expected


def test_bubble_truncation_ladder_full_to_parent_dir():
    # basename too long for the cap -> degrade to the parent dir.
    out = _shell_cmd_bubble(wrap("python3 scripts/squid_task_complete.py"))
    assert out == "runs python3 in scripts/"
    assert len(out) <= MAX_BUBBLE_CHARS


def test_every_real_case_fits_the_cap():
    for cmd in ["pytest tests/test_observer.py -v", "git push origin main",
                "grep -rn shell_cmdline src/squid_pet/", "rm -rf dist/",
                "python3 scripts/squid_task_complete.py"]:
        out = _shell_cmd_bubble(wrap(cmd))
        assert out is not None and len(out) <= MAX_BUBBLE_CHARS


# ---------------------------------------------------------------------------
# Fallback: unparseable wrapper never leaks internals
# ---------------------------------------------------------------------------

def test_wrapper_without_eval_anchor_and_only_noise_returns_none():
    # A wrapper whose recoverable command is pure noise -> no bubble.
    assert _shell_cmd_bubble(["/bin/zsh", "-c", "cd /tmp"]) is None


def test_on_state_change_falls_back_to_reason_when_bubble_is_none():
    """If _shell_cmd_bubble can't produce anything, on_state_change must
    fall through to the state-reason explanation, never leak the wrapper."""
    o = Observer(get_muted=lambda: False)
    line = o.on_state_change(
        "idle", "working",
        shell_cmdline=["/bin/zsh", "-c", "cd /tmp"],  # noise-only -> None
        state_reason="shell child active (claude_code)",
    )
    assert line == "claude ran a command"       # the reason-map fallback
    assert "eval" not in line and "source" not in line and "/tmp" not in line


def test_bubble_never_shows_snapshot_or_eval_tokens():
    out = _shell_cmd_bubble(wrap("pytest tests/ -v"))
    assert "eval" not in out and "source" not in out and "snapshot" not in out
