"""The hook and the watcher must agree on which Claude Code surfaces exist.

scripts/claude_pet_hook.py writes one of its _KNOWN_SURFACES (or "unknown");
watcher._normalize_surface rewrites anything outside _CLAUDE_KNOWN_SURFACES to
"unknown", which focus treats as a terminal session. A surface added to only
one side would be written correctly and then silently demoted -- so pin the
two sets together, in the style of test_window_constants_agree.py.
Import-based and hermetic (the hook is loaded with SQUID_PET_HOME in tmp).
"""
from __future__ import annotations

from squid_pet import watcher
from tests.test_claude_pet_hook_script import _load_hook_module


def test_hook_and_watcher_know_the_same_surfaces(tmp_path):
    hook = _load_hook_module(tmp_path)
    assert set(hook._KNOWN_SURFACES) == (
        set(watcher._CLAUDE_KNOWN_SURFACES) - {watcher.CLAUDE_SURFACE_UNKNOWN})


def test_hook_and_watcher_share_the_unknown_sentinel(tmp_path):
    hook = _load_hook_module(tmp_path)
    assert hook._SURFACE_UNKNOWN == watcher.CLAUDE_SURFACE_UNKNOWN
    assert watcher.CLAUDE_SURFACE_UNKNOWN in watcher._CLAUDE_KNOWN_SURFACES
    assert watcher.CLAUDE_SURFACE_UNKNOWN not in hook._KNOWN_SURFACES


def test_the_desktop_surface_focus_acts_on_is_one_the_hook_writes(tmp_path):
    hook = _load_hook_module(tmp_path)
    assert watcher.CLAUDE_SURFACE_DESKTOP in hook._KNOWN_SURFACES
