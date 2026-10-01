"""Recording files: safe names, listing, rename, delete, disk space, WAV repair."""

import re
import shutil
import struct
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

import mutagen

AUDIO_EXTENSIONS = (".flac", ".wav", ".mp3")
PART_SUFFIX = ".part"  # unfinished recording: ".<final name>.part"

# Only these characters may appear in a file name.
_SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9._ -]+$")
# "2026-10-01_20-40-00" or "2026-10-01_20-40-00_tag"
_DATE_PREFIX_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2})(?:_(.*))?$")


class InvalidName(ValueError):
    """The file name is not allowed (path traversal, bad characters, ...)."""


def slugify(text: str, max_len: int = 60) -> str:
    """Make a user tag safe for a file name: "Bass riff #2!" -> "bass-riff-2"."""
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    text = re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-").lower()
    return text[:max_len].strip("-")


def build_filename(tag: str, ext: str, now: datetime | None = None) -> str:
    """YYYY-MM-DD_HH-MM-SS[_tag].<ext>"""
    now = now or datetime.now()
    stem = now.strftime("%Y-%m-%d_%H-%M-%S")
    slug = slugify(tag or "")
    if slug:
        stem += "_" + slug
    return f"{stem}.{ext}"


def safe_path(base: Path, name: str) -> Path:
    """Return base/name, but only if name is a plain, visible audio file name.

    Rejects: "/", "\\", "..", hidden files, other extensions, odd characters.
    """
    if not name or len(name) > 200:
        raise InvalidName("bad file name")
    if "/" in name or "\\" in name or name.startswith(".") or ".." in name:
        raise InvalidName("bad file name")
    if not _SAFE_NAME_RE.match(name):
        raise InvalidName("bad characters in file name")
    if not name.lower().endswith(AUDIO_EXTENSIONS):
        raise InvalidName("not an audio file")
    path = (base / name).resolve()
    if path.parent != base.resolve():
        raise InvalidName("bad file name")
    return path


def duration_of(path: Path) -> float | None:
    """Length in seconds, or None if the file cannot be read."""
    try:
        info = mutagen.File(path)
        return round(info.info.length, 2) if info else None
    except Exception:
        return None


# Cache: path -> ((mtime, size), duration). Reading tags is cheap, but lists can be long.
_duration_cache: dict[str, tuple[tuple[float, int], float | None]] = {}


def list_recordings(base: Path) -> list[dict]:
    """All visible audio files, newest first."""
    items = []
    for p in base.iterdir():
        if not p.is_file() or p.name.startswith(".") or not p.name.lower().endswith(AUDIO_EXTENSIONS):
            continue
        st = p.stat()
        key = (st.st_mtime, st.st_size)
        cached = _duration_cache.get(str(p))
        if cached and cached[0] == key:
            duration = cached[1]
        else:
            duration = duration_of(p)
            _duration_cache[str(p)] = (key, duration)
        items.append({
            "name": p.name,
            "size": st.st_size,
            "mtime": st.st_mtime,
            "date": datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat(timespec="seconds"),
            "duration": duration,
        })
    items.sort(key=lambda x: x["mtime"], reverse=True)
    return items


def rename_recording(base: Path, old: str, new_tag: str) -> str:
    """Rename: keep the date prefix and the extension, replace only the tag.

    If the old name has no date prefix, the new name is just "<slug>.<ext>".
    Returns the new file name.
    """
    src = safe_path(base, old)
    if not src.exists():
        raise FileNotFoundError(old)
    stem, ext = src.stem, src.suffix
    slug = slugify(new_tag)
    m = _DATE_PREFIX_RE.match(stem)
    if m:
        new_stem = m.group(1) + (f"_{slug}" if slug else "")
    else:
        if not slug:
            raise InvalidName("name is empty")
        new_stem = slug
    new_name = new_stem + ext
    dst = safe_path(base, new_name)
    if dst == src:
        return new_name
    if dst.exists():
        raise FileExistsError(new_name)
    src.rename(dst)
    # Keep side files (markers, mp3 export) together with the recording.
    side = base / f"{src.name}.markers.json"
    if side.exists():
        side.rename(base / f"{new_name}.markers.json")
    return new_name


def delete_recording(base: Path, name: str) -> None:
    path = safe_path(base, name)
    if not path.exists():
        raise FileNotFoundError(name)
    path.unlink()
    (base / f"{name}.markers.json").unlink(missing_ok=True)


def disk_free_mb(path: Path) -> float:
    return shutil.disk_usage(path).free / (1024 * 1024)


def fix_wav_header(path: Path) -> None:
    """Write correct RIFF and data chunk sizes, based on the real file size.

    ffmpeg writes the sizes only at the end. If it was killed, they are 0 or wrong.
    """
    size = path.stat().st_size
    with open(path, "r+b") as f:
        if f.read(4) != b"RIFF":
            return
        f.seek(12)
        while True:
            hdr = f.read(8)
            if len(hdr) < 8:
                return
            chunk_id, chunk_size = struct.unpack("<4sI", hdr)
            if chunk_id == b"data":
                data_start = f.tell()
                data_size = size - data_start
                f.seek(data_start - 4)
                f.write(struct.pack("<I", min(data_size, 0xFFFFFFFF)))
                f.seek(4)
                f.write(struct.pack("<I", min(size - 8, 0xFFFFFFFF)))
                return
            # Skip other chunks (fmt, LIST, ...). Chunks are padded to even size.
            f.seek(chunk_size + (chunk_size & 1), 1)
