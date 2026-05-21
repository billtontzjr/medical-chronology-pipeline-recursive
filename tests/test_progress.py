"""Tests for the progress reporter interface."""

import logging

from src.progress import (
    CallbackProgressReporter,
    ConsoleProgressReporter,
    ProgressReporter,
)


def test_console_reporter_does_not_crash(caplog):
    """ConsoleProgressReporter should handle all method calls without error."""
    r = ConsoleProgressReporter()
    with caplog.at_level(logging.INFO, logger="precision_chronology"):
        r.phase_start(1, 8, "Downloading PDFs")
        r.phase_progress(3, 10, "file_003.pdf")
        r.phase_complete(1, "3 files downloaded")
        r.phase_log("some detail")
        r.phase_skipped(2, 8, "OCR already done")
        r.pipeline_complete("all done")
        r.pipeline_failed("something broke")
    assert "Phase 1/8" in caplog.text
    assert "3/10" in caplog.text
    assert "Pipeline complete" in caplog.text


def test_callback_reporter_forwards_all_calls():
    """CallbackProgressReporter should call the legacy callback for every method."""
    messages = []
    r = CallbackProgressReporter(messages.append)
    r.phase_start(1, 8, "Downloading")
    r.phase_progress(5, 20, "chunk_05")
    r.phase_complete(1, "done")
    r.phase_log("detail")
    r.phase_skipped(2, 8, "OCR")
    r.pipeline_complete("finished")
    r.pipeline_failed("error")
    assert len(messages) == 7
    assert "Phase 1/8" in messages[0]
    assert "5/20" in messages[1]


def test_progress_reporter_is_abstract():
    """ProgressReporter cannot be instantiated directly."""
    import pytest

    with pytest.raises(TypeError):
        ProgressReporter()
