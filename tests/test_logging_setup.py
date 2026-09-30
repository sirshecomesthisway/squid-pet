"""Tests for logging_setup.setup_logging()."""
import logging

import pytest

from squid_pet import logging_setup


def _clear_handlers():
    logger = logging.getLogger(logging_setup._PACKAGE_LOGGER)
    for h in list(logger.handlers):
        logger.removeHandler(h)


@pytest.fixture
def tmp_logdir(tmp_path, monkeypatch):
    d = tmp_path / ".squid-pet"
    monkeypatch.setattr(logging_setup, "LOG_DIR", d)
    monkeypatch.setattr(logging_setup, "LOG_FILE", d / "squid.log")
    monkeypatch.setattr(logging_setup, "_configured", False)
    # setup_logging is only ever called by the app entry points, never by
    # other tests, so clearing the package logger's handlers here is safe and
    # keeps each test deterministic.
    _clear_handlers()
    yield d
    _clear_handlers()


def _handler_types(logger):
    return sorted(type(h).__name__ for h in logger.handlers)


def test_returns_package_logger(tmp_logdir):
    logger = logging_setup.setup_logging()
    assert logger.name == "squid_pet"


def test_attaches_stream_and_file_handlers(tmp_logdir):
    # force=True gives a deterministic base regardless of what pytest's own
    # logging plumbing has attached to the shared logger.
    logger = logging_setup.setup_logging(force=True)
    types = _handler_types(logger)
    assert "StreamHandler" in types
    assert "RotatingFileHandler" in types


def test_creates_log_file_and_writes(tmp_logdir):
    logger = logging_setup.setup_logging(force=True)
    logger.warning("hello squid")
    for h in logger.handlers:
        h.flush()
    assert (tmp_logdir / "squid.log").exists()
    assert "hello squid" in (tmp_logdir / "squid.log").read_text()


def test_idempotent_no_duplicate_handlers(tmp_logdir):
    logger = logging_setup.setup_logging(force=True)
    n = len(logger.handlers)
    assert n == 2  # stream + rotating file
    logging_setup.setup_logging()  # no-op: already configured
    logging_setup.setup_logging()
    assert len(logger.handlers) == n


def test_force_resets_handlers(tmp_logdir):
    logging_setup.setup_logging(force=True)
    logger = logging_setup.setup_logging(force=True)
    # force clears then re-adds -- count stays at 2, not doubled.
    assert len(logger.handlers) == 2


def test_respects_env_level(tmp_logdir, monkeypatch):
    monkeypatch.setenv("SQUID_LOG_LEVEL", "DEBUG")
    logger = logging_setup.setup_logging(force=True)
    assert logger.level == logging.DEBUG


def test_unwritable_logdir_degrades_to_stdout(tmp_path, monkeypatch):
    # Point LOG_DIR at a path whose parent is a file -> mkdir fails.
    blocker = tmp_path / "afile"
    blocker.write_text("x")
    monkeypatch.setattr(logging_setup, "LOG_DIR", blocker / "nested")
    monkeypatch.setattr(logging_setup, "LOG_FILE", blocker / "nested" / "squid.log")
    monkeypatch.setattr(logging_setup, "_configured", False)
    _clear_handlers()
    try:
        result = logging_setup.setup_logging()
        # Must not raise; a StreamHandler is still attached, no file handler.
        assert any(type(h).__name__ == "StreamHandler" for h in result.handlers)
        assert not any(type(h).__name__ == "RotatingFileHandler"
                       for h in result.handlers)
    finally:
        _clear_handlers()


def test_startup_markers_reach_the_stdout_log_doctor_reads(tmp_logdir, capsys):
    from squid_pet.doctor import REQUIRED_STARTUP_MARKERS, check_startup_log_complete

    logger = logging_setup.setup_logging(force=True)
    for marker in REQUIRED_STARTUP_MARKERS:
        logger.info(marker)
    captured = capsys.readouterr()
    launchd_stdout = tmp_logdir / "launchd-stdout.log"
    launchd_stdout.write_text(captured.out)
    result = check_startup_log_complete(launchd_stdout)
    assert result.passed, result.diagnostic
    assert captured.err == ""
