"""Recording control: one ffmpeg process at a time, clean stop, safety limits, crash recovery."""

import asyncio
import json
import logging
import os
import signal
import subprocess
import time
from collections import deque
from pathlib import Path

from .audio_input import AudioInput, child_setup
from .config import Settings
from .files import PART_SUFFIX, build_filename, disk_free_mb, fix_wav_header
from .level_meter import LevelMeter

log = logging.getLogger(__name__)

START_CHECK_S = 0.7  # wait this long after start to see if ffmpeg fails at once


class RecorderError(Exception):
    """Error with a message for the user. `status` is the HTTP status code."""

    def __init__(self, message: str, status: int = 409):
        super().__init__(message)
        self.status = status


def part_path(directory: Path, final_name: str) -> Path:
    """Hidden temp file for a running recording: ".<final>.part"."""
    return directory / f".{final_name}{PART_SUFFIX}"


def final_name_of(part: Path) -> str:
    return part.name[1 : -len(PART_SUFFIX)]


def _unique(directory: Path, name: str, check_parts: bool = True) -> str:
    """Add -2, -3, ... if the name is already taken."""
    stem, dot, ext = name.rpartition(".")
    candidate, n = name, 2
    while (directory / candidate).exists() or (check_parts and part_path(directory, candidate).exists()):
        candidate = f"{stem}-{n}{dot}{ext}"
        n += 1
    return candidate


def finalize_part(part: Path, repair_flac: bool) -> Path:
    """Make a .part file a normal, playable recording and give it the final name.

    - WAV: fix the header sizes (needed if ffmpeg was killed).
    - FLAC: if ffmpeg did not finish cleanly, the header has no length. Encode
      it again (FLAC -> FLAC is lossless), so the header gets the correct length.
      A plain stream copy ("-c copy") keeps the broken header.
    """
    directory = part.parent
    final = directory / _unique(directory, final_name_of(part), check_parts=False)
    if final.suffix == ".wav":
        fix_wav_header(part)
    elif final.suffix == ".flac" and repair_flac:
        tmp = directory / f".{final.name}.repair"
        r = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
             "-i", str(part), "-c:a", "flac", "-f", "flac", str(tmp)],
            capture_output=True, text=True,
        )
        if r.returncode == 0 and tmp.exists() and tmp.stat().st_size > 0:
            tmp.replace(part)
        else:
            log.warning("FLAC repair failed for %s: %s", part, r.stderr[-300:])
            tmp.unlink(missing_ok=True)
    part.rename(final)
    return final


def recover_parts(directory: Path) -> list[str]:
    """On startup: repair recordings that were cut off by a crash."""
    recovered = []
    for part in directory.glob(f".*{PART_SUFFIX}"):
        try:
            if part.stat().st_size == 0:
                part.unlink()
                continue
            final = finalize_part(part, repair_flac=True)
            recovered.append(final.name)
            log.info("recovered unfinished recording: %s", final.name)
        except Exception:
            log.exception("could not recover %s", part)
    return recovered


class Recorder:
    def __init__(self, settings: Settings, meter: LevelMeter):
        self.settings = settings
        self.meter = meter
        self.audio: AudioInput | None = None
        self._lock = asyncio.Lock()
        self._proc: asyncio.subprocess.Process | None = None
        self._tasks: list[asyncio.Task] = []
        self._stderr: deque[str] = deque(maxlen=20)
        self.state = "idle"  # idle | recording | stopping
        self.part: Path | None = None
        self.filename: str | None = None
        self.started_at: float | None = None  # time.monotonic()
        self.started_wall: float | None = None  # time.time()
        self.last_error: str | None = None
        self.last_stop_reason: str | None = None
        self.last_file: str | None = None
        self.markers: list[dict] = []

    @property
    def recording(self) -> bool:
        return self.state != "idle"

    def elapsed(self) -> float:
        return time.monotonic() - self.started_at if self.started_at else 0.0

    def status(self) -> dict:
        size = 0
        if self.part and self.part.exists():
            size = self.part.stat().st_size
        return {
            "state": self.state,
            "elapsed": round(self.elapsed(), 1) if self.recording else 0,
            "file": self.filename,
            "size": size,
            "last_error": self.last_error,
            "last_stop_reason": self.last_stop_reason,
            "last_file": self.last_file,
            "markers": self.markers,
        }

    # ---- ffmpeg command ------------------------------------------------------

    def _output_args(self, out: Path) -> list[str]:
        s = self.settings
        # Write every packet at once: the size in the UI is live, and less is lost in a crash.
        args: list[str] = ["-flush_packets", "1"]
        if s.record_mono == "left":
            args += ["-af", "pan=mono|c0=c0"]
        elif s.record_mono == "right" and s.channels > 1:
            args += ["-af", "pan=mono|c0=c1"]
        elif s.record_mono == "mix":
            args += ["-ac", "1"]
        if s.format == "flac":
            args += ["-c:a", "flac"]
            args += ["-sample_fmt", "s16"] if s.bit_depth == 16 else ["-sample_fmt", "s32", "-bits_per_raw_sample", "24"]
            args += ["-f", "flac"]
        else:
            args += ["-c:a", "pcm_s16le" if s.bit_depth == 16 else "pcm_s24le", "-f", "wav"]
        return args + [str(out)]

    def build_command(self, out: Path, with_meter: bool) -> list[str]:
        cmd = ["ffmpeg", "-hide_banner", "-nostats", "-loglevel", "error", "-y",
               *self.audio.ffmpeg_input_args(), *self._output_args(out)]
        if with_meter:
            # Second output: raw PCM for the level meter (when the input is not shared).
            cmd += ["-f", "s16le", "-c:a", "pcm_s16le", "-ac", str(self.settings.channels), "pipe:1"]
        return cmd

    # ---- start / stop --------------------------------------------------------

    async def start(self, tag: str = "") -> dict:
        async with self._lock:
            if self.recording:
                raise RecorderError("A recording is already running.")
            if self.audio is None:
                raise RecorderError("No audio input device.", 503)
            s = self.settings
            free = disk_free_mb(s.recordings_dir)
            if free < s.min_free_disk_mb:
                raise RecorderError(f"Not enough free disk space ({free:.0f} MB, need {s.min_free_disk_mb:.0f} MB).", 507)

            name = _unique(s.recordings_dir, build_filename(tag, s.format))
            part = part_path(s.recordings_dir, name)
            with_meter = not self.audio.shared
            if with_meter:
                await self.meter.pause()  # free the device for the recorder

            self._stderr.clear()
            self._proc = await asyncio.create_subprocess_exec(
                *self.build_command(part, with_meter),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE if with_meter else asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
                env=self.audio.env(), preexec_fn=child_setup,
            )
            self._tasks = [asyncio.create_task(self._read_stderr(self._proc))]
            if with_meter:
                self._tasks.append(asyncio.create_task(self._feed_meter(self._proc)))

            # If the device is busy or the format is wrong, ffmpeg stops at once.
            try:
                await asyncio.wait_for(self._proc.wait(), START_CHECK_S)
            except asyncio.TimeoutError:
                pass
            if self._proc.returncode is not None:
                await asyncio.gather(*self._tasks, return_exceptions=True)
                part.unlink(missing_ok=True)
                self._proc = None
                if with_meter:
                    await self.meter.resume()
                raise RecorderError("Recording could not start: " + self._error_text())

            self.state = "recording"
            self.part, self.filename = part, name
            self.started_at, self.started_wall = time.monotonic(), time.time()
            self.last_error = self.last_stop_reason = None
            self.markers = []
            self._tasks.append(asyncio.create_task(self._watchdog()))
            log.info("recording started: %s", name)
            return self.status()

    async def stop(self, reason: str = "user") -> dict:
        async with self._lock:
            if not self.recording:
                raise RecorderError("Nothing is recording.")
            return await self._stop_locked(reason)

    async def _stop_locked(self, reason: str) -> dict:
        self.state = "stopping"
        proc = self._proc
        clean = await self._stop_process(proc)
        # Stop helper tasks (but not the task that is running this code).
        me = asyncio.current_task()
        for t in self._tasks:
            if t is not me and not t.done():
                t.cancel()
        await asyncio.gather(*(t for t in self._tasks if t is not me), return_exceptions=True)
        self._tasks = []

        final_name = None
        if self.part and self.part.exists() and self.part.stat().st_size > 0:
            final = await asyncio.to_thread(finalize_part, self.part, not clean)
            final_name = final.name
        elif self.part:
            self.part.unlink(missing_ok=True)

        if reason not in ("user", "shutdown"):
            self.last_error = reason  # auto stop: show it to the user
        self.last_stop_reason = reason
        self.last_file = final_name
        if final_name and self.markers:
            (self.settings.recordings_dir / f"{final_name}.markers.json").write_text(json.dumps(self.markers, indent=2))
        log.info("recording stopped (%s): %s", reason, final_name)

        self.state = "idle"
        self._proc = None
        self.part = self.filename = None
        self.started_at = self.started_wall = None
        if not self.audio.shared:
            await self.meter.resume()
        return self.status()

    async def _stop_process(self, proc: asyncio.subprocess.Process) -> bool:
        """Stop ffmpeg so it closes the file properly. Returns True if it exited cleanly.

        1) send "q" on stdin (ffmpeg's normal quit key)
        2) SIGINT (same as Ctrl-C)
        3) SIGKILL (last resort; the file is repaired afterwards)
        """
        if proc.returncode is not None:
            return proc.returncode == 0
        try:
            proc.stdin.write(b"q")
            await proc.stdin.drain()
            proc.stdin.close()
        except (BrokenPipeError, ConnectionResetError, RuntimeError):
            pass
        for sig, wait in ((None, 5), (signal.SIGINT, 5), (signal.SIGKILL, 5)):
            if sig is not None:
                try:
                    os.killpg(proc.pid, sig)
                except ProcessLookupError:
                    pass
            try:
                await asyncio.wait_for(proc.wait(), wait)
                return sig is None or sig == signal.SIGINT
            except asyncio.TimeoutError:
                continue
        return False

    def add_marker(self, note: str = "") -> dict:
        if not self.recording:
            raise RecorderError("Nothing is recording.")
        marker = {"t": round(self.elapsed(), 2), "note": note[:200]}
        self.markers.append(marker)
        return marker

    # ---- background tasks ----------------------------------------------------

    async def _watchdog(self) -> None:
        """Every second: check limits and if ffmpeg is still alive."""
        s = self.settings
        while self.state == "recording":
            await asyncio.sleep(1)
            if self.state != "recording":
                return
            reason = None
            if self._proc.returncode is not None:
                reason = "ffmpeg stopped: " + self._error_text()
            elif self.elapsed() >= s.max_duration_min * 60:
                reason = f"maximum length reached ({s.max_duration_min:g} min)"
            elif disk_free_mb(s.recordings_dir) < s.min_free_disk_mb:
                reason = f"disk almost full (under {s.min_free_disk_mb:g} MB free)"
            if reason:
                log.warning("auto stop: %s", reason)
                async with self._lock:
                    if self.state == "recording":
                        await self._stop_locked(reason)
                return

    async def _read_stderr(self, proc: asyncio.subprocess.Process) -> None:
        async for line in proc.stderr:
            text = line.decode(errors="replace").strip()
            if text:
                self._stderr.append(text)
                log.debug("ffmpeg: %s", text)

    async def _feed_meter(self, proc: asyncio.subprocess.Process) -> None:
        while True:
            data = await proc.stdout.read(4096)
            if not data:
                return
            self.meter.feed(data)

    def _error_text(self) -> str:
        text = " ".join(self._stderr) or "unknown error"
        low = text.lower()
        if "busy" in low:
            return "the audio device is busy (used by another program). " + text
        if "no such file" in low or "no such device" in low or "cannot open audio device" in low:
            return "the audio device was not found. " + text
        return text
