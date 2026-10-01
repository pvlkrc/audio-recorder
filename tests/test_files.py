from datetime import datetime

import pytest

from app.files import (
    InvalidName, build_filename, fix_wav_header, list_recordings, rename_recording, safe_path, slugify,
)


def test_slugify():
    assert slugify("Bass riff #2!") == "bass-riff-2"
    assert slugify("  ../../etc/passwd ") == "etc-passwd"
    assert slugify("Příliš žluťoučký") == "prilis-zlutoucky"
    assert slugify("") == ""


def test_build_filename():
    now = datetime(2026, 10, 1, 20, 40, 5)
    assert build_filename("bass-riff", "flac", now) == "2026-10-01_20-40-05_bass-riff.flac"
    assert build_filename("", "wav", now) == "2026-10-01_20-40-05.wav"


@pytest.mark.parametrize("name", [
    "../secret.flac", "..", "a/b.flac", "a\\b.flac", ".hidden.flac", "x.flac/../../y.flac",
    "/etc/passwd", "settings.json", "a.flac.part", "", "x" * 300 + ".flac", "a;rm.flac", "a\x00.flac",
])
def test_safe_path_rejects(tmp_path, name):
    with pytest.raises(InvalidName):
        safe_path(tmp_path, name)


def test_safe_path_ok(tmp_path):
    assert safe_path(tmp_path, "2026-10-01_20-40-05_riff.flac") == tmp_path / "2026-10-01_20-40-05_riff.flac"


def test_rename_keeps_date_and_ext(tmp_path):
    (tmp_path / "2026-10-01_20-40-05_old.flac").write_bytes(b"x")
    (tmp_path / "2026-10-01_20-40-05_old.flac.markers.json").write_text("[]")
    new = rename_recording(tmp_path, "2026-10-01_20-40-05_old.flac", "New Riff")
    assert new == "2026-10-01_20-40-05_new-riff.flac"
    assert (tmp_path / new).exists()
    assert (tmp_path / f"{new}.markers.json").exists()


def test_list_hides_parts_and_sorts(tmp_path):
    import os
    (tmp_path / "a.flac").write_bytes(b"1")
    (tmp_path / "b.wav").write_bytes(b"22")
    os.utime(tmp_path / "a.flac", (1, 1))
    (tmp_path / ".c.flac.part").write_bytes(b"333")
    (tmp_path / "notes.txt").write_text("x")
    names = [r["name"] for r in list_recordings(tmp_path)]
    assert names == ["b.wav", "a.flac"]


def test_fix_wav_header(tmp_path):
    import struct
    import wave
    p = tmp_path / "t.wav"
    with wave.open(str(p), "wb") as w:
        w.setnchannels(2); w.setsampwidth(2); w.setframerate(48000)
        w.writeframes(b"\x01\x00" * 2 * 4800)
    raw = bytearray(p.read_bytes())
    raw[4:8] = b"\0\0\0\0"; raw[40:44] = b"\0\0\0\0"  # like a killed ffmpeg
    p.write_bytes(raw)
    fix_wav_header(p)
    with wave.open(str(p)) as w:
        assert w.getnframes() == 4800
    assert struct.unpack("<I", p.read_bytes()[4:8])[0] == p.stat().st_size - 8
