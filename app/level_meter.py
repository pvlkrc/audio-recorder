"""Live input level (peak dBFS per channel).

The meter gets raw 16-bit PCM (s16le). It comes either from its own ffmpeg
process (shared input), or from the recording ffmpeg (second output) when the
input cannot be shared.
"""

import asyncio
import logging
import math
import time

import numpy as np

from .audio_input import AudioInput, child_setup

log = logging.getLogger(__name__)

SILENCE_DB = -90.0
CLIP_DB = -0.5  # at or above this we show "CLIP"
WINDOW_S = 0.1  # one value every 100 ms (10 updates per second)


def peak_db(samples: np.ndarray) -> float:
    """Peak of int16 samples in dBFS."""
    if samples.size == 0:
        return SILENCE_DB
    peak = int(np.max(np.abs(samples.astype(np.int32))))
    if peak == 0:
        return SILENCE_DB
    return max(SILENCE_DB, round(20 * math.log10(peak / 32768), 1))


class LevelMeter:
    def __init__(self, channels: int, sample_rate: int):
        self.channels = channels
        self.window_bytes = int(sample_rate * WINDOW_S) * channels * 2
        self._buf = bytearray()
        self.latest = {"peak_db": [SILENCE_DB] * channels, "clip": False, "active": False}
        self._updated = 0.0

        self.audio: AudioInput | None = None
        self.clients = 0
        self._proc: asyncio.subprocess.Process | None = None
        self._task: asyncio.Task | None = None
        self._paused = False  # True while the recorder owns the (not shared) device

    # ---- level computation -------------------------------------------------

    def feed(self, data: bytes) -> None:
        """Add raw s16le bytes. Publishes a new value every WINDOW_S of audio."""
        self._buf += data
        while len(self._buf) >= self.window_bytes:
            chunk = bytes(self._buf[: self.window_bytes])
            del self._buf[: self.window_bytes]
            frames = np.frombuffer(chunk, dtype="<i2").reshape(-1, self.channels)
            peaks = [peak_db(frames[:, c]) for c in range(self.channels)]
            self.latest = {
                "peak_db": peaks,
                "clip": any(p >= CLIP_DB for p in peaks),
                "active": True,
            }
            self._updated = time.monotonic()

    def current(self) -> dict:
        """Latest value; shows silence if no audio came for a while."""
        if time.monotonic() - self._updated > 1.0:
            return {"peak_db": [SILENCE_DB] * self.channels, "clip": False, "active": False}
        return self.latest

    # ---- own ffmpeg process (runs only while somebody looks at the meter) ---

    async def client_connected(self) -> None:
        self.clients += 1
        await self._update()

    async def client_disconnected(self) -> None:
        self.clients = max(0, self.clients - 1)
        await self._update()

    async def pause(self) -> None:
        """Free the input device (the recorder needs it and will feed us)."""
        self._paused = True
        await self._update()

    async def resume(self) -> None:
        self._paused = False
        await self._update()

    async def restart(self) -> None:
        """Input device changed: reopen it."""
        await self._stop_proc()
        await self._update()

    async def _update(self) -> None:
        want = self.clients > 0 and not self._paused and self.audio is not None
        running = self._task is not None and not self._task.done()
        if want and not running:
            self._task = asyncio.create_task(self._run())
        elif not want and running:
            await self._stop_proc()

    async def _run(self) -> None:
        """Run ffmpeg -> s16le on stdout; restart it if it stops."""
        while True:
            audio = self.audio
            cmd = [
                "ffmpeg", "-hide_banner", "-nostats", "-loglevel", "error", "-nostdin",
                *audio.ffmpeg_input_args(),
                "-f", "s16le", "-c:a", "pcm_s16le", "-ac", str(self.channels), "pipe:1",
            ]
            try:
                self._proc = await asyncio.create_subprocess_exec(
                    *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                    env=audio.env(), preexec_fn=child_setup,
                )
            except OSError as e:
                log.error("meter: cannot start ffmpeg: %s", e)
                await asyncio.sleep(5)
                continue
            while True:
                data = await self._proc.stdout.read(4096)
                if not data:
                    break
                self.feed(data)
            await self._proc.wait()
            err = (await self._proc.stderr.read()).decode(errors="replace").strip()
            log.warning("meter: ffmpeg stopped (%s): %s", self._proc.returncode, err[-300:])
            await asyncio.sleep(2)  # e.g. device busy or unplugged: try again later

    async def _stop_proc(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        if self._proc and self._proc.returncode is None:
            self._proc.kill()
            await self._proc.wait()
        self._proc = None
