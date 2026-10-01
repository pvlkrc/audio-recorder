"""Files for sharing to other apps (Messenger, Instagram, WhatsApp, ...).

The phone's share menu gets a ready file:
- "mp3": the recording as MP3 (Messenger, WhatsApp, e-mail, ...)
- "mp4": a video with a still picture (waveform, name, date) and the sound.
  Instagram direct messages accept only photos and videos, not audio files.

The files are made on demand and kept in DATA_DIR/share, so they do not show
in the recordings list (or in Navidrome).
"""

import asyncio
import re
import time
from pathlib import Path

from .files import duration_of

FORMATS = {"mp3": "audio/mpeg", "mp4": "video/mp4"}
CACHE_MAX_AGE_S = 24 * 3600
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
VIDEO_SIZE = 1080  # square video, fits Instagram and Messenger

_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})_(\d{2})-(\d{2})-\d{2}_?(.*)$")


class ShareError(Exception):
    pass


async def run_ffmpeg(*args: str) -> None:
    """Run ffmpeg; raise ShareError with the last error lines if it fails."""
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y", *args,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
    )
    _, err = await proc.communicate()
    if proc.returncode != 0:
        raise ShareError(err.decode(errors="replace").strip()[-300:] or "ffmpeg failed")


async def encode_mp3(src: Path, dst: Path) -> None:
    """MP3, VBR ~190 kbit/s. Writes to a temp file first, so dst is never half done."""
    tmp = dst.with_name(f".{dst.name}.tmp")
    try:
        await run_ffmpeg("-i", str(src), "-map", "0:a", "-c:a", "libmp3lame", "-q:a", "2", "-f", "mp3", str(tmp))
        tmp.replace(dst)
    finally:
        tmp.unlink(missing_ok=True)


def title_lines(name: str, duration: float | None) -> tuple[str, str]:
    """Big title + small line for the video picture.

    "2026-10-01_20-40-05_bass-riff.flac" -> ("bass-riff", "2026-10-01 20:40 · 3:12")
    """
    stem = Path(name).stem
    m = _DATE_RE.match(stem)
    if m:
        y, mo, d, h, mi, tag = m.groups()
        title, sub = tag or "Recording", f"{y}-{mo}-{d} {h}:{mi}"
    else:
        title, sub = stem, ""
    if duration:
        s = int(duration)
        length = f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"
        sub = f"{sub} · {length}" if sub else length
    return title, sub


async def make_cover(src: Path, png: Path) -> None:
    """Still picture: dark background, waveform of the whole recording, title, date."""
    size = VIDEO_SIZE
    title, sub = title_lines(src.name, duration_of(src))
    title_file = png.with_suffix(".title.txt")
    sub_file = png.with_suffix(".sub.txt")
    title_file.write_text(title)
    sub_file.write_text(sub)
    wave = (
        f"[1:a]aformat=channel_layouts=mono,"
        f"showwavespic=s={size - 120}x420:colors=0x5b9cff:split_channels=0[w];"
        f"[0:v][w]overlay=(W-w)/2:(H-h)/2+40"
    )
    # textfile= avoids any escaping problems with the names.
    text = (
        f",drawtext=fontfile={FONT}:textfile={title_file}:fontsize=72:fontcolor=white:"
        f"x=(w-text_w)/2:y=170,"
        f"drawtext=fontfile={FONT}:textfile={sub_file}:fontsize=40:fontcolor=0x8b919c:"
        f"x=(w-text_w)/2:y=270"
    )
    base = ["-f", "lavfi", "-i", f"color=c=0x111317:s={size}x{size}:d=1", "-i", str(src)]
    try:
        try:
            await run_ffmpeg(*base, "-filter_complex", wave + text, "-frames:v", "1", str(png))
        except ShareError:
            # No font or no drawtext in this ffmpeg: picture with waveform only.
            await run_ffmpeg(*base, "-filter_complex", wave, "-frames:v", "1", str(png))
    finally:
        title_file.unlink(missing_ok=True)
        sub_file.unlink(missing_ok=True)


async def encode_video(src: Path, dst: Path) -> None:
    """MP4 (H.264 + AAC) from the still picture and the sound. Low frame rate = small and fast."""
    png = dst.with_name(f".{dst.stem}.cover.png")
    tmp = dst.with_name(f".{dst.name}.tmp")
    try:
        await make_cover(src, png)
        await run_ffmpeg(
            "-loop", "1", "-framerate", "5", "-i", str(png), "-i", str(src),
            "-map", "0:v", "-map", "1:a",
            "-c:v", "libx264", "-preset", "veryfast", "-tune", "stillimage", "-pix_fmt", "yuv420p", "-r", "5",
            "-c:a", "aac", "-b:a", "192k", "-ac", "2",
            "-shortest", "-movflags", "+faststart", "-f", "mp4", str(tmp),
        )
        tmp.replace(dst)
    finally:
        png.unlink(missing_ok=True)
        tmp.unlink(missing_ok=True)


class ShareCache:
    def __init__(self, cache_dir: Path):
        self.dir = cache_dir
        self._locks: dict[str, asyncio.Lock] = {}

    def path_for(self, src: Path, fmt: str) -> Path:
        # Full name in the key, so "x.flac" and "x.wav" do not mix.
        return self.dir / f"{src.name}.{fmt}"

    async def get(self, src: Path, fmt: str) -> Path:
        """Return a ready file for sharing; make it if it is missing or older than the recording."""
        if fmt not in FORMATS:
            raise ShareError("unknown format")
        if fmt == "mp3" and src.suffix.lower() == ".mp3":
            return src
        self.dir.mkdir(parents=True, exist_ok=True)
        dst = self.path_for(src, fmt)
        lock = self._locks.setdefault(dst.name, asyncio.Lock())
        async with lock:  # two taps on "Share" make the file only once
            if not dst.exists() or dst.stat().st_mtime < src.stat().st_mtime:
                self.prune()
                if fmt == "mp3":
                    await encode_mp3(src, dst)
                else:
                    await encode_video(src, dst)
        return dst

    def forget(self, name: str) -> None:
        """Recording was renamed or deleted: drop its share files."""
        for fmt in FORMATS:
            (self.dir / f"{name}.{fmt}").unlink(missing_ok=True)

    def prune(self) -> None:
        """Delete share files older than one day (they are only needed for a moment)."""
        if not self.dir.exists():
            return
        limit = time.time() - CACHE_MAX_AGE_S
        for p in self.dir.iterdir():
            if p.is_file() and p.stat().st_mtime < limit:
                p.unlink(missing_ok=True)
