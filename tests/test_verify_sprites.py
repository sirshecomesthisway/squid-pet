"""Exercise the sprite verifier with real image files and its public CLI."""

import subprocess
import sys
from pathlib import Path

import pytest
from PIL import Image

SCRIPT = Path(__file__).resolve().parents[1] / "tools" / "verify_sprites.py"
SPRITES = SCRIPT.parent.parent / "src" / "squid_pet" / "frontend" / "sprites"


def run_verifier(directory, cwd=None):
    return subprocess.run(
        [sys.executable, str(SCRIPT), str(directory)],
        cwd=cwd, capture_output=True, text=True, check=False,
    )


@pytest.fixture
def sprites(tmp_path):
    # Use the shipped inventory but tiny compressed, synthetic pixel content.
    for source in SPRITES.glob("*.png"):
        with Image.open(source) as original:
            image = Image.new("RGBA", original.size, (0, 0, 0, 0))
        image.putpixel((0, 0), (100, 50, 200, 255))
        image.save(tmp_path / source.name)
    return tmp_path


def test_shipped_sprites_pass_from_another_directory(tmp_path):
    result = subprocess.run(
        [sys.executable, str(SCRIPT)], cwd=tmp_path,
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_valid_inventory_ignores_archived_artwork(sprites):
    archive = sprites / "_originals_with_bg"
    archive.mkdir()
    (archive / "invalid.png").write_bytes(b"not an image")
    result = run_verifier(sprites)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("kind, diagnostic", [
    ("missing", "missing"),
    ("unexpected", "unexpected"),
    ("uppercase", "unexpected"),
    ("corrupt", "decode"),
    ("truncated", "decode"),
    ("jpeg", "PNG"),
    ("dimensions", "dimensions"),
    ("rgb", "alpha"),
    ("opaque", "transparent"),
    ("invisible", "visible"),
])
def test_rejects_invalid_sprite(sprites, kind, diagnostic):
    path = sprites / "idle.png"
    if kind == "missing":
        path.unlink()
    elif kind == "unexpected":
        path.rename(sprites / "typo.png")
    elif kind == "uppercase":
        path.rename(sprites / "IDLE.PNG")
    elif kind == "corrupt":
        path.write_bytes(b"not an image")
    elif kind == "truncated":
        path.write_bytes(path.read_bytes()[:-20])
    elif kind == "jpeg":
        Image.new("RGB", (1254, 1254)).save(path, format="JPEG")
    elif kind == "dimensions":
        Image.new("RGBA", (32, 32)).save(path)
    elif kind == "rgb":
        Image.new("RGB", (1254, 1254)).save(path)
    elif kind == "opaque":
        Image.new("RGBA", (1254, 1254), (0, 0, 0, 255)).save(path)
    elif kind == "invisible":
        Image.new("RGBA", (1254, 1254), (0, 0, 0, 0)).save(path)
    result = run_verifier(sprites)
    assert result.returncode == 1, result.stdout + result.stderr
    assert diagnostic in result.stdout
    assert "idle.png" in result.stdout


@pytest.mark.parametrize("exists", [True, False])
def test_empty_or_missing_directory_fails(tmp_path, exists):
    directory = tmp_path if exists else tmp_path / "missing"
    result = run_verifier(directory)
    assert result.returncode == 1
    assert "missing" in result.stdout or "directory" in result.stdout
