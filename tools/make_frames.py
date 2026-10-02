"""Derive multi-frame animation cycles for Squid's static states.

Every frame is the state's base sprite with a few local pixel edits (pupil
moves, blinks, thought-bubble growth, "!" pops, ...). Nothing is scaled,
translated or recoloured, so the body, palette and pixel density stay exactly
as drawn; body motion remains the job of the CSS keyframes in index.html.

Frame 1 of every cycle is the untouched base sprite and is not written out
(the frontend maps frame 1 to `<state>.png`). Frames 2.. are written as
`<state>_<n>.png` next to the base sprites. The output is deterministic, and
`--check` regenerates in memory and compares against the committed files.

Usage:
    uv run --frozen python tools/make_frames.py                  # write all
    uv run --frozen python tools/make_frames.py --state thinking
    uv run --frozen python tools/make_frames.py --check
    uv run --frozen python tools/make_frames.py --out DIR --contact-sheet DIR
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from collections.abc import Callable
from pathlib import Path

from PIL import Image, ImageChops

SPRITES = Path(__file__).resolve().parents[1] / "src/squid_pet/frontend/sprites"

Box = tuple[int, int, int, int]  # x0, y0, x1, y1 (half-open)
Pixel = tuple[int, int, int, int]
Op = Callable[["Canvas"], None]

TRANSPARENT: Pixel = (0, 0, 0, 0)
BAR_INK: Pixel = (30, 30, 35, 255)  # the closed-eye bar colour used by blink.png


# --------------------------------------------------------------------------- #
# Pixel roles and the canvas
# --------------------------------------------------------------------------- #
def role(p: Pixel) -> str:
    """Coarse colour role: '.' clear, 'K' ink, 'W' cream/white, 'P' pink, 'R' red, '?' blend."""
    r, g, b, a = p
    if a < 16:
        return "."
    if r < 70 and g < 70 and b < 80:
        return "K"
    if r > 200 and g < 60 and b < 90:
        return "R"
    if r > 235 and g > 225 and b > 215:
        return "W"
    if r > 190 and 70 < g < 150 and 90 < b < 175 and r - g > 80:
        return "P"
    return "?"


def in_box(x: int, y: int, box: Box) -> bool:
    return box[0] <= x < box[2] and box[1] <= y < box[3]


def clamp_box(box: Box, size: tuple[int, int]) -> Box:
    return (max(0, box[0]), max(0, box[1]), min(size[0], box[2]), min(size[1], box[3]))


def bbox_of(cells: set[tuple[int, int]]) -> Box:
    xs = [x for x, _ in cells]
    ys = [y for _, y in cells]
    return (min(xs), min(ys), max(xs) + 1, max(ys) + 1)


class Canvas:
    """A mutable RGBA copy of a base sprite plus the edit helpers."""

    def __init__(self, base: Image.Image):
        self.img = base.convert("RGBA").copy()
        self.px = self.img.load()
        self.size = self.img.size
        assert self.px is not None

    def get(self, x: int, y: int) -> Pixel:
        return self.px[x, y]  # type: ignore[index,no-any-return]

    def put(self, x: int, y: int, p: Pixel) -> None:
        self.px[x, y] = p  # type: ignore[index]

    def coords(self, box: Box, pred: Callable[[Pixel], bool]) -> set[tuple[int, int]]:
        x0, y0, x1, y1 = clamp_box(box, self.size)
        return {
            (x, y)
            for y in range(y0, y1)
            for x in range(x0, x1)
            if pred(self.get(x, y))
        }

    def common(self, box: Box, want: str) -> Pixel:
        """Most common exact colour among `want`-role pixels in `box`."""
        c = Counter(
            self.get(x, y)
            for x, y in self.coords(box, lambda p: role(p) == want)
        )
        if not c:
            raise AssertionError(f"no '{want}' pixels in {box}")
        return c.most_common(1)[0][0]

    def dilate(self, cells: set[tuple[int, int]], r: int) -> set[tuple[int, int]]:
        out = set()
        for x, y in cells:
            for dy in range(-r, r + 1):
                for dx in range(-r, r + 1):
                    out.add((x + dx, y + dy))
        return out

    def move(
        self,
        cells: set[tuple[int, int]],
        dx: int,
        dy: int,
        fill: Pixel,
        clip: set[tuple[int, int]] | None = None,
    ) -> None:
        """Lift `cells`, paint `fill` where they were, redraw them offset by (dx, dy).

        With `clip`, pixels landing outside it are dropped (keeps a pupil
        inside its sclera).
        """
        lifted = {c: self.get(*c) for c in cells}
        for x, y in cells:
            self.put(x, y, fill)
        for (x, y), p in lifted.items():
            nx, ny = x + dx, y + dy
            if clip is not None and (nx, ny) not in clip:
                continue
            if 0 <= nx < self.size[0] and 0 <= ny < self.size[1]:
                self.put(nx, ny, p)

    def fill(self, cells: set[tuple[int, int]], p: Pixel) -> None:
        for x, y in cells:
            self.put(x, y, p)

    def rect(self, box: Box, p: Pixel) -> None:
        x0, y0, x1, y1 = clamp_box(box, self.size)
        for y in range(y0, y1):
            for x in range(x0, x1):
                self.put(x, y, p)


# --------------------------------------------------------------------------- #
# Eyes: pupil moves and blinks
# --------------------------------------------------------------------------- #
class Eye:
    """One eye located inside a hand-given region of interest."""

    def __init__(self, cv: Canvas, roi: Box):
        self.cv = cv
        self.roi = roi
        self.pupil_core = cv.coords(roi, lambda p: role(p) == "K")
        assert self.pupil_core, f"no pupil found in {roi}"
        self.sclera = cv.coords(roi, lambda p: role(p) in "WK")
        assert len(self.sclera) > 2000, f"sclera too small in {roi}"
        # The pupil's antialiased fringe moves with it; never grab body pink.
        ring = cv.dilate(self.pupil_core, 6)
        self.pupil = {
            c for c in ring
            if in_box(*c, roi) and role(cv.get(*c)) not in (".", "P")
        }
        self.white = cv.common(roi, "W")
        self.pink = self._neighbour_pink()

    def _neighbour_pink(self) -> Pixel:
        return self.cv.common(self.roi, "P")

    def look(self, dx: int, dy: int) -> None:
        self.cv.move(self.pupil, dx, dy, self.white, clip=self.sclera | self.pupil)

    def close(self) -> None:
        """Replace the open eye with a flat closed-eye bar (as blink.png draws)."""
        edge = self.cv.dilate(self.sclera | self.pupil, 2)
        cells = {c for c in edge if in_box(*c, self.roi) and role(self.cv.get(*c)) != "P"}
        self.cv.fill(cells, self.pink)
        x0, y0, x1, y1 = bbox_of(self.sclera)
        cy = (y0 + y1) // 2
        self.cv.rect((x0, cy - 11, x1, cy + 11), BAR_INK)


def eyes(cv: Canvas, rois: tuple[Box, Box]) -> tuple[Eye, Eye]:
    return Eye(cv, rois[0]), Eye(cv, rois[1])


# --------------------------------------------------------------------------- #
# Region ops for accessories that float over a transparent background
# (music notes, Z's, sparkles) and for flat features painted on the body.
# --------------------------------------------------------------------------- #
def _solid(box: Box) -> Callable[[Canvas], set[tuple[int, int]]]:
    return lambda cv: cv.coords(box, lambda p: p[3] > 0)


def shift_box(box: Box, dx: int, dy: int) -> Op:
    """Move everything visible inside `box` by (dx, dy) over a clear background."""
    def op(cv: Canvas) -> None:
        cv.move(_solid(box)(cv), dx, dy, TRANSPARENT)
    return op


def erase_box(box: Box) -> Op:
    def op(cv: Canvas) -> None:
        cv.fill(_solid(box)(cv), TRANSPARENT)
    return op


def scale_box(box: Box, factor: float) -> Op:
    """Shrink the sprite piece in `box` about its own centre (nearest-neighbour)."""
    def op(cv: Canvas) -> None:
        cells = _solid(box)(cv)
        x0, y0, x1, y1 = bbox_of(cells)
        piece = cv.img.crop((x0, y0, x1, y1))
        cv.fill(cells, TRANSPARENT)
        w, h = max(1, round((x1 - x0) * factor)), max(1, round((y1 - y0) * factor))
        small = piece.resize((w, h), Image.Resampling.NEAREST)
        cv.img.paste(small, ((x0 + x1 - w) // 2, (y0 + y1 - h) // 2))
    return op


def cover_rows(box: Box, rows: int, p_box: Box) -> Op:
    """Paint the top `rows` pixel rows of `box` with body pink (an eyelid)."""
    def op(cv: Canvas) -> None:
        cv.rect((box[0], box[1], box[2], box[1] + rows), cv.common(p_box, "P"))
    return op


def close_box(box: Box, p_box: Box, thick: int = 16) -> Op:
    """Paint `box` over with body pink and draw a flat closed-eye bar."""
    def op(cv: Canvas) -> None:
        cv.rect(box, cv.common(p_box, "P"))
        cy = (box[1] + box[3]) // 2
        cv.rect((box[0], cy - thick // 2, box[2], cy + thick // 2), BAR_INK)
    return op


def hide_ink(cells_fn: Callable[[Canvas], set[tuple[int, int]]]) -> Op:
    """Erase ink (plus its antialiased fringe) by copying the nearest clean pixel in each row.

    Row-wise on purpose: the body edge is a staircase whose steps are whole
    rows, so the nearest pixel in the same row is always on the right side
    of any step the ink sits on.
    """
    def op(cv: Canvas) -> None:
        gone = cv.dilate(cells_fn(cv), 3)
        w = cv.size[0]
        fills = {}
        for x, y in gone:
            for d in range(1, 80):
                for nx in (x - d, x + d):
                    if 0 <= nx < w and (nx, y) not in gone and cv.get(nx, y)[3] in (0, 255):
                        fills[(x, y)] = cv.get(nx, y)
                        break
                else:
                    continue
                break
            else:
                raise AssertionError(f"no clean pixel beside {(x, y)}")
        for (x, y), v in fills.items():
            cv.put(x, y, v)
    return op


Point = tuple[float, float]


def rotated(points: list[Point], pivot: Point, degrees: float) -> list[Point]:
    """Rotate `points` clockwise on screen by `degrees` about `pivot`."""
    import math

    t = math.radians(degrees)
    c, s = math.cos(t), math.sin(t)
    return [
        (pivot[0] + (x - pivot[0]) * c - (y - pivot[1]) * s,
         pivot[1] + (x - pivot[0]) * s + (y - pivot[1]) * c)
        for x, y in points
    ]


def limb_cells(
    points: list[Point], radii: list[float], cell: int = 16, origin: tuple[int, int] = (0, 0)
) -> set[tuple[int, int]]:
    """Grid cells covered by a tapering limb drawn along a polyline.

    The limb is the union of discs whose radius is interpolated between the
    `radii` of consecutive `points`. A cell is filled when at least half of it
    lies inside, which gives the stepped silhouette of hand-drawn pixel art.
    """
    import math

    def inside(x: float, y: float) -> bool:
        for (ax, ay), (bx, by), ra, rb in zip(points, points[1:], radii, radii[1:]):
            dx, dy = bx - ax, by - ay
            ln = dx * dx + dy * dy
            t = 0.0 if ln == 0 else max(0.0, min(1.0, ((x - ax) * dx + (y - ay) * dy) / ln))
            if math.hypot(x - (ax + t * dx), y - (ay + t * dy)) <= ra + (rb - ra) * t:
                return True
        return False

    pad = max(radii) + cell
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    ox, oy = origin
    cells = set()
    for cy in range(int((min(ys) - pad - oy) // cell), int((max(ys) + pad - oy) // cell) + 1):
        for cx in range(int((min(xs) - pad - ox) // cell), int((max(xs) + pad - ox) // cell) + 1):
            hits = sum(
                inside(ox + cx * cell + (i + 0.5) * cell / 4, oy + cy * cell + (j + 0.5) * cell / 4)
                for i in range(4) for j in range(4)
            )
            if hits >= 8:
                cells.add((cx, cy))
    return cells


def redraw_limb(
    erase: Callable[[int, int, Pixel], bool],
    erase_box: Box,
    points: list[Point],
    radii: list[float],
    colour_box: Box,
    cell: int = 16,
) -> Op:
    """Erase the drawn limb selected by `erase`, then draw it afresh along `points`."""
    def op(cv: Canvas) -> None:
        colour = cv.common(colour_box, "P")
        x0, y0, x1, y1 = clamp_box(erase_box, cv.size)
        cv.fill(
            {(x, y) for y in range(y0, y1) for x in range(x0, x1)
             if cv.get(x, y)[3] > 0 and erase(x, y, cv.get(x, y))},
            TRANSPARENT,
        )
        for cx, cy in limb_cells(points, radii, cell):
            cv.rect((cx * cell, cy * cell, (cx + 1) * cell, (cy + 1) * cell), colour)
    return op


def typing_fingers(left_len: int, right_len: int) -> Op:
    """Two rounded fingertips poking out of the tentacles onto the keys.

    Each is a short capsule that grows out of the pink mass above it, so it is
    always attached; `*_len` is how far it reaches below the tentacle's edge.
    """
    def op(cv: Canvas) -> None:
        pink = cv.common(WORK_PINK, "P")
        for cx, edge, length in ((586, 856, left_len), (644, 836, right_len)):
            pts = [(float(cx), float(edge - 14)), (float(cx), float(edge + length - 16))]
            for gx, gy in limb_cells(pts, [19.0, 19.0], 8):
                cv.rect((gx * 8, gy * 8, gx * 8 + 8, gy * 8 + 8), pink)
    return op


def screen_cursor_off(box: Box) -> Op:
    """Blank the blinking cursor on the laptop screen with the screen colour."""
    def op(cv: Canvas) -> None:
        cur = cv.coords(box, lambda p: role(p) in ("C", "?") and p[1] > p[0] + 30)
        ring = cv.dilate(cur, 3) - cur
        c = Counter(cv.get(x, y) for x, y in ring if cv.get(x, y)[1] <= cv.get(x, y)[0] + 30)
        cv.fill(cur, c.most_common(1)[0][0])
    return op


# --------------------------------------------------------------------------- #
# Frame specs
# --------------------------------------------------------------------------- #
THINK_EYES: tuple[Box, Box] = ((426, 618, 546, 744), (672, 570, 792, 702))
THINK_BUBBLE: Box = (820, 70, 1160, 480)
THINK_DOT: Box = (944, 206, 978, 240)
THINK_INK: Pixel = (20, 15, 18, 255)
THINK_DOT_SIZE = 24
THINK_DOT_Y = 222  # vertical centre of the bubble's original dot


def _thinking_dots(centres: list[int]) -> Op:
    def op(cv: Canvas) -> None:
        white = cv.common(THINK_BUBBLE, "W")
        old = cv.coords(THINK_DOT, lambda p: role(p) in ("K", "?"))
        cv.fill(old, white)
        h = THINK_DOT_SIZE // 2
        for cx in centres:
            cv.rect((cx - h, THINK_DOT_Y - h, cx + h, THINK_DOT_Y + h), THINK_INK)
    return op


def _bubble_part(x: int, y: int) -> str:
    """Split the bubble sprite into its three growth stages."""
    if x < 925 and y >= 382:
        return "small"
    if y >= 325 and x <= 962:
        return "mid"
    return "big"


def _thinking_tail(keep: set[str]) -> Op:
    def op(cv: Canvas) -> None:
        cells = cv.coords(THINK_BUBBLE, lambda p: p[3] > 0)
        gone = {c for c in cells if _bubble_part(*c) not in keep}
        cv.fill(gone, TRANSPARENT)
    return op


def _look_both(rois: tuple[Box, Box], offs: tuple[tuple[int, int], tuple[int, int]]) -> Op:
    def op(cv: Canvas) -> None:
        for eye, (dx, dy) in zip(eyes(cv, rois), offs):
            eye.look(dx, dy)
    return op


def _blink_both(rois: tuple[Box, Box]) -> Op:
    def op(cv: Canvas) -> None:
        for eye in eyes(cv, rois):
            eye.close()
    return op


GLANCE = _look_both(THINK_EYES, ((-36, 24), (-36, 24)))
THINK_DOTS3 = _thinking_dots([936, 984, 1032])

CONC_EYES: tuple[Box, Box] = ((438, 636, 576, 774), (672, 636, 804, 774))
CONC_BANG: Box = (600, 110, 654, 306)
BANG_POP = 18


def _bang(dy: int, visible: bool = True) -> Op:
    def op(cv: Canvas) -> None:
        cells = cv.coords(CONC_BANG, lambda p: p[3] > 0)
        if not visible:
            cv.fill(cells, TRANSPARENT)
        else:
            cv.move(cells, 0, dy, TRANSPARENT)
    return op


# sleeping: Z's drift up one at a time.
Z_SMALL: Box = (776, 402, 846, 468)
Z_MID: Box = (836, 314, 916, 402)
Z_BIG: Box = (916, 208, 1004, 318)
Z_UP = 24

# drowsy: lids droop and the loading ring turns.
DROWSY_EYE_L: Box = (332, 572, 452, 656)
DROWSY_EYE_R: Box = (616, 572, 740, 656)
DROWSY_PINK: Box = (460, 480, 600, 560)
DROWSY_RING: Box = (700, 180, 985, 430)


def _ring_dots(cv: Canvas) -> list[set[tuple[int, int]]]:
    """The ring's dots, ordered clockwise from the top."""
    import math
    ink = cv.coords(DROWSY_RING, lambda p: role(p) == "K")
    seen: set[tuple[int, int]] = set()
    dots: list[set[tuple[int, int]]] = []
    for start in sorted(ink):
        if start in seen:
            continue
        stack, comp = [start], set()
        seen.add(start)
        while stack:
            x, y = stack.pop()
            comp.add((x, y))
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    n = (x + dx, y + dy)
                    if n in ink and n not in seen:
                        seen.add(n)
                        stack.append(n)
        if len(comp) > 150:
            dots.append(comp)
    assert 8 <= len(dots) <= 20, f"unexpected ring dot count {len(dots)}"
    allx = [x for d in dots for x, _ in d]
    ally = [y for d in dots for _, y in d]
    cx, cy = (min(allx) + max(allx)) / 2, (min(ally) + max(ally)) / 2

    def ang(d: set[tuple[int, int]]) -> float:
        mx = sum(x for x, _ in d) / len(d)
        my = sum(y for _, y in d) / len(d)
        return math.atan2(mx - cx, cy - my) % (2 * math.pi)

    return sorted(dots, key=ang)


RING_CENTRE: Point = (843.0, 307.0)
RING_RADIUS = 100.0
RING_DOTS = 12
RING_DOT = 34
RING_FILL: Pixel = (46, 48, 54, 255)
RING_EDGE: Pixel = (30, 30, 34, 255)


def _ring_redraw(phase_deg: float) -> Op:
    """Redraw the whole loading ring as an even circle of dots, turned by `phase_deg`.

    The old, uneven ring is erased first, so every frame shows one full circle
    (including the dots that sit over her head).
    """
    import math

    def op(cv: Canvas) -> None:
        old = {c for d in _ring_dots(cv) for c in d}
        hide_ink(lambda _cv: old)(cv)
        for k in range(RING_DOTS):
            a = math.radians(phase_deg + k * 360.0 / RING_DOTS)
            cx = round(RING_CENTRE[0] + RING_RADIUS * math.sin(a))
            cy = round(RING_CENTRE[1] - RING_RADIUS * math.cos(a))
            h = RING_DOT // 2
            cv.rect((cx - h, cy - h, cx + h, cy + h), RING_EDGE)
            cv.rect((cx - h + 2, cy - h + 2, cx + h - 2, cy + h - 2), RING_FILL)
    return op


# working: the cursor blinks and she glances at the screen.
WORK_EYES: tuple[Box, Box] = ((410, 575, 530, 712), (625, 590, 750, 712))
WORK_CURSOR: Box = (330, 836, 380, 864)
WORK_PINK: Box = (640, 772, 720, 800)

# grooving: the three notes bob out of phase.
NOTE_A: Box = (244, 284, 352, 412)
NOTE_B: Box = (156, 652, 248, 788)
NOTE_C: Box = (972, 380, 1068, 520)

# grooving: both raised arms are redrawn (not shifted) as tapering limbs on the
# art grid, swinging about their shoulders. The mid pose is fitted to the base art.
ARM_L_BOX: Box = (270, 596, 432, 742)
ARM_R_BOX: Box = (856, 682, 1000, 772)
ARM_L_SHOULDER: Point = (405.0, 745.0)
ARM_R_SHOULDER: Point = (830.0, 800.0)
ARM_L_PTS: list[Point] = [(322, 628), (340, 670), (347, 692), (368, 718), (400, 760)]
ARM_L_RAD = [29.0, 30.0, 31.0, 36.0, 50.0]
ARM_R_PTS: list[Point] = [(941, 712), (940, 760), (900, 790), (830, 800)]
ARM_R_RAD = [29.0, 30.0, 34.0, 44.0]
ARM_BODY: Box = (520, 440, 700, 560)


def arm_left(deg: float) -> Op:
    return redraw_limb(
        lambda x, y, _p: x <= 418 and not (x >= 356 and y < 648), ARM_L_BOX,
        rotated(ARM_L_PTS, ARM_L_SHOULDER, deg), ARM_L_RAD, ARM_BODY,
    )


def arm_right(deg: float) -> Op:
    return redraw_limb(
        lambda x, y, _p: x >= 858, ARM_R_BOX,
        rotated(ARM_R_PTS, ARM_R_SHOULDER, deg), ARM_R_RAD, ARM_BODY,
    )


# celebrating: the four sparkles twinkle in diagonal pairs.
STAR_A: Box = (260, 224, 372, 348)
STAR_B: Box = (852, 200, 956, 312)
STAR_C: Box = (988, 772, 1084, 888)
STAR_D: Box = (140, 684, 232, 780)
TWINKLE = 0.55


FRAMES: dict[str, list[list[Op]]] = {
    "thinking": [
        [],                                              # 1 base: one dot
        [_thinking_dots([960, 1008])],                   # 2 two dots
        [THINK_DOTS3],                                   # 3 three dots
        [_thinking_tail({"small"})],                     # 4 bubble forming: smallest
        [_thinking_tail({"small", "mid"})],              # 5 bubble forming: middle
        [THINK_DOTS3, GLANCE],                           # 6 eyes drift in, thinking
        [THINK_DOTS3, _blink_both(THINK_EYES)],          # 7 blink
    ],
    "concerned": [
        [],                                              # 1 base
        [_look_both(CONC_EYES, ((0, 0), (-60, 0)))],     # 2 both look left
        [_look_both(CONC_EYES, ((60, 0), (0, 0)))],      # 3 both look right
        [_bang(-BANG_POP)],                              # 4 "!" pops up
        [_bang(0, visible=False)],                       # 5 "!" flashes off
        [_blink_both(CONC_EYES)],                        # 6 blink
    ],
    "sleeping": [
        [],                                                        # 1 base: three Z's
        [erase_box(Z_SMALL), erase_box(Z_MID), erase_box(Z_BIG)],  # 2 none
        [erase_box(Z_MID), erase_box(Z_BIG)],                      # 3 smallest only
        [erase_box(Z_BIG)],                                        # 4 two
        [shift_box(Z_SMALL, 0, -Z_UP), shift_box(Z_MID, 0, -Z_UP),
         shift_box(Z_BIG, 0, -Z_UP)],                              # 5 all drift up
    ],
    "drowsy": [
        [],                                                        # 1 base
        [cover_rows(DROWSY_EYE_L, 26, DROWSY_PINK), cover_rows(DROWSY_EYE_R, 26, DROWSY_PINK)],
        [cover_rows(DROWSY_EYE_L, 44, DROWSY_PINK), cover_rows(DROWSY_EYE_R, 44, DROWSY_PINK)],
        [close_box(DROWSY_EYE_L, DROWSY_PINK), close_box(DROWSY_EYE_R, DROWSY_PINK)],
        [_ring_redraw(0)],                                         # 5 full ring, turning
        [_ring_redraw(10)],                                        # 6
        [_ring_redraw(20)],                                        # 7
    ],
    "working": [
        [],                                                        # 1 base
        [typing_fingers(42, 8)],                                   # 2 left tip taps, right lifts
        [typing_fingers(8, 42)],                                   # 3 right tip taps, left lifts
        [screen_cursor_off(WORK_CURSOR)],                          # 4 cursor off
        [_look_both(WORK_EYES, ((-8, 0), (-16, 0)))],              # 5 glance at screen
    ],
    "grooving": [
        [],                                                        # 1 base (entry only)
        [arm_left(6), arm_right(22),                              # 2 left arm up, right down
         shift_box(NOTE_A, 0, -14), shift_box(NOTE_B, 0, 14), shift_box(NOTE_C, 0, -14)],
        [arm_left(0), arm_right(0)],                               # 3 both mid
        [arm_left(-22), arm_right(-12),                            # 4 left arm down, right up
         shift_box(NOTE_A, 0, 14), shift_box(NOTE_B, 0, -14), shift_box(NOTE_C, 0, 14)],
    ],
    "celebrating": [
        [],                                                        # 1 base
        [scale_box(STAR_A, TWINKLE), scale_box(STAR_C, TWINKLE)],
        [scale_box(b, TWINKLE) for b in (STAR_A, STAR_B, STAR_C, STAR_D)],
        [scale_box(STAR_B, TWINKLE), scale_box(STAR_D, TWINKLE)],
    ],
}

# Regions a state's frames may touch; anything else must match the base.
ZONES: dict[str, list[Box]] = {
    "thinking": [THINK_BUBBLE, *[(r[0] - 8, r[1] - 8, r[2] + 8, r[3] + 8) for r in THINK_EYES]],
    "concerned": [CONC_BANG, *[(r[0] - 8, r[1] - 8, r[2] + 8, r[3] + 8) for r in CONC_EYES]],
    "sleeping": [(776, 180, 1010, 470)],
    "drowsy": [DROWSY_EYE_L, DROWSY_EYE_R, DROWSY_RING],
    "working": [WORK_CURSOR, (540, 740, 700, 900),
                *[(r[0] - 8, r[1] - 8, r[2] + 8, r[3] + 8) for r in WORK_EYES]],
    "grooving": [(a[0] - 80, a[1] - 80, a[2] + 80, a[3] + 80) for a in (ARM_L_BOX, ARM_R_BOX)] + [
                 *[(n[0] - 20, n[1] - 20, n[2] + 20, n[3] + 20) for n in (NOTE_A, NOTE_B, NOTE_C)]],
    "celebrating": [STAR_A, STAR_B, STAR_C, STAR_D],
}


# --------------------------------------------------------------------------- #
# Build, verify, report
# --------------------------------------------------------------------------- #
def build_state(state: str, base: Image.Image) -> list[Image.Image]:
    out = []
    for ops in FRAMES[state]:
        cv = Canvas(base)
        for op in ops:
            op(cv)
        out.append(cv.img)
    return out


def verify_state(state: str, base: Image.Image, frames: list[Image.Image]) -> list[str]:
    errors = []
    base = base.convert("RGBA")
    base_alpha = {v for _, v in (base.getchannel("A").getcolors(256) or [])}
    for i, fr in enumerate(frames, 1):
        if fr.size != base.size:
            errors.append(f"{state}_{i}: size {fr.size} != {base.size}")
            continue
        if i == 1 and fr.tobytes() != base.tobytes():
            errors.append(f"{state}_1 must equal the base sprite")
        a, b = base.copy(), fr.copy()
        for z in ZONES[state]:
            a.paste(TRANSPARENT, clamp_box(z, a.size))
            b.paste(TRANSPARENT, clamp_box(z, b.size))
        if ImageChops.difference(a, b).getbbox() is not None:
            errors.append(f"{state}_{i}: pixels changed outside the edit zones")
        alphas = {v for _, v in (fr.getchannel("A").getcolors(256) or [])}
        if alphas - base_alpha:
            errors.append(f"{state}_{i}: new alpha values {sorted(alphas - base_alpha)[:5]}")
    return errors


def save(img: Image.Image, path: Path) -> None:
    img.save(path, "PNG", optimize=True)


def contact_sheet(state: str, frames: list[Image.Image], path: Path) -> None:
    cell, pad = 380, 6
    sheet = Image.new("RGBA", (len(frames) * (cell + pad) + pad, cell + 2 * pad), (90, 170, 110, 255))
    for i, fr in enumerate(frames):
        small = fr.resize((cell, cell), Image.Resampling.NEAREST)
        sheet.alpha_composite(small, (pad + i * (cell + pad), pad))
    sheet.save(path)


def js_timelines() -> dict[str, list[tuple[int, int]]]:
    """STATE_ANIMS timelines parsed from the frontend, as {state: [(frame, ms)]}."""
    import json
    import re

    html = (SPRITES.parent / "index.html").read_text()
    block = re.search(r"const STATE_ANIMS = \{(.*?)\n    \};", html, re.DOTALL)
    assert block, "STATE_ANIMS not found in index.html"
    return {
        state: [tuple(step) for step in json.loads("[" + body + "]")]  # type: ignore[misc]
        for state, body in re.findall(
            r"(\w+): \{ frames: \d+, timeline: \[(.*?)\] \}", block.group(1), re.DOTALL
        )
    }


def write_gif(state: str, frames: list[Image.Image], path: Path, size: int = 360) -> None:
    """Play `state`'s frontend timeline into an animated GIF (review aid)."""
    bg = (255, 244, 236, 255)
    shown = []
    for fr in frames:
        canvas = Image.new("RGBA", fr.size, bg)
        canvas.alpha_composite(fr)
        shown.append(canvas.resize((size, size), Image.Resampling.NEAREST).convert("RGB"))
    steps = js_timelines()[state]
    shown_seq = [shown[n - 1] for n, _ in steps]
    shown_seq[0].save(
        path, save_all=True, append_images=shown_seq[1:], duration=[ms for _, ms in steps],
        loop=0, optimize=False,
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--state", action="append", choices=sorted(FRAMES), help="only these states")
    ap.add_argument("--out", type=Path, default=SPRITES, help="output directory")
    ap.add_argument("--check", action="store_true", help="compare against committed frames")
    ap.add_argument("--contact-sheet", type=Path, help="also write per-state review sheets here")
    ap.add_argument("--gif", type=Path, help="also write per-state animated previews here")
    args = ap.parse_args(argv)

    failed = False
    for state in args.state or sorted(FRAMES):
        base = Image.open(SPRITES / f"{state}.png").convert("RGBA")
        frames = build_state(state, base)
        errors = verify_state(state, base, frames)
        for e in errors:
            print(f"ERROR {e}", file=sys.stderr)
        failed |= bool(errors)
        if args.contact_sheet:
            args.contact_sheet.mkdir(parents=True, exist_ok=True)
            contact_sheet(state, frames, args.contact_sheet / f"{state}.png")
        if args.gif:
            args.gif.mkdir(parents=True, exist_ok=True)
            write_gif(state, frames, args.gif / f"{state}.gif")
        for i, fr in enumerate(frames, 1):
            if i == 1:
                continue  # the base sprite itself
            name = f"{state}_{i}.png"
            if args.check:
                committed = SPRITES / name
                if not committed.is_file():
                    print(f"DRIFT {name}: missing", file=sys.stderr)
                    failed = True
                elif Image.open(committed).convert("RGBA").tobytes() != fr.tobytes():
                    print(f"DRIFT {name}: differs from regenerated frame", file=sys.stderr)
                    failed = True
            else:
                args.out.mkdir(parents=True, exist_ok=True)
                save(fr, args.out / name)
        print(f"{state}: {len(frames)} frames")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
