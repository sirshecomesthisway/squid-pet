"""Contract tests for the duration of Squid's speech bubbles."""
from pathlib import Path

from squid_pet import watcher

INDEX_HTML = Path(watcher.__file__).parent / "frontend" / "index.html"


def test_speech_bubble_hold_is_twice_the_original_duration():
    html = INDEX_HTML.read_text()
    assert "const BUBBLE_HOLD_MS = 5000;" in html
    assert "}, BUBBLE_HOLD_MS);" in html
