"""Pink-2026-09-19: take-me-there must follow the SAME signal that produced
the face.

focus._freshest_in() read the flag dirs with no freshness rule while the
watcher honoured one, so the two could disagree. The dirs are only *deleted*
on their 2h stale sweep -- far longer than any "still counts" window -- so a
long-dead flag could still be picked as the session to raise. That is the
exact wrong-window class this module exists to fix.
"""
from __future__ import annotations

import os
import time

from squid_pet import focus, watcher


def _flag(dir_path, session_id, age_sec):
    os.makedirs(dir_path, exist_ok=True)
    p = os.path.join(dir_path, session_id)
    with open(p, "w") as f:
        f.write("")
    old = time.time() - age_sec
    os.utime(p, (old, old))
    return p


def test_freshest_in_is_unbounded_by_default():
    """Existing callers that pass no window keep the old behaviour."""
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        _flag(d, "ancient", 10_000)
        assert focus._freshest_in(d) == "ancient"


def test_freshest_in_ignores_entries_past_the_window():
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        _flag(d, "ancient", 10_000)
        assert focus._freshest_in(d, 300) is None


def test_freshest_in_never_deletes():
    """Pruning belongs to the watcher's stale sweep. A double-click must not
    be able to destroy state the watcher is still reasoning about."""
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        p = _flag(d, "ancient", 10_000)
        focus._freshest_in(d, 300)
        assert os.path.exists(p), "focus deleted a flag it only meant to ignore"


def test_freshest_in_prefers_the_newest_still_fresh_entry():
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        _flag(d, "older", 200)
        _flag(d, "newer", 10)
        _flag(d, "stale", 10_000)
        assert focus._freshest_in(d, 300) == "newer"


def test_concerned_window_is_derived_from_the_one_that_decides_the_face():
    """Not an independent policy: the face itself lasts CLAUDE_FAILED_FRESH_SEC
    (CONCERN_DISMISS_SEC is defined as exactly that), so the lookup window is
    that same value -- no new magic number -- plus only the small grace margin
    that covers the watcher/frontend poll lag (see
    test_the_bound_has_slack_for_the_sprite_lag)."""
    assert watcher.CONCERN_DISMISS_SEC == watcher.CLAUDE_FAILED_FRESH_SEC
    assert (focus._state_fresh_sec("concerned")
            == watcher.CLAUDE_FAILED_FRESH_SEC + focus._FRESHNESS_GRACE_SEC)


def test_every_other_state_is_deliberately_left_unbounded():
    """Documents a KNOWN GAP, and pins the scope so it cannot widen by accident.

    `working`, `thinking`, `celebrating` and `grooving` carry the same latent
    bug -- their flags outlive their "still counts" window by up to the 2h
    sweep, and all of them can also be raised by Codex or a Git commit with no
    Claude flag at all. They are NOT bounded here, on purpose.

    An earlier revision of this change did bound them. It was inert: with the
    unbounded fallback that had to be added alongside (so a state held past its
    marker -- celebrating under celebrate_until -- still resolved the session
    that did the work), the bounded lookup could only ever return the same sid
    as the unbounded one, because _freshest_in returns the global newest or
    None. Dead code plus a false claim in the changelog.

    Bounding them for real means deciding what happens when no session can be
    named, and that collides with a deliberate, tested UX choice: raise *a*
    window rather than do nothing
    (test_an_active_state_with_no_flag_still_raises_the_app). That is a product
    call, not a bug fix, so it stays on the PR as a known gap.
    """
    for state in ("working", "thinking", "celebrating", "grooving"):
        assert focus._state_fresh_sec(state) is None


def test_the_bound_actually_changes_the_lookup_for_concerned(tmp_path):
    """Guards against the inertness above ever coming back: the window must
    make a real difference to what _freshest_in returns, not just to what
    _state_fresh_sec reports."""
    d = tmp_path / "failed"
    _flag(str(d), "aged-out", watcher.CLAUDE_FAILED_FRESH_SEC + 60)
    assert focus._freshest_in(str(d)) == "aged-out"                      # unbounded
    assert focus._freshest_in(str(d), focus._state_fresh_sec("concerned")) is None


def test_approval_needed_is_left_unbounded_for_now():
    """Documents a KNOWN gap rather than endorsing it.

    A session waiting on you stays waiting however long you take, so mtime is
    genuinely not the right clock here. But the watcher does gate this dir --
    by _CLAUDE_SESSION_SNOOZE_SEC (120s from first-seen), not by staleness --
    so a snoozed Claude prompt can still be picked when a *Codex* request is
    what re-raised approval_needed. Fixing that needs the snooze's first-seen
    bookkeeping, not a freshness window; see the PR checklist.
    """
    assert focus._state_fresh_sec("approval_needed") is None


def test_a_stale_claude_failure_no_longer_hijacks_a_codex_concern(tmp_path, monkeypatch):
    """The scenario the review found, end to end.

    A Claude session fails at T. At T+400s it is no longer fresh, so she stops
    looking concerned -- but the flag is still on disk (deleted only by the 2h
    sweep). At T+600s Codex fails, so she is concerned again, from a Codex
    signal that has no per-session flag dir. Double-clicking used to read the
    stale *Claude* flag and raise that old session's tab.
    """
    failed_dir = tmp_path / "claude_failed"
    _flag(str(failed_dir), "stale-claude-session", watcher.CLAUDE_FAILED_FRESH_SEC + 300)
    monkeypatch.setitem(focus.STATE_SIGNAL_DIRS, "concerned", str(failed_dir))

    assert focus.focus_for_state("concerned", run=lambda s: "") == "resting", (
        "a stale Claude failure was still being used to pick the window"
    )


def test_a_fresh_claude_failure_is_still_honoured(tmp_path, monkeypatch):
    """The bound must not break the feature it is protecting: a genuinely
    fresh failure still names its session."""
    failed_dir = tmp_path / "claude_failed"
    _flag(str(failed_dir), "live-session", 5)
    assert focus._freshest_in(str(failed_dir),
                              focus._state_fresh_sec("concerned")) == "live-session"


def test_single_source_states_keep_their_blind_fallback(tmp_path, monkeypatch):
    """The bound must NOT become a blanket 'no flag -> do nothing'.

    Celebrating can outlive its marker's freshness window, and only Claude
    ever produces it, so raising the right app still beats doing nothing --
    the deliberate behaviour pinned by
    test_an_active_state_with_no_flag_still_raises_the_app. This is here so a
    future tightening of the concerned fix cannot quietly take it away.
    """
    monkeypatch.setitem(focus.STATE_SIGNAL_DIRS, "celebrating", str(tmp_path / "empty"))
    monkeypatch.setattr("squid_pet.watcher.find_claude_code_processes", lambda: [])
    monkeypatch.setattr("squid_pet.watcher.find_terminal_app_bundle_for_claude_code",
                        lambda: "com.googlecode.iterm2")
    assert focus.focus_for_state("celebrating", run=lambda s: "") == "app-only"


def test_only_multi_source_states_refuse_to_guess():
    """Why concerned is special: Codex can raise it, and a Codex failure
    leaves no per-session flag, so 'nothing fresh' can mean 'no Claude
    session was involved' rather than 'the flag aged out'."""
    assert "concerned" in focus._MULTI_SOURCE_STATES
    for single_source in ("celebrating", "grooving", "working", "thinking",
                          "approval_needed"):
        assert single_source not in focus._MULTI_SOURCE_STATES


def test_a_named_but_dead_session_still_raises_the_app(tmp_path, monkeypatch):
    """The narrow scope of the multi-source guard, pinned deliberately.

    A code review proposed refusing to guess whenever no tab resolves. That
    conflates two cases with different evidence: here a FRESH flag names a
    session, so a Claude turn demonstrably just failed -- we simply cannot
    find its tab because the process exited (common after auth/billing
    failures). Raising the right app is a reasonable best effort, and is this
    feature author's documented choice. Only "nothing fresh named a session"
    -- where the concern can only have come from Codex -- refuses to guess.
    """
    d = tmp_path / "failed"
    _flag(str(d), "sess-gone", 5)                       # fresh, so sid resolves
    monkeypatch.setitem(focus.STATE_SIGNAL_DIRS, "concerned", str(d))
    monkeypatch.setattr("squid_pet.watcher.claude_session_tty", lambda sid: None)
    monkeypatch.setattr("squid_pet.watcher.find_terminal_app_bundle_for_session",
                        lambda sid: None)
    monkeypatch.setattr("squid_pet.watcher.find_claude_code_processes", lambda: [])
    monkeypatch.setattr("squid_pet.watcher.find_terminal_app_bundle_for_claude_code",
                        lambda: "com.googlecode.iterm2")
    assert focus.focus_for_state("concerned", run=lambda s: "") == "app-only"


def test_the_bound_has_slack_for_exactly_the_clock_that_feeds_it():
    """take_me_there reads PetApi._latest.state, refreshed by the watcher
    thread every POLL_INTERVAL_SEC -- the frontend's own poll never enters
    this decision. Bounding at exactly the watcher's window would make a
    double-click during that one-tick lag do nothing at all, worse than the
    old behaviour.

    The slack is pinned TIGHT on purpose: every second of it is a second in
    which focus accepts a flag the watcher has already rejected, re-opening
    the cross-source hijack the bound exists to close. Two ticks, no more.
    """
    slack = focus._state_fresh_sec("concerned") - watcher.CLAUDE_FAILED_FRESH_SEC
    assert slack > 0
    assert slack <= 2 * watcher.POLL_INTERVAL_SEC, (
        "grace widened beyond the lag it is justified by"
    )


def test_a_just_expired_flag_is_still_honoured_during_the_lag(tmp_path):
    """One watcher tick past the window -- the state we were handed can be
    this stale -- must still resolve, or the double-click silently does
    nothing while she is still showing the face."""
    d = tmp_path / "failed"
    _flag(str(d), "just-expired", watcher.CLAUDE_FAILED_FRESH_SEC + watcher.POLL_INTERVAL_SEC)
    assert focus._freshest_in(str(d), focus._state_fresh_sec("concerned")) == "just-expired"


def test_a_flag_past_the_grace_is_not_honoured(tmp_path):
    """The other edge: the grace is slack for one tick of lag, not a wider
    window. Past it the cross-source guard must engage again."""
    d = tmp_path / "failed"
    _flag(str(d), "too-old", watcher.CLAUDE_FAILED_FRESH_SEC + focus._FRESHNESS_GRACE_SEC + 5)
    assert focus._freshest_in(str(d), focus._state_fresh_sec("concerned")) is None


def test_a_dead_named_session_can_still_raise_an_unrelated_tab(tmp_path, monkeypatch):
    """EXPOSES a pre-existing gap rather than masking it.

    When the named session's process is gone, focus falls back to
    _any_claude_tty(), which returns whichever live claude turns up first. With
    another session alive that selects ITS tab and reports "matched" -- you are
    confidently taken to an unrelated, healthy session.

    This change deliberately does not narrow that: it is the feature author's
    documented "better to raise the right app than to do nothing" choice, and
    changing it is the same guess-vs-nothing product call left on the PR. The
    sibling test stubs the process list empty, which hides this; this one uses
    a live decoy so the behaviour is on record.
    """
    d = tmp_path / "failed"
    _flag(str(d), "sess-dead", 5)                       # fresh -> sid resolves
    monkeypatch.setitem(focus.STATE_SIGNAL_DIRS, "concerned", str(d))
    monkeypatch.setattr("squid_pet.watcher.claude_session_tty", lambda sid: None)
    monkeypatch.setattr("squid_pet.watcher.find_terminal_app_bundle_for_session",
                        lambda sid: None)

    class _Decoy:                                       # an UNRELATED live session
        def terminal(self):
            return "/dev/ttys099"
    monkeypatch.setattr("squid_pet.watcher.find_claude_code_processes", lambda: [_Decoy()])
    monkeypatch.setattr("squid_pet.watcher.find_terminal_app_bundle_for_claude_code",
                        lambda: "com.apple.Terminal")

    seen = []
    # Return a non-empty result, i.e. osascript reporting it DID select a tab
    # (_raise maps an empty result to "app-only").
    result = focus.focus_for_state("concerned",
                                   run=lambda s: seen.append(s) or "matched")
    assert result == "matched"
    assert "/dev/ttys099" in seen[0], (
        "documents that an unrelated session's tab is the one raised"
    )
