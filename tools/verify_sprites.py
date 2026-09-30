"""Validate shipped sprite assets without starting Squid or modifying artwork.

Only the top-level runtime inventory is checked; _originals_with_bg contains
source artwork, not assets consumed by the frontend. Update EXPECTED_SIZES when
intentionally adding/removing runtime sprites or changing their canvas sizes.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from PIL import Image

DEFAULT_DIRECTORY = Path(__file__).resolve().parents[1] / "src/squid_pet/frontend/sprites"
BASE_NAMES = (
    "attention_needed", "blink", "celebrating", "concerned", "drowsy",
    "grooving", "idle", "look-left", "look-right", "sleeping", "stretch",
    "thinking", "working",
)
EXPECTED_SIZES = {
    **{f"{name}.png": (1254, 1254) for name in BASE_NAMES},
    **{f"attention_needed_{i}.png": (1254, 1254) for i in range(1, 5)},
    **{f"pancake_flip_{i}.png": (1254, 1254) for i in range(1, 25)},
    "heart.png": (670, 612),
    "idle_menubar.png": (710, 725),
    "sleeping_menubar.png": (753, 756),
}


def verify_sprites(directory: Path) -> list[str]:
    """Return all asset errors, with filenames, in deterministic order."""
    if not directory.is_dir():
        return [f"{directory}: sprite directory is missing or is not a directory"]
    errors = []
    try:
        # Non-image documentation and source-art subdirectories are not assets.
        names = {p.name for p in directory.iterdir() if p.suffix.lower() == ".png"}
    except OSError as exc:
        return [f"{directory}: cannot read directory: {exc}"]
    for name in sorted(EXPECTED_SIZES.keys() - names):
        errors.append(f"{name}: missing runtime sprite")
    for name in sorted(names - EXPECTED_SIZES.keys()):
        errors.append(f"{name}: unexpected runtime sprite name")
    for name in sorted(names & EXPECTED_SIZES.keys()):
        path = directory / name
        try:
            # verify checks PNG chunk integrity; reopen/load also decodes pixels.
            with Image.open(path) as sprite:
                if sprite.format != "PNG":
                    errors.append(f"{name}: expected PNG content, got {sprite.format}")
                    continue
                sprite.verify()
            with Image.open(path) as sprite:
                sprite.load()
                if sprite.size != EXPECTED_SIZES[name]:
                    errors.append(
                        f"{name}: dimensions {sprite.size}, expected {EXPECTED_SIZES[name]}"
                    )
                if "A" not in sprite.getbands():
                    errors.append(f"{name}: missing alpha channel")
                    continue
                minimum, maximum = sprite.getchannel("A").getextrema()
                if minimum != 0:
                    errors.append(f"{name}: no fully transparent background pixels")
                if maximum == 0:
                    errors.append(f"{name}: no visible pixels")
        except (OSError, ValueError, SyntaxError) as exc:
            errors.append(f"{name}: cannot decode PNG: {exc}")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", nargs="?", type=Path, default=DEFAULT_DIRECTORY)
    args = parser.parse_args()
    errors = verify_sprites(args.directory)
    if errors:
        for error in errors:
            print(f"ERROR: {error}")
        print(f"Sprite verification failed: {len(errors)} error(s)")
        return 1
    print(f"Verified {len(EXPECTED_SIZES)} runtime sprites: PNG, dimensions, alpha, names")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
