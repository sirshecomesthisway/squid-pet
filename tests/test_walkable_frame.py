from types import SimpleNamespace as NS

from squid_pet.window import _walkable_frame


def _rect(x, y, w, h):
    return NS(origin=NS(x=x, y=y), size=NS(width=w, height=h))


def test_bottom_extends_to_screen_edge_under_visible_dock():
    screen = NS(frame=lambda: _rect(0, 0, 1470, 956),
                visibleFrame=lambda: _rect(0, 74, 1470, 849))
    # Top (74 + 849 = 923) is unchanged; bottom drops to the screen edge.
    assert _walkable_frame(screen) == (0, 0, 1470, 923)
