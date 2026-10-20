from types import SimpleNamespace as NS

from squid_pet.window import _walkable_frame


def _rect(x, y, w, h):
    return NS(origin=NS(x=x, y=y), size=NS(width=w, height=h))


def test_bottom_extends_to_screen_edge_under_visible_dock():
    screen = NS(frame=lambda: _rect(0, 0, 1470, 956),
                visibleFrame=lambda: _rect(0, 74, 1470, 849))
    # Top (74 + 849 = 923) is unchanged; bottom drops to the screen edge.
    assert _walkable_frame(screen) == (0, 0, 1470, 923)


def test_autohide_dock_leaves_frame_unchanged():
    screen = NS(frame=lambda: _rect(0, 0, 1470, 956),
                visibleFrame=lambda: _rect(0, 0, 1470, 923))
    assert _walkable_frame(screen) == (0, 0, 1470, 923)


def test_secondary_screen_with_nonzero_origin():
    screen = NS(frame=lambda: _rect(1470, -200, 1920, 1080),
                visibleFrame=lambda: _rect(1470, -126, 1920, 1006))
    # Top = -126 + 1006 = 880; bottom drops to the frame's -200.
    assert _walkable_frame(screen) == (1470, -200, 1920, 1080)


def test_inverted_geometry_falls_back_to_visible_frame():
    screen = NS(frame=lambda: _rect(0, 500, 1470, 956),
                visibleFrame=lambda: _rect(0, 74, 1470, 100))
    assert _walkable_frame(screen) == (0, 74, 1470, 100)
