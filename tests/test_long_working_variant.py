"""Tests for the long-running "pancake flip" presentation variant of the
`working` state (feat/pancake-flip-long-working).

The variant is NOT a new backend state: it's a flag (get_state()['long_working'])
that flips True once accumulated agent-work time has lasted at least
config.long_working_threshold_sec(). The frontend uses it to swap the static
working sprite for the 20-frame pancake-flip animation; thinking and tool
activity contribute to the accumulated total, while approval waits pause it.

These tests pin the duration-tracking rules:
  - below threshold  -> long_working False (normal working art)
  - past threshold    -> long_working True  (pancake variant)
  - approval_needed   -> accumulated total is held, not advanced
  - a quiet new session -> timer resets; the next session counts from zero

Reuses the __new__ + manual-attribute + MagicMock-observer fixture pattern from
test_working_reannounce.py: PetApi's real __init__ builds a window/menu/watcher
thread none of this needs.
"""
from __future__ import annotations

import threading
from unittest.mock import MagicMock, patch

from squid_pet.watcher import PetState
from squid_pet.window import PetApi

# A deliberately short threshold so the tests don't juggle 3600s literals.
THRESHOLD = 100.0


def _make_api():
    api = PetApi.__new__(PetApi)
    api._lock = threading.Lock()
    api._latest = PetState()
    api._last_state_for_bubble = "idle"
    api._forced_state = None
    api._passthrough = None
    api._sm = None
    api._observer = MagicMock()
    api._observer.on_state_change.return_value = None
    api._observer.on_still_working.return_value = None
    api._observer.on_new_command.return_value = None
    api._pending_bubble = None
    api._pending_bubble_priority = 0
    api._last_working_bubble_at = 0.0
    api._last_working_bubble_text = ""
    # Attributes get_state() reads (fixture bypasses __init__).
    api._frontend_mood = ""
    api._last_wake_at = 0.0
    api._wander_sub_state = ""
    api._wander_edge = ""
    api._hint_text = ""
    api._hint_seq = 0
    api._pinned = False
    api._wrapper_deg_override = None
    api._wake_trigger_seq = 0
    api._user_wake_until = 0.0
    api._sprint_fast_transition = False
    return api


def _short_threshold():
    """Patch the config accessor PetApi reads so tests need not wait an hour.
    Mirrors how a manual tester would shorten it via ~/.squid-pet/config.json."""
    return patch("squid_pet.config.long_working_threshold_sec", return_value=THRESHOLD)


def test_below_threshold_is_not_long_working():
    api = _make_api()
    with _short_threshold():
        api.update(PetState(state="working", timestamp=1010.0, work_seconds=10.0))
        assert api._is_long_working() is False
        assert api.get_state()["long_working"] is False


def test_past_threshold_is_long_working():
    api = _make_api()
    with _short_threshold():
        api.update(PetState(state="working", timestamp=1000.0,
                            work_seconds=THRESHOLD + 1))
        assert api._is_long_working() is True
        assert api.get_state()["long_working"] is True


def test_exactly_at_threshold_is_long_working():
    """Boundary: elapsed == threshold counts as long-working (>=)."""
    api = _make_api()
    with _short_threshold():
        api.update(PetState(state="working", timestamp=1000.0,
                            work_seconds=THRESHOLD))
        assert api._is_long_working() is True


def test_leaving_working_resets_the_timer():
    api = _make_api()
    with _short_threshold():
        api.update(PetState(state="working", timestamp=1000.0,
                            work_seconds=THRESHOLD + 1))
        assert api._is_long_working() is True

        # Leave working -> the stretch is over, the clock must clear.
        api.update(PetState(state="idle", timestamp=5000.0))
        assert api._is_long_working() is False
        assert api.get_state()["long_working"] is False


def test_next_working_stretch_counts_from_zero():
    api = _make_api()
    with _short_threshold():
        api.update(PetState(state="working", timestamp=1000.0,
                            work_seconds=THRESHOLD + 1))
        api.update(PetState(state="thinking", timestamp=5000.0,
                            work_seconds=THRESHOLD + 1))

        # A brand-new working stretch starts counting from its own entry,
        # NOT from the earlier long stretch.
        api.update(PetState(state="working", timestamp=6000.0, work_seconds=10.0))
        assert api._is_long_working() is False


def test_reannounce_refresh_does_not_reset_the_timer():
    """A 'still working' reannounce (same state across ticks, no transition)
    not affect the work clock. The reannounce path fires on the same-state
    branch of update(); this walks several ticks while the backend supplies
    the same accumulated total."""
    api = _make_api()
    # Make on_still_working return a fresh line each call so the reannounce
    # path actually does work (publishes bubbles) across these ticks.
    lines = iter(f"still working {i}" for i in range(100))
    api._observer.on_still_working.side_effect = lambda _cmd: next(lines)
    with _short_threshold():
        api.update(PetState(state="working", timestamp=1000.0, work_seconds=0.0))

        # Many same-state ticks well past the reannounce cadence -- each one
        # is a reannounce refresh, none is a transition. Ends one tick past
        # the threshold (1000 + THRESHOLD + 1) so the variant should trip.
        for t in range(1015, 1000 + int(THRESHOLD) + 2, 15):
            api.update(PetState(state="working", timestamp=float(t),
                                work_seconds=float(t - 1000)))
        api.update(PetState(state="working", timestamp=1000.0 + THRESHOLD + 1,
                            work_seconds=THRESHOLD + 1))

        # The accumulated wall-time still trips the variant.
        assert api._is_long_working() is True


def test_accumulated_work_seconds_cross_state_boundaries():
    """Thinking time contributes to the pancake threshold."""
    api = _make_api()
    with _short_threshold():
        api.update(PetState(state="working", timestamp=1000.0,
                            work_seconds=90.0))
        api.update(PetState(state="thinking", timestamp=1001.0,
                            work_seconds=THRESHOLD + 1))
        api.update(PetState(state="working", timestamp=1002.0,
                            work_seconds=THRESHOLD + 1))
        assert api._is_long_working() is True


def test_approval_pause_keeps_accumulated_work_without_advancing_it():
    """An approval display pauses the total and resumes at the same total."""
    api = _make_api()
    with _short_threshold():
        api.update(PetState(state="working", timestamp=1000.0,
                            work_seconds=90.0))
        api.update(PetState(state="approval_needed", timestamp=2000.0,
                            work_seconds=90.0))
        assert api._is_long_working() is False
        api.update(PetState(state="thinking", timestamp=2001.0,
                            work_seconds=90.0))
        assert api._is_long_working() is False
        api.update(PetState(state="working", timestamp=2002.0,
                            work_seconds=THRESHOLD + 1))
        assert api._is_long_working() is True


def test_non_working_state_is_never_long_working():
    api = _make_api()
    with _short_threshold():
        api.update(PetState(state="idle", timestamp=1000.0))
        api.update(PetState(state="idle", timestamp=1000.0 + THRESHOLD + 1))
        assert api._is_long_working() is False
        assert api.get_state()["long_working"] is False


def test_default_threshold_is_one_hour():
    """The shipped default is 1h so only genuinely long stretches trip it.

    Deliberately does NOT assert the *live* value: that reads the developer's
    own ~/.squid-pet/config.json, so this used to fail on any machine where
    the setting had been tuned (e.g. shortened to eyeball the animation).
    Patch get() to ignore user config and return the caller's fallback, which
    is what pins the accessor's own default."""
    from unittest.mock import patch as _patch

    from squid_pet import config
    assert config.DEFAULTS["long_working_threshold_sec"] == 3600
    with _patch("squid_pet.config.get", side_effect=lambda key, default=None: default):
        assert config.long_working_threshold_sec() == 3600


# ─────────────────────────────────────────────────────────────────────────
# Asset + frontend-wiring contract for the pancake-flip frames.
#
# The 20 PNGs and the JS that cycles them sit either side of a language
# boundary with nothing coupling them at runtime -- a missing frame or a
# changed count would only show up as a stalled/blank pet. These pin the
# two sides together (same spirit as test_frontend_state_contract.py).
# ─────────────────────────────────────────────────────────────────────────
import re
from pathlib import Path

from PIL import Image

from squid_pet import watcher

FRONTEND_DIR = Path(watcher.__file__).parent / "frontend"
INDEX_HTML = FRONTEND_DIR / "index.html"
SPRITES_DIR = FRONTEND_DIR / "sprites"
PANCAKE_FRAME_COUNT = 24


def _frame(i: int) -> Path:
    return SPRITES_DIR / f"pancake_flip_{i}.png"


def test_all_pancake_frames_exist():
    missing = [i for i in range(1, PANCAKE_FRAME_COUNT + 1) if not _frame(i).is_file()]
    assert not missing, f"missing pancake_flip frames: {missing}"


def test_frontend_frame_count_matches_disk():
    """The JS builds its frame list with Array.from({length: N}). If someone
    adds/removes a PNG without updating N (or vice versa) the cycle would
    skip a frame or 404 -- neither is visible from Python otherwise."""
    text = INDEX_HTML.read_text()
    m = re.search(r"const PANCAKE_FRAMES\s*=\s*Array\.from\(\s*\{\s*length:\s*(\d+)\s*\}", text, re.S)
    assert m, "could not find the PANCAKE_FRAMES Array.from({length: N}) in index.html"
    assert "pancake_flip_" in text
    assert int(m.group(1)) == PANCAKE_FRAME_COUNT


def test_frontend_reads_the_long_working_flag():
    """get_state()['long_working'] is the only channel that turns the variant
    on; if the frontend stopped reading it the animation would never fire."""
    text = INDEX_HTML.read_text()
    assert "long_working" in text
    assert "syncPancakeFlip" in text


def test_frames_match_the_sprite_sheet_geometry():
    """Every other sprite is a 1254x1254 RGBA with fully binary alpha. A frame
    that drifts from that would render at a different scale or show an
    anti-aliased halo against the desktop."""
    for i in range(1, PANCAKE_FRAME_COUNT + 1):
        im = Image.open(_frame(i)).convert("RGBA")
        assert im.size == (1254, 1254), f"frame {i} is {im.size}"
        partial = sum(1 for a in im.getchannel("A").tobytes() if 10 < a < 245)
        assert partial == 0, f"frame {i} has {partial} anti-aliased alpha px"


def test_squid_keeps_her_size_and_only_bobs_by_whole_cells():
    """THE invariant behind this variant: Squid is never rescaled, and she is
    only ever translated by whole 25px art-cells (the deliberate bob). So her
    silhouette in every frame is idle.png's, shifted by 0 or +/-1 cell -- which
    is what guarantees no size change or jump when the sprite swaps.

    A bug that rescaled her, or nudged her by a fraction of a cell, would break
    this even though the frame would still 'look about right'."""
    CELL = 25
    box = (700, 300, 971, 900)          # her body only, clear of pan/arm/hat
    idle = Image.open(SPRITES_DIR / "idle.png").convert("RGBA")
    ref = idle.crop(box).getchannel("A").point(lambda a: 255 if a > 128 else 0).tobytes()
    for i in range(1, PANCAKE_FRAME_COUNT + 1):
        im = Image.open(_frame(i)).convert("RGBA")
        matched = None
        for dy in (0, CELL, -CELL):
            shifted = (box[0], box[1] + dy, box[2], box[3] + dy)
            got = im.crop(shifted).getchannel("A").point(lambda a: 255 if a > 128 else 0)
            if got.tobytes() == ref:
                matched = dy
                break
        assert matched is not None, (
            f"frame {i}: Squid's silhouette is not idle.png shifted by a whole "
            f"cell -- she has been rescaled or sub-cell nudged"
        )


def test_the_loop_closes():
    """The last frame must hand back to frame 1 seamlessly, otherwise the cycle
    visibly pops once every 3s."""
    first = Image.open(_frame(1)).convert("RGBA")
    last = Image.open(_frame(PANCAKE_FRAME_COUNT)).convert("RGBA")
    assert first.tobytes() == last.tobytes()


def test_chef_hat_is_visible_against_a_white_desktop():
    """The toque is near-white, so without a border it disappears on a white
    background (she is composited straight onto the desktop). Guard the
    outline: the topmost pixel of the sprite is the hat's crown, and it must
    be meaningfully darker than white or the hat has lost its border."""
    for i in (1, PANCAKE_FRAME_COUNT // 2, PANCAKE_FRAME_COUNT):
        im = Image.open(_frame(i)).convert("RGBA")
        alpha = im.getchannel("A")
        top_y = min(y for y in range(im.height)
                    if any(alpha.getpixel((x, y)) > 128 for x in range(im.width)))
        row = [im.getpixel((x, top_y)) for x in range(im.width)
               if alpha.getpixel((x, top_y)) > 128]
        # Luminance of the hat's top edge -- the outline, not the white crown.
        lum = min(0.299 * r + 0.587 * g + 0.114 * b for r, g, b, _ in row)
        assert lum < 200, (
            f"frame {i}: hat top edge luminance {lum:.0f} is too pale to read "
            f"against a white desktop -- the outline is missing"
        )


def test_sprite_url_yields_to_the_running_pancake_cycle():
    """Regression (2026-09-19): several layers restore the "base sprite" by
    calling setSpriteSrcDirect(spriteUrl(currentState)) -- applySubState does
    it on EVERY poll tick when there is no sub-state. While the pancake cycle
    is running that resolved to working.png and stomped the animation, so she
    visibly alternated between flipping a pancake and standing still.

    spriteUrl() must therefore hand back the frame currently on screen for
    `working`, and it must do so BEFORE the generic `sprites/${state}.png`
    return or the guard is dead code."""
    text = INDEX_HTML.read_text()
    m = re.search(r"function spriteUrl\(state\)\s*\{(.*?)\n    \}", text, re.S)
    assert m, "could not find spriteUrl() in index.html"
    body = m.group(1)
    guard = body.find("_pancakeSrc")
    generic = body.find("VALID.has(state)")
    assert guard != -1, "spriteUrl() no longer yields to the pancake cycle"
    assert generic != -1
    assert guard < generic, (
        "the pancake guard must come BEFORE spriteUrl's generic return, "
        "otherwise working.png wins and the animation is stomped again"
    )


def test_pancake_cycle_releases_sprite_ownership_when_it_stops():
    """The flip side of the guard above: stopPancakeFlip must clear
    _pancakeSrc *before* restoring, or spriteUrl would keep handing back a
    stale pancake frame after she has left working."""
    text = INDEX_HTML.read_text()
    m = re.search(r"function stopPancakeFlip\(\)\s*\{(.*?)\n    \}", text, re.S)
    assert m, "could not find stopPancakeFlip() in index.html"
    body = m.group(1)
    release = body.find("_pancakeSrc = null")
    restore = body.find("setSpriteSrcDirect(spriteUrl(currentState))")
    assert release != -1, "stopPancakeFlip no longer releases _pancakeSrc"
    assert restore != -1
    assert release < restore, "ownership must be released before the restore"
