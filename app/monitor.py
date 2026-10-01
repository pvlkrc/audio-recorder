"""Software headphone monitor: send the input to the card's headphone output.

Uses `alsaloop` (from alsa-utils). It reads the shared input "rec_in", so it
can run while recording. The card's own direct monitor has no latency, so it
is the better choice if the card has it. Do not use both at once (echo).
"""

import asyncio
import logging

from .audio_input import AudioInput, child_setup

log = logging.getLogger(__name__)


class Monitor:
    def __init__(self, latency_us: int):
        self.latency_us = latency_us
        self.audio: AudioInput | None = None
        self.playback: str | None = None  # e.g. "hw:CARD=SC1,DEV=0"
        self.enabled = False
        self.last_error: str | None = None
        self._task: asyncio.Task | None = None
        self._proc: asyncio.subprocess.Process | None = None

    @property
    def available(self) -> bool:
        return (
            self.audio is not None
            and not self.audio.is_test
            and self.audio.settings.use_dsnoop
            and self.playback is not None
        )

    def unavailable_reason(self) -> str | None:
        if self.audio is None or self.audio.is_test:
            return "no sound card selected"
        if not self.audio.settings.use_dsnoop:
            return "needs USE_DSNOOP=true"
        if self.playback is None:
            return "the card has no playback (headphone) device"
        return None

    def status(self) -> dict:
        return {
            "enabled": self.enabled,
            "running": self._proc is not None and self._proc.returncode is None,
            "available": self.available,
            "reason": self.unavailable_reason(),
            "playback": self.playback,
            "latency_ms": self.latency_us / 1000,
            "last_error": self.last_error,
        }

    async def set_enabled(self, enabled: bool) -> None:
        self.enabled = enabled
        await self.apply()

    async def apply(self) -> None:
        """Start or stop alsaloop to match `enabled`."""
        await self._stop()
        if self.enabled and self.available:
            self.last_error = None
            self._task = asyncio.create_task(self._run())

    def command(self) -> list[str]:
        s = self.audio.settings
        return [
            "alsaloop",
            "-C", self.audio.alsa_name,
            "-P", self.playback,
            "-r", str(s.sample_rate),
            "-c", str(s.channels),
            "-f", s.alsa_format,
            "-t", str(self.latency_us),
        ]

    async def _run(self) -> None:
        """Keep alsaloop running; restart it after an error."""
        while True:
            try:
                self._proc = await asyncio.create_subprocess_exec(
                    *self.command(),
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
                    env=self.audio.env(), preexec_fn=child_setup,
                )
            except OSError as e:
                self.last_error = f"cannot start alsaloop: {e}"
                return
            _, err = await self._proc.communicate()
            self.last_error = err.decode(errors="replace").strip()[-300:] or f"alsaloop exited ({self._proc.returncode})"
            log.warning("monitor: %s", self.last_error)
            await asyncio.sleep(3)

    async def _stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        if self._proc and self._proc.returncode is None:
            self._proc.terminate()
            try:
                await asyncio.wait_for(self._proc.wait(), 3)
            except asyncio.TimeoutError:
                self._proc.kill()
                await self._proc.wait()
        self._proc = None

    async def shutdown(self) -> None:
        await self._stop()
