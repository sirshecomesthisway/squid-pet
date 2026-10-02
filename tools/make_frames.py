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

    def bbox(self, cells: set[tuple[int, int]]) -> Box:
        xs = [x for x, _ in cells]
        ys = [y for _, y in cells]
        return (min(xs), min(ys), max(xs) + 1, max(ys) + 1)

    def look(self, dx: int, dy: int) -> None:
        self.cv.move(self.pupil, dx, dy, self.white, clip=self.sclera | self.pupil)

    def close(self) -> None:
        """Replace the open eye with a flat closed-eye bar (as blink.png draws)."""
        edge = self.cv.dilate(self.sclera | self.pupil, 2)
        cells = {c for c in edge if in_box(*c, self.roi) and role(self.cv.get(*c)) != "P"}
        self.cv.fill(cells, self.pink)
        x0, y0, x1, y1 = self.bbox(self.sclera)
        cy = (y0 + y1) // 2
        self.cv.rect((x0, cy - 11, x1, cy + 11), BAR_INK)


def eyes(cv: Canvas, rois: tuple[Box, Box]) -> tuple[Eye, Eye]:
    return Eye(cv, rois[0]), Eye(cv, rois[1])


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
}

# Regions a state's frames may touch; anything else must match the base.
ZONES: dict[str, list[Box]] = {
    "thinking": [THINK_BUBBLE, *[(r[0] - 8, r[1] - 8, r[2] + 8, r[3] + 8) for r in THINK_EYES]],
    "concerned": [CONC_BANG, *[(r[0] - 8, r[1] - 8, r[2] + 8, r[3] + 8) for r in CONC_EYES]],
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


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--state", action="append", choices=sorted(FRAMES), help="only these states")
    ap.add_argument("--out", type=Path, default=SPRITES, help="output directory")
    ap.add_argument("--check", action="store_true", help="compare against committed frames")
    ap.add_argument("--contact-sheet", type=Path, help="also write per-state review sheets here")
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
