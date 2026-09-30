"""Regression tests: _menu_snap() must sync the wanderer's edge tracker
after moving the window, same as next_corner() and drag's _on_end already do.

Found in a 2026-08-17 code review: without the sync, clicking
Position -> <corner> from the right-click menu moves the
window but leaves the sprite rotation (and passthrough's edge-aware
click hit-test offset, which keys off the same tracked edge) stale
until the next wander tick happens to run.

(The Recenter menu item + _menu_recenter() were removed 2026-09-19: after
starting_corner was made authoritative over position.json in load_corner(),
Recenter just jumped to the configured corner -- redundant with the explicit
Position -> <corner> items -- so its regression tests went with it.)

Pink-2026-08-27d: the original fix called wanderer.refresh_edge()
(distance-based classification from live window origin), but that
never actually worked for corner-snapped positions -- move_to_corner's
own EDGE_MARGIN(20) was never coordinated with wanderer.py's tighter
margins tuned for wander-walk targets, so a freshly corner-snapped
window sits just outside EDGE_BAND_PX and refresh_edge() silently
returns "" (no edge), leaving the sprite un-rotated regardless. Fixed
by using force_edge() with an edge derived directly from the corner
name (window._edge_for_corner) instead of inferring it from distance.

PetApi.__init__ pulls in real pywebview/AppKit state, so these tests
build a minimal double via __new__ + direct attribute assignment
(same pattern as test_passthrough_state_mapping.py's controller
double) rather than constructing a real PetApi.
"""
from __future__ import annotations

import threading
from unittest.mock import MagicMock, patch

from squid_pet.window import CORNERS, PetApi, _edge_for_corner


def _make_api(wanderer=None):
    api = PetApi.__new__(PetApi)
    api._wanderer = wanderer
    api._lock = threading.Lock()
    api._hint_text = ""
    api._hint_seq = 0
    return api


def test_menu_snap_refreshes_edge_on_success():
    fake_wanderer = MagicMock()
    api = _make_api(wanderer=fake_wanderer)
    with patch("squid_pet.window.move_to_corner", return_value=True):
        api._menu_snap("top-left")
    fake_wanderer.force_edge.assert_called_once_with("top")


def test_menu_snap_does_not_refresh_edge_on_move_failure():
    fake_wanderer = MagicMock()
    api = _make_api(wanderer=fake_wanderer)
    with patch("squid_pet.window.move_to_corner", return_value=False):
        api._menu_snap("top-left")
    fake_wanderer.force_edge.assert_not_called()


def test_menu_snap_tolerates_missing_wanderer():
    api = _make_api(wanderer=None)
    with patch("squid_pet.window.move_to_corner", return_value=True):
        api._menu_snap("top-left")  # must not raise


def test_menu_snap_tolerates_refresh_edge_exception():
    fake_wanderer = MagicMock()
    fake_wanderer.force_edge.side_effect = RuntimeError("boom")
    api = _make_api(wanderer=fake_wanderer)
    with patch("squid_pet.window.move_to_corner", return_value=True):
        api._menu_snap("top-left")  # must not raise


def test_next_corner_syncs_edge_via_force_edge():
    """next_corner() (right-click cycling) has the exact same
    corner-snap-vs-wander-margin mismatch as menu snap/startup --
    same fix, same regression coverage."""
    fake_wanderer = MagicMock()
    api = _make_api(wanderer=fake_wanderer)
    api._corner = "top-right"
    with patch("squid_pet.window.move_to_corner", return_value=True):
        new_corner = api.next_corner()
    assert new_corner == "bottom-right"
    fake_wanderer.force_edge.assert_called_once_with("bottom")


def test_next_corner_cycles_in_memory_without_persisting():
    """Option A (Pink-2026-09-23): corner cycling is in-session only. It must
    still advance through CORNERS in order using in-memory self._corner -- with
    no save_corner() (removed) and nothing written to disk."""
    from squid_pet import window as win

    assert not hasattr(win, "save_corner")  # persistence primitive is gone
    api = _make_api(wanderer=MagicMock())
    api._corner = "bottom-right"
    seen = []
    with patch("squid_pet.window.move_to_corner", return_value=True):
        for _ in range(len(win.CORNERS)):
            seen.append(api.next_corner())
    # one full lap, ending back where we started; order follows CORNERS
    start = win.CORNERS.index("bottom-right")
    expected = [win.CORNERS[(start + 1 + i) % len(win.CORNERS)]
                for i in range(len(win.CORNERS))]
    assert seen == expected
    assert seen[-1] == "bottom-right"
    assert api._corner == "bottom-right"


def test_edge_for_corner_uses_bottom_top_priority():
    """Matches wanderer._compute_edge_at's own bottom>top>left>right
    corner tiebreak (see its docstring) -- every CORNERS entry leads
    with 'top' or 'bottom' so that's the only distinction that matters."""
    assert _edge_for_corner("top-right") == "top"
    assert _edge_for_corner("top-left") == "top"
    assert _edge_for_corner("bottom-right") == "bottom"
    assert _edge_for_corner("bottom-left") == "bottom"
    assert _edge_for_corner("unknown") == ""


def test_menu_snap_updates_in_memory_corner_and_cycling_continues_from_it():
    api = _make_api(wanderer=MagicMock())
    api._corner = "bottom-right"
    with patch("squid_pet.window.move_to_corner", return_value=True):
        api._menu_snap("top-left")
        assert api._corner == "top-left"
        assert api.next_corner() == CORNERS[(CORNERS.index("top-left") + 1) % len(CORNERS)]


def test_menu_snap_keeps_corner_when_move_fails():
    api = _make_api(wanderer=MagicMock())
    api._corner = "bottom-right"
    with patch("squid_pet.window.move_to_corner", return_value=False):
        api._menu_snap("top-left")
    assert api._corner == "bottom-right"


def test_corner_moves_never_write_to_disk(tmp_path, monkeypatch):
    """In-session only: snapping and cycling persist nothing under ~/.squid-pet."""
    monkeypatch.setattr("squid_pet.window.Path.home", classmethod(lambda cls: tmp_path))
    api = _make_api(wanderer=MagicMock())
    api._corner = "bottom-right"
    with patch("squid_pet.window.move_to_corner", return_value=True):
        api._menu_snap("top-left")
        api.next_corner()
    assert list(tmp_path.rglob("*")) == []
