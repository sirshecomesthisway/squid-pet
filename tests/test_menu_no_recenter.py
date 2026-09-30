"""The Recenter menu item + its plumbing were removed 2026-09-19.

Once load_corner() was fixed so an explicit starting_corner outranks
position.json, "Recenter" (which called load_corner() then move_to_corner())
simply jumped to the configured corner -- identical to the explicit
Position -> <corner> items already in the same submenu, so it was removed as
redundant/confusing. These tests pin that it stays gone: the built menu tree
carries no Recenter item, and the handler/selector/emoji it needed are all
gone with it. The still-present snap items are asserted as a positive control
that the menu actually built.
"""
from __future__ import annotations

from unittest.mock import MagicMock

from squid_pet import menu as menu_mod
from squid_pet.menu import _build_menu, _MenuTarget
from squid_pet.window import PetApi


def _walk(nsmenu):
    """Yield (title, action_selector_str) for every item in the tree."""
    for item in nsmenu.itemArray():
        action = item.action()
        yield str(item.title()), (str(action) if action is not None else "")
        if item.hasSubmenu():
            yield from _walk(item.submenu())


def test_built_menu_has_no_recenter_item():
    entries = list(_walk(_build_menu(None, MagicMock())))
    titles = [t for t, _ in entries]
    actions = [a for _, a in entries]
    assert not any("Recenter" in t for t in titles), titles
    assert "recenter:" not in actions, actions
    # positive control: the Position submenu actually built
    assert any("Bottom-Right" in t for t in titles), titles


def test_menu_target_has_no_recenter_selector():
    assert not hasattr(_MenuTarget, "recenter_")
    # positive control: the surviving snap selectors are still wired
    assert hasattr(_MenuTarget, "snapBR_")


def test_petapi_has_no_menu_recenter_handler():
    assert not hasattr(PetApi, "_menu_recenter")
    # positive control: the snap handler it lived beside survives
    assert hasattr(PetApi, "_menu_snap")


def test_crosshair_emoji_constant_removed():
    # only Recenter used it; it should not linger as dead module state
    assert not hasattr(menu_mod, "EMO_CROSSHAIR")
