"""
logging_setup.py -- one place that configures logging for the squid-pet
daemon.

Before this existed, modules called ``print("[squid-pet] ...", flush=True)``.
That has two problems for a long-running desktop daemon:

  1. Errors caught by the broad ``except Exception`` blocks (deliberate --
     a failed psutil/Cocoa call must never crash the pet) were printed
     without a traceback and with no severity, so a real regression looked
     the same as routine chatter and was effectively invisible.
  2. Output went only to whatever launchd happened to capture, un-levelled
     and unbounded.

``setup_logging()`` attaches two handlers to the ``squid_pet`` package
logger:

  * a RotatingFileHandler at ~/.squid-pet/squid.log (bounded, levelled,
    with tracebacks) -- the durable record to grep when something misbehaves;
  * a StreamHandler on stdout -- so launchd's own capture keeps working and
    ``squid doctor``'s startup-marker parsing (which reads that stdout log)
    is unaffected.

It is idempotent: calling it more than once (main() and the watcher-only
entry point both call it) will not stack duplicate handlers.

Level defaults to INFO; export ``SQUID_LOG_LEVEL=DEBUG`` (etc.) to change it.
"""
from __future__ import annotations

import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOG_DIR = Path.home() / ".squid-pet"
LOG_FILE = LOG_DIR / "squid.log"

_PACKAGE_LOGGER = "squid_pet"
_MAX_BYTES = 2 * 1024 * 1024   # 2 MB per file
_BACKUP_COUNT = 3              # squid.log + .1 .2 .3
_configured = False


def _level_from_env() -> int:
    raw = os.environ.get("SQUID_LOG_LEVEL", "INFO").upper()
    return getattr(logging, raw, logging.INFO)


def setup_logging(*, force: bool = False) -> logging.Logger:
    """Configure and return the ``squid_pet`` package logger.

    Idempotent: subsequent calls are no-ops unless ``force=True`` (used by
    tests that want a clean handler set).
    """
    global _configured
    logger = logging.getLogger(_PACKAGE_LOGGER)
    if _configured and not force:
        return logger
    if force:
        for h in list(logger.handlers):
            logger.removeHandler(h)

    level = _level_from_env()
    logger.setLevel(level)
    # Don't double-emit through the root logger's default handler.
    logger.propagate = False

    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # stdout -- keeps launchd capture + doctor's marker parsing working.
    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(fmt)
    logger.addHandler(stream)

    # Rotating file -- best-effort; a daemon must still run if ~/.squid-pet
    # is unwritable, so a failure here degrades to stdout-only.
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            LOG_FILE, maxBytes=_MAX_BYTES, backupCount=_BACKUP_COUNT,
            encoding="utf-8",
        )
        file_handler.setFormatter(fmt)
        logger.addHandler(file_handler)
    except OSError as e:
        logger.warning("could not open log file %s: %s", LOG_FILE, e)

    _configured = True
    return logger
