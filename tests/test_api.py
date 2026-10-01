import base64

from fastapi.testclient import TestClient

from app.main import create_app

NAME = "2026-10-01_20-40-05_riff.flac"


def put_file(settings, name=NAME, data=bytes(range(256)) * 40):
    (settings.recordings_dir / name).write_bytes(data)
    return data


def test_status(client):
    s = client.get("/api/status").json()
    assert s["state"] == "idle"
    assert s["device"] == "test"
    assert s["config"]["bit_depth"] == 16
    assert s["disk_free_mb"] > 0


def test_index_page(client):
    r = client.get("/")
    assert r.status_code == 200 and "Recorder" in r.text


def test_list_and_range(client, settings):
    data = put_file(settings)
    items = client.get("/api/recordings").json()
    assert [i["name"] for i in items] == [NAME]
    r = client.get(f"/api/recordings/{NAME}", headers={"Range": "bytes=100-199"})
    assert r.status_code == 206
    assert r.content == data[100:200]
    assert r.headers["content-type"] == "audio/flac"


def test_download_header(client, settings):
    put_file(settings)
    r = client.get(f"/api/recordings/{NAME}/download")
    assert r.status_code == 200
    assert "attachment" in r.headers["content-disposition"]


def test_rename_and_delete(client, settings):
    put_file(settings)
    r = client.patch(f"/api/recordings/{NAME}", json={"name": "Better Take"})
    assert r.json()["name"] == "2026-10-01_20-40-05_better-take.flac"
    assert client.delete("/api/recordings/2026-10-01_20-40-05_better-take.flac").status_code == 200
    assert client.get("/api/recordings").json() == []


def test_rename_conflict(client, settings):
    put_file(settings)
    put_file(settings, "2026-10-01_20-40-05_other.flac")
    r = client.patch(f"/api/recordings/{NAME}", json={"name": "other"})
    assert r.status_code == 409


def test_path_traversal(client, settings):
    (settings.data_dir / "settings.json").write_text("{}")
    for bad in ["..%2Fdata%2Fsettings.json", "%2E%2E%2Fsecret.flac", ".hidden.flac", "x.txt"]:
        r = client.get(f"/api/recordings/{bad}")
        assert r.status_code in (400, 404), bad
        assert client.delete(f"/api/recordings/{bad}").status_code in (400, 404, 405)


def test_not_found(client):
    assert client.get("/api/recordings/nope.flac").status_code == 404
    assert client.delete("/api/recordings/nope.flac").status_code == 404


def test_stop_when_idle(client):
    assert client.post("/api/record/stop").status_code == 409


def test_devices(client):
    d = client.get("/api/devices").json()
    assert d["selected"] == "test"
    assert any(x["id"] == "test" for x in d["devices"])
    assert client.put("/api/devices/selected", json={"device": "hw:99,0"}).status_code == 400
    assert client.put("/api/devices/selected", json={"device": "test"}).status_code == 200


def test_monitor_unavailable_with_test_device(client):
    assert client.get("/api/monitor").json()["available"] is False
    assert client.put("/api/monitor", json={"enabled": True}).status_code == 409


def test_basic_auth(settings):
    settings.auth_user, settings.auth_pass = "me", "secret"
    with TestClient(create_app(settings)) as c:
        assert c.get("/api/status").status_code == 401
        token = base64.b64encode(b"me:secret").decode()
        assert c.get("/api/status", headers={"Authorization": f"Basic {token}"}).status_code == 200
        bad = base64.b64encode(b"me:wrong").decode()
        assert c.get("/api/status", headers={"Authorization": f"Basic {bad}"}).status_code == 401
