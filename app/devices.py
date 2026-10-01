"""ALSA devices: list cards, read hardware params, save the choice, write the dsnoop config."""

import json
import re
import subprocess
from pathlib import Path

TEST_DEVICE = "test"  # ffmpeg sine tone, no sound card needed
SHARED_PCM = "rec_in"  # name of the dsnoop device in our asoundrc

# "card 1: SC1 [FIFINE SC1], device 0: USB Audio [USB Audio]"
_CARD_RE = re.compile(r"^card (\d+): (\S+) \[(.*?)\], device (\d+): (.*?) \[(.*?)\]")


def parse_alsa_list(text: str) -> list[dict]:
    """Parse the output of `arecord -l` or `aplay -l`.

    The device id uses the card *name* (hw:CARD=SC1,DEV=0), because card
    numbers can change after a reboot or replug.
    """
    devices = []
    for line in text.splitlines():
        m = _CARD_RE.match(line.strip())
        if not m:
            continue
        num, card_id, card_name, dev, _dev_id, dev_name = m.groups()
        devices.append({
            "id": f"hw:CARD={card_id},DEV={dev}",
            "card": card_id,
            "card_index": int(num),
            "device": int(dev),
            "label": f"{card_name} – {dev_name} (hw:{num},{dev})",
        })
    return devices


def _run(cmd: list[str], timeout: float = 5) -> str:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.stdout + r.stderr
    except (OSError, subprocess.TimeoutExpired):
        return ""


def list_capture_devices() -> list[dict]:
    return parse_alsa_list(_run(["arecord", "-l"]))


def list_playback_devices() -> list[dict]:
    return parse_alsa_list(_run(["aplay", "-l"]))


def parse_hw_params(text: str) -> dict:
    """Parse `arecord --dump-hw-params` lines like 'CHANNELS: [1 2]' or 'FORMAT: S16_LE S24_3LE'."""
    params = {}
    for line in text.splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()
        if key == "FORMAT":
            params["formats"] = value.split()
        elif key in ("CHANNELS", "RATE"):
            nums = [int(n) for n in re.findall(r"\d+", value)]
            if nums:
                params[key.lower()] = {"min": min(nums), "max": max(nums)}
    return params


def read_hw_params(device: str) -> dict | None:
    """Ask the card which channels / rates / formats it supports.

    This opens the device for a moment, so it fails (returns None) while the
    device is in use. That is fine: the result is only informative.
    """
    if device == TEST_DEVICE:
        return None
    out = _run(["arecord", "-D", device, "--dump-hw-params", "-d", "1", "-q", "/dev/null"])
    if "busy" in out.lower():
        return None
    params = parse_hw_params(out)
    return params or None


def resolve_device(selected: str | None, configured: str) -> str:
    """Pick the input device: saved choice > env var > first USB card > first card > test tone.

    A saved choice is used only if that card is plugged in now (it may come
    from another machine, or the card is unplugged).
    """
    devices = list_capture_devices()
    ids = {d["id"] for d in devices} | {TEST_DEVICE}
    if selected and selected in ids:
        return selected
    if configured and configured != "auto":
        return configured
    usb = [d for d in devices if "USB" in d["label"]]
    if usb or devices:
        return (usb or devices)[0]["id"]
    return TEST_DEVICE


def resolve_playback(capture_device: str, configured: str) -> str | None:
    """Headphone output for the SW monitor. "auto" = same card as the input."""
    if capture_device == TEST_DEVICE:
        return None
    if configured and configured != "auto":
        return configured
    m = re.search(r"CARD=([^,]+)", capture_device) or re.match(r"(?:plug)?hw:(\d+)", capture_device)
    if not m:
        return None
    card = m.group(1)
    for d in list_playback_devices():
        if d["card"] == card or str(d["card_index"]) == card:
            return d["id"]
    return None


def write_asoundrc(home: Path, device: str, rate: int, channels: int, alsa_format: str) -> None:
    """Write <home>/.asoundrc with a shared (dsnoop) input called "rec_in".

    dsnoop lets several programs read the same input at once: the recorder,
    the level meter and the headphone monitor. We only give this HOME to our
    own child processes, so the user's real ~/.asoundrc is never touched.
    """
    home.mkdir(parents=True, exist_ok=True)
    (home / ".asoundrc").write_text(f"""# Written by audio-recorder. Changes are overwritten.
pcm.{SHARED_PCM} {{
    type dsnoop
    ipc_key 47110
    ipc_perm 0666
    slave {{
        pcm "{device}"
        rate {rate}
        channels {channels}
        format {alsa_format}
        period_size 256
        buffer_size 1024
    }}
}}
""")


class SettingsStore:
    """Small JSON file in DATA_DIR that remembers the user's choices."""

    def __init__(self, data_dir: Path):
        self.path = data_dir / "settings.json"

    def load(self) -> dict:
        try:
            return json.loads(self.path.read_text())
        except (OSError, ValueError):
            return {}

    def save(self, **values) -> None:
        data = self.load()
        data.update(values)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        tmp.replace(self.path)
