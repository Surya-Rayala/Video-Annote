from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))  # media_factory

from video_annote.media.ffmpeg import FFmpegNotFoundError, find_toolchain  # noqa: E402


def _have_ffmpeg() -> bool:
    try:
        find_toolchain()
        return True
    except FFmpegNotFoundError:
        return False


def pytest_collection_modifyitems(config, items):
    skip_media = None if _have_ffmpeg() else pytest.mark.skip(reason="ffmpeg/ffprobe not found")
    samples = os.environ.get("VIDEO_ANNOTE_SAMPLES")
    skip_samples = None if samples and os.path.isdir(samples) else pytest.mark.skip(
        reason="set VIDEO_ANNOTE_SAMPLES to a folder of real recordings to run")
    for item in items:
        if skip_media is not None and ("media" in item.keywords or "samples" in item.keywords):
            item.add_marker(skip_media)
        if skip_samples is not None and "samples" in item.keywords:
            item.add_marker(skip_samples)


@pytest.fixture(autouse=True, scope="session")
def _scratch_settings(tmp_path_factory):
    """The GUI remembers window layout and the last data root; tests never read or write the user's."""
    from PyQt5.QtCore import QSettings

    QSettings.setPath(QSettings.IniFormat, QSettings.UserScope, str(tmp_path_factory.mktemp("settings")))


@pytest.fixture()
def chicago(monkeypatch):
    """This computer's time zone, for times written as local time: US Central (UTC-6, UTC-5 in summer)."""
    import time

    monkeypatch.setenv("TZ", "America/Chicago")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()
