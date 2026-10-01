"""App settings, read from environment variables (with sensible defaults).

Defaults fit the Fifine SC1 USB card: 48 kHz, 16-bit, 2 channels.
"""

import os
from dataclasses import dataclass
from pathlib import Path


def _bool(value: str) -> bool:
    return value.strip().lower() in ("1", "true", "yes", "on")


@dataclass
class Settings:
    audio_device: str  # "auto", "test", or an ALSA name like "hw:1,0"
    playback_device: str  # "auto" = same card as the input
    sample_rate: int
    channels: int
    format: str  # "flac" or "wav"
    bit_depth: int  # 16 or 24
    record_mono: str  # "off", "left", "right", "mix"
    use_dsnoop: bool
    monitor_default: bool
    monitor_latency_us: int
    recordings_dir: Path
    data_dir: Path
    max_duration_min: float
    min_free_disk_mb: float

    @classmethod
    def from_env(cls) -> "Settings":
        env = os.environ.get
        s = cls(
            audio_device=env("AUDIO_DEVICE", "auto"),
            playback_device=env("PLAYBACK_DEVICE", "auto"),
            sample_rate=int(env("SAMPLE_RATE", "48000")),
            channels=int(env("CHANNELS", "2")),
            format=env("FORMAT", "flac").lower(),
            bit_depth=int(env("BIT_DEPTH", "16")),
            record_mono=env("RECORD_MONO", "off").lower(),
            use_dsnoop=_bool(env("USE_DSNOOP", "true")),
            monitor_default=_bool(env("MONITOR_DEFAULT", "off")),
            monitor_latency_us=int(env("MONITOR_LATENCY_US", "10000")),
            recordings_dir=Path(env("RECORDINGS_DIR", "/recordings")).resolve(),
            data_dir=Path(env("DATA_DIR", "/data")).resolve(),
            max_duration_min=float(env("MAX_DURATION_MIN", "120")),
            min_free_disk_mb=float(env("MIN_FREE_DISK_MB", "1000")),
        )
        if s.format not in ("flac", "wav"):
            raise ValueError("FORMAT must be 'flac' or 'wav'")
        if s.bit_depth not in (16, 24):
            raise ValueError("BIT_DEPTH must be 16 or 24")
        if s.record_mono not in ("off", "left", "right", "mix"):
            raise ValueError("RECORD_MONO must be off, left, right or mix")
        return s

    @property
    def alsa_format(self) -> str:
        """Sample format name for ALSA (asoundrc, alsaloop)."""
        return "S16_LE" if self.bit_depth == 16 else "S32_LE"

    @property
    def out_channels(self) -> int:
        """Channels in the saved file."""
        return 1 if self.record_mono != "off" else self.channels
