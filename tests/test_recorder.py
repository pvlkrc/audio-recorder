"""Real recordings with the ffmpeg test tone (AUDIO_DEVICE=test)."""

import json
import subprocess
import time

from app.recorder import recover_parts

from .conftest import needs_ffmpeg


def probe(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=codec_name,sample_rate,channels,sample_fmt",
         "-show_entries", "format=duration", "-of", "json", str(path)],
        capture_output=True, text=True, check=True,
    ).stdout
    return json.loads(out)


@needs_ffmpeg
def test_record_start_stop(client, settings):
    r = client.post("/api/record/start", json={"name": "Bass Riff"})
    assert r.status_code == 200, r.text
    assert r.json()["file"].endswith("_bass-riff.flac")

    # Only one recording at a time.
    assert client.post("/api/record/start", json={}).status_code == 409
    time.sleep(2)
    st = client.get("/api/status").json()
    assert st["state"] == "recording" and st["size"] > 0 and st["elapsed"] >= 1.5
    assert client.post("/api/record/marker", json={"note": "chorus"}).status_code == 200

    stopped = client.post("/api/record/stop").json()
    name = stopped["last_file"]
    assert stopped["state"] == "idle" and name.endswith("_bass-riff.flac")

    info = probe(settings.recordings_dir / name)
    s = info["streams"][0]
    assert s["codec_name"] == "flac" and s["sample_rate"] == "48000" and s["channels"] == 2
    assert 1.5 < float(info["format"]["duration"]) < 5
    assert not list(settings.recordings_dir.glob(".*.part"))
    markers = client.get(f"/api/recordings/{name}/markers").json()
    assert markers[0]["note"] == "chorus"

    items = client.get("/api/recordings").json()
    assert items[0]["name"] == name and items[0]["duration"] > 1.5


@needs_ffmpeg
def test_level_websocket(client):
    with client.websocket_connect("/ws/level") as ws:
        msg = None
        for _ in range(30):
            msg = ws.receive_json()
            if msg["active"]:
                break
        assert msg["active"]
        assert -10 < msg["peak_db"][0] <= 0  # test tone peaks around -6 dB


@needs_ffmpeg
def test_max_duration_auto_stop(client, settings):
    settings.max_duration_min = 2 / 60  # 2 seconds
    client.post("/api/record/start", json={})
    time.sleep(4)
    st = client.get("/api/status").json()
    assert st["state"] == "idle"
    assert "maximum length" in st["last_error"]
    assert st["last_file"]


@needs_ffmpeg
def test_wav_and_mp3(client, settings):
    settings.format = "wav"
    client.post("/api/record/start", json={"name": "w"})
    time.sleep(1.5)
    name = client.post("/api/record/stop").json()["last_file"]
    assert name.endswith(".wav")
    assert probe(settings.recordings_dir / name)["streams"][0]["codec_name"] == "pcm_s16le"
    r = client.post(f"/api/recordings/{name}/mp3")
    assert r.status_code == 200, r.text
    assert (settings.recordings_dir / r.json()["name"]).exists()


@needs_ffmpeg
def test_recover_killed_flac(settings):
    """A FLAC recording whose ffmpeg was killed (-9) is repaired at startup."""
    part = settings.recordings_dir / ".2026-10-01_20-40-05_crash.flac.part"
    p = subprocess.Popen(
        ["ffmpeg", "-v", "error", "-re", "-f", "lavfi", "-i", "sine=f=220:r=48000",
         "-flush_packets", "1", "-c:a", "flac", "-f", "flac", str(part)],
        stdin=subprocess.DEVNULL,
    )
    time.sleep(2)
    p.kill()
    p.wait()
    assert recover_parts(settings.recordings_dir) == ["2026-10-01_20-40-05_crash.flac"]
    info = probe(settings.recordings_dir / "2026-10-01_20-40-05_crash.flac")
    assert float(info["format"]["duration"]) > 1


@needs_ffmpeg
def test_share_mp3_and_video(client, settings):
    client.post("/api/record/start", json={"name": "share me"})
    time.sleep(2)
    name = client.post("/api/record/stop").json()["last_file"]

    r = client.get(f"/api/recordings/{name}/share/mp3")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "audio/mpeg"
    assert name.replace(".flac", ".mp3") in r.headers["content-disposition"]

    r = client.get(f"/api/recordings/{name}/share/mp4")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "video/mp4"
    video = settings.data_dir / "share" / f"{name}.mp4"
    info = probe(video)
    codecs = sorted(s["codec_name"] for s in info["streams"])
    assert codecs == ["aac", "h264"]
    assert float(info["format"]["duration"]) > 1.5

    # Share files are not in the recordings list, and go away with the recording.
    assert [i["name"] for i in client.get("/api/recordings").json()] == [name]
    client.delete(f"/api/recordings/{name}")
    assert not video.exists()
    assert client.get(f"/api/recordings/{name}/share/xyz").status_code == 404
