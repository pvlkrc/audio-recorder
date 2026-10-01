import shutil

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is not installed")


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setenv("AUDIO_DEVICE", "test")  # ffmpeg sine tone, no sound card
    monkeypatch.setenv("RECORDINGS_DIR", str(tmp_path / "rec"))
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("MIN_FREE_DISK_MB", "1")
    s = Settings.from_env()
    s.recordings_dir.mkdir(parents=True)
    return s


@pytest.fixture
def client(settings):
    with TestClient(create_app(settings)) as c:
        yield c
