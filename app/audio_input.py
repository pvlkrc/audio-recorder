"""Which input to read and how to start child processes (ffmpeg, alsaloop).

All audio programs read the same input. With dsnoop (default) they read the
shared ALSA device "rec_in", so the recorder, the level meter and the
headphone monitor can run at the same time.
"""

import ctypes
import os
import signal
from dataclasses import dataclass
from pathlib import Path

from .config import Settings
from .devices import SHARED_PCM, TEST_DEVICE, write_asoundrc

_PR_SET_PDEATHSIG = 1
try:
    _libc = ctypes.CDLL("libc.so.6", use_errno=True)
except OSError:  # not Linux / no glibc
    _libc = None


def child_setup() -> None:
    """Runs in the child before exec.

    - Own session: Ctrl-C in a terminal / signals to the app do not hit ffmpeg
      directly, so the app can stop it cleanly ("q").
    - If the app dies, the kernel sends SIGINT to the child. ffmpeg then
      finishes the file properly instead of recording forever.
    """
    os.setsid()
    if _libc is not None:
        _libc.prctl(_PR_SET_PDEATHSIG, signal.SIGINT)


@dataclass
class AudioInput:
    settings: Settings
    device: str  # selected capture device, e.g. "hw:CARD=SC1,DEV=0" or "test"
    alsa_home: Path  # HOME for child processes (contains our .asoundrc)

    @property
    def is_test(self) -> bool:
        return self.device == TEST_DEVICE

    @property
    def shared(self) -> bool:
        """True if several programs can read the input at once (dsnoop or test tone)."""
        return self.is_test or self.settings.use_dsnoop

    def prepare(self) -> None:
        """Write the dsnoop config for the current device."""
        if not self.is_test and self.settings.use_dsnoop:
            s = self.settings
            write_asoundrc(self.alsa_home, self.device, s.sample_rate, s.channels, s.alsa_format)

    @property
    def alsa_name(self) -> str:
        """ALSA device name that our programs open."""
        return SHARED_PCM if self.settings.use_dsnoop else self.device

    def env(self) -> dict:
        env = dict(os.environ)
        env["HOME"] = str(self.alsa_home)
        return env

    def ffmpeg_input_args(self) -> list[str]:
        s = self.settings
        if self.is_test:
            # Two test tones (left 220 Hz, right 330 Hz), slowly pulsing so
            # the meter moves. "-re" = real time, like a real sound card.
            ch = "|".join(
                f"{0.5 if i == 0 else 0.3}*sin(2*PI*{220 + 110 * i}*t)*(0.6+0.4*sin(2*PI*0.5*t))"
                for i in range(s.channels)
            )
            return ["-re", "-f", "lavfi", "-i", f"aevalsrc={ch}:s={s.sample_rate}"]
        # ffmpeg's ALSA input reads 16-bit by default; for 24-bit cards ask for 32-bit samples.
        codec = ["-c:a", "pcm_s32le"] if s.bit_depth == 24 else []
        return [
            "-f", "alsa",
            *codec,
            "-thread_queue_size", "1024",
            "-sample_rate", str(s.sample_rate),
            "-channels", str(s.channels),
            "-i", self.alsa_name,
        ]
