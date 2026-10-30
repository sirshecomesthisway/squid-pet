"""Click hit-testing follows her silhouette, not a box.

Pink-2026-10-29: the hit test was one worst-case bounding box covering
nearly the whole 180px sprite square, so clicks on the empty corners
around her landed on her instead of the app underneath. It now uses a
per-state mask: every frame that state can show, swept through its CSS
motion, floating decorations dropped, plus a small halo.
"""
from __future__ import annotations

import pytest
from PIL import Image, ImageChops, ImageFilter

from squid_pet.passthrough import (
    ALPHA_THRESHOLD,
    CLICK_HALO_PX,
    GAP_CLOSE_PX,
    MASK_PAD,
    SPRITE_HEIGHT,
    SPRITE_WIDTH,
    SPRITES_DIR,
    _body_alpha,
    _window_to_sprite,
    load_alpha_masks,
)

# The retired box, in sprite coords (6..175 x 0..187, clipped to the sprite).
OLD_BOX_AREA = (175 - 6 + 1) * SPRITE_HEIGHT

CORE_STATES = ["idle", "thinking", "working", "grooving", "celebrating",
               "concerned", "sleeping", "attention_needed"]


@pytest.fixture(scope="module")
def masks():
    return load_alpha_masks()


def _hit(mask, sx, sy) -> bool:
    return mask.getpixel((sx + MASK_PAD, sy + MASK_PAD)) > ALPHA_THRESHOLD


@pytest.mark.parametrize("state", CORE_STATES)
def test_every_state_has_a_mask(masks, state):
    assert state in masks


@pytest.mark.parametrize("state", CORE_STATES)
def test_mask_is_much_smaller_than_the_old_box(masks, state):
    mask = masks[state]
    area = sum(mask.histogram()[ALPHA_THRESHOLD + 1:])
    assert area < 0.6 * OLD_BOX_AREA, (state, area)


@pytest.mark.parametrize("state", CORE_STATES)
def test_her_middle_is_clickable(masks, state):
    assert _hit(masks[state], SPRITE_WIDTH // 2, SPRITE_HEIGHT // 2 + 10)


@pytest.mark.parametrize("state", ["idle", "thinking", "concerned", "sleeping"])
def test_empty_bottom_corners_click_through(masks, state):
    # Inside the old box, nowhere near her: these used to grab her.
    mask = masks[state]
    assert not _hit(mask, 10, SPRITE_HEIGHT - 5)
    assert not _hit(mask, SPRITE_WIDTH - 10, SPRITE_HEIGHT - 5)


def test_idle_mask_covers_every_pixel_of_her(masks):
    body = _body_alpha(SPRITES_DIR / "idle.png")
    idle = masks["idle"]
    missed = [(x, y) for x in range(SPRITE_WIDTH) for y in range(SPRITE_HEIGHT)
              if body.getpixel((x, y)) and not _hit(idle, x, y)]
    assert missed == []


def test_margin_stays_thin(masks):
    # concerned has no swept motion, so its mask is her outline grown by
    # at most the gap fill plus the halo -- nothing reaches further out.
    body = _body_alpha(SPRITES_DIR / "concerned.png")
    for path in sorted(SPRITES_DIR.glob("concerned_*.png")):
        body = ImageChops.lighter(body, _body_alpha(path))
    reach = Image.new("L", masks["concerned"].size, 0)
    reach.paste(body, (MASK_PAD, MASK_PAD))
    for _ in range(GAP_CLOSE_PX + CLICK_HALO_PX):
        reach = reach.filter(ImageFilter.MaxFilter(3))
    beyond = ImageChops.subtract(masks["concerned"], reach)
    assert beyond.getbbox() is None


def test_window_to_sprite_upright_is_a_plain_offset():
    # Sprite sits at (10, 120) in the 200x300 window.
    assert _window_to_sprite(100, 210, "") == (90, 90)
    assert _window_to_sprite(100, 210, "bottom") == (90, 90)
    assert _window_to_sprite(10, 120, "") == (0, 0)


def test_window_to_sprite_undoes_side_rotation():
    # Head (sprite (90, 40)) points toward screen centre: right on the
    # left edge, left on the right edge.
    assert _window_to_sprite(150, 210, "left") == (90, 40)
    assert _window_to_sprite(50, 210, "right") == (90, 40)


def test_window_to_sprite_undoes_top_flip_and_shift():
    # Upside down and shifted up 80px: centre at (100, 130), head below.
    assert _window_to_sprite(100, 130, "top") == (90, 90)
    assert _window_to_sprite(100, 180, "top") == (90, 40)
    assert _window_to_sprite(100, 80, "top") == (90, 140)
