from app.devices import SettingsStore, parse_alsa_list, parse_hw_params, write_asoundrc

ARECORD_L = """**** List of CAPTURE Hardware Devices ****
card 0: PCH [HDA Intel PCH], device 0: ALC892 Analog [ALC892 Analog]
  Subdevices: 1/1
  Subdevice #0: subdevice #0
card 2: SC1 [FIFINE SC1], device 0: USB Audio [USB Audio]
  Subdevices: 1/1
  Subdevice #0: subdevice #0
"""

HW_PARAMS = """HW Params of device "hw:2,0":
--------------------
ACCESS:  MMAP_INTERLEAVED RW_INTERLEAVED
FORMAT:  S16_LE
SUBFORMAT:  STD
SAMPLE_BITS: 16
FRAME_BITS: 32
CHANNELS: 2
RATE: [44100 48000]
--------------------
"""


def test_parse_alsa_list():
    devs = parse_alsa_list(ARECORD_L)
    assert [d["id"] for d in devs] == ["hw:CARD=PCH,DEV=0", "hw:CARD=SC1,DEV=0"]
    assert devs[1]["label"] == "FIFINE SC1 – USB Audio (hw:2,0)"
    assert devs[1]["card"] == "SC1"


def test_parse_hw_params():
    p = parse_hw_params(HW_PARAMS)
    assert p["formats"] == ["S16_LE"]
    assert p["channels"] == {"min": 2, "max": 2}
    assert p["rate"] == {"min": 44100, "max": 48000}


def test_asoundrc(tmp_path):
    write_asoundrc(tmp_path, "hw:CARD=SC1,DEV=0", 48000, 2, "S16_LE")
    text = (tmp_path / ".asoundrc").read_text()
    assert "type dsnoop" in text and 'pcm "hw:CARD=SC1,DEV=0"' in text


def test_settings_store(tmp_path):
    s = SettingsStore(tmp_path)
    assert s.load() == {}
    s.save(device="hw:1,0")
    s.save(monitor=True)
    assert s.load() == {"device": "hw:1,0", "monitor": True}


def test_resolve_device(monkeypatch):
    from app import devices
    monkeypatch.setattr(devices, "list_capture_devices", lambda: parse_alsa_list(ARECORD_L))
    # USB card wins over the onboard card
    assert devices.resolve_device(None, "auto") == "hw:CARD=SC1,DEV=0"
    # saved choice is used if the card is there ...
    assert devices.resolve_device("hw:CARD=PCH,DEV=0", "auto") == "hw:CARD=PCH,DEV=0"
    # ... but ignored if it is not plugged in
    assert devices.resolve_device("hw:CARD=Gone,DEV=0", "auto") == "hw:CARD=SC1,DEV=0"
    assert devices.resolve_device("hw:CARD=Gone,DEV=0", "hw:5,0") == "hw:5,0"
    monkeypatch.setattr(devices, "list_capture_devices", lambda: [])
    assert devices.resolve_device(None, "auto") == "test"
