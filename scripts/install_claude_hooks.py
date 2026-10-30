#!/usr/bin/env python3
"""Run Claude hook setup directly from a checkout, without an installed package."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from squid_pet.claude_hooks import main

if __name__ == "__main__":
    sys.exit(main())
