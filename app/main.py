"""FastAPI app: REST API, level WebSocket, static web page."""

import asyncio
import base64
import logging
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import devices as dev
from .audio_input import AudioInput
from .config import Settings
from .files import (
    InvalidName, delete_recording, disk_free_mb, list_recordings, rename_recording, safe_path,
)
from .level_meter import LevelMeter
from .monitor import Monitor
from .recorder import Recorder, RecorderError, recover_parts

log = logging.getLogger("audio_recorder")
STATIC_DIR = Path(__file__).parent / "static"
MEDIA_TYPES = {".flac": "audio/flac", ".wav": "audio/wav", ".mp3": "audio/mpeg"}


class StartBody(BaseModel):
    name: str = ""


class RenameBody(BaseModel):
    name: str


class DeviceBody(BaseModel):
    device: str


class MonitorBody(BaseModel):
    enabled: bool


class MarkerBody(BaseModel):
    note: str = ""


class Engine:
    """Holds all the parts of the app and switches the input device."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.store = dev.SettingsStore(settings.data_dir)
        self.meter = LevelMeter(settings.channels, settings.sample_rate)
        self.recorder = Recorder(settings, self.meter)
        self.monitor = Monitor(settings.monitor_latency_us)
        self.device: str | None = None
        self.hw_params: dict | None = None
        self.warnings: list[str] = []

    async def set_device(self, device: str) -> None:
        s = self.settings
        # Free the input before we read its parameters / rewrite the config.
        await self.monitor.shutdown()
        self.meter.audio = None
        await self.meter.restart()

        self.device = device
        self.hw_params = await asyncio.to_thread(dev.read_hw_params, device)
        self.warnings = self._check_params()
        audio = AudioInput(s, device, s.data_dir / "alsa-home")
        audio.prepare()
        self.recorder.audio = audio
        self.meter.audio = audio
        self.monitor.audio = audio
        self.monitor.playback = await asyncio.to_thread(dev.resolve_playback, device, s.playback_device)
        await self.meter.restart()
        await self.monitor.apply()
        log.info("input device: %s (playback: %s)", device, self.monitor.playback)

    def _check_params(self) -> list[str]:
        """Warn if the card does not support what the env vars ask for."""
        p, s, out = self.hw_params or {}, self.settings, []
        ch, rate, fmts = p.get("channels"), p.get("rate"), p.get("formats")
        if ch and not ch["min"] <= s.channels <= ch["max"]:
            out.append(f"CHANNELS={s.channels} is not supported (card: {ch['min']}-{ch['max']}).")
        if rate and not rate["min"] <= s.sample_rate <= rate["max"]:
            out.append(f"SAMPLE_RATE={s.sample_rate} is not supported (card: {rate['min']}-{rate['max']}).")
        if fmts and s.alsa_format not in fmts:
            out.append(f"BIT_DEPTH={s.bit_depth} ({s.alsa_format}) is not supported (card: {', '.join(fmts)}).")
        return out


def basic_auth_ok(header: str | None, user: str, password: str) -> bool:
    if not header or not header.lower().startswith("basic "):
        return False
    try:
        u, _, p = base64.b64decode(header[6:]).decode().partition(":")
    except Exception:
        return False
    return secrets.compare_digest(u, user) and secrets.compare_digest(p, password)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    engine = Engine(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        settings.recordings_dir.mkdir(parents=True, exist_ok=True)
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(recover_parts, settings.recordings_dir)
        saved = engine.store.load()
        engine.monitor.enabled = saved.get("monitor", settings.monitor_default)
        await engine.set_device(dev.resolve_device(saved.get("device"), settings.audio_device))
        yield
        # Shutdown: finish a running recording properly.
        if engine.recorder.recording:
            try:
                await engine.recorder.stop("shutdown")
            except RecorderError:
                pass
        await engine.monitor.shutdown()
        engine.meter.audio = None
        await engine.meter.restart()

    app = FastAPI(title="Audio Recorder", lifespan=lifespan)
    app.state.engine = engine
    rec_dir = settings.recordings_dir

    # ---- optional HTTP Basic Auth ------------------------------------------
    if settings.auth_user and settings.auth_pass:
        @app.middleware("http")
        async def auth(request: Request, call_next):
            if not basic_auth_ok(request.headers.get("authorization"), settings.auth_user, settings.auth_pass):
                return Response("Login required", 401, {"WWW-Authenticate": 'Basic realm="Audio Recorder"'})
            return await call_next(request)

    @app.exception_handler(RecorderError)
    async def recorder_error(_: Request, exc: RecorderError):
        return JSONResponse({"detail": str(exc)}, status_code=exc.status)

    def file_or_404(name: str) -> Path:
        try:
            path = safe_path(rec_dir, name)
        except InvalidName as e:
            raise HTTPException(400, str(e))
        if not path.is_file():
            raise HTTPException(404, "recording not found")
        return path

    # ---- status / recording ---------------------------------------------------

    @app.get("/api/status")
    async def status():
        return {
            **engine.recorder.status(),
            "device": engine.device,
            "disk_free_mb": round(disk_free_mb(rec_dir), 1),
            "warnings": engine.warnings,
            "monitor": engine.monitor.status(),
            "config": {
                "format": settings.format,
                "sample_rate": settings.sample_rate,
                "channels": settings.channels,
                "out_channels": settings.out_channels,
                "bit_depth": settings.bit_depth,
                "max_duration_min": settings.max_duration_min,
                "min_free_disk_mb": settings.min_free_disk_mb,
            },
        }

    @app.post("/api/record/start")
    async def record_start(body: StartBody | None = None):
        return await engine.recorder.start(body.name if body else "")

    @app.post("/api/record/stop")
    async def record_stop():
        return await engine.recorder.stop("user")

    @app.post("/api/record/marker")
    async def record_marker(body: MarkerBody | None = None):
        return engine.recorder.add_marker(body.note if body else "")

    # ---- recordings ---------------------------------------------------------

    @app.get("/api/recordings")
    async def recordings():
        return await asyncio.to_thread(list_recordings, rec_dir)

    @app.get("/api/recordings/{name}")
    async def recording_stream(name: str):
        path = file_or_404(name)
        # FileResponse supports HTTP Range requests (seeking in the player).
        return FileResponse(path, media_type=MEDIA_TYPES.get(path.suffix.lower()))

    @app.get("/api/recordings/{name}/download")
    async def recording_download(name: str):
        path = file_or_404(name)
        return FileResponse(path, media_type=MEDIA_TYPES.get(path.suffix.lower()), filename=path.name)

    @app.patch("/api/recordings/{name}")
    async def recording_rename(name: str, body: RenameBody):
        file_or_404(name)
        try:
            return {"name": rename_recording(rec_dir, name, body.name)}
        except InvalidName as e:
            raise HTTPException(400, str(e))
        except FileExistsError:
            raise HTTPException(409, "a recording with this name already exists")

    @app.delete("/api/recordings/{name}")
    async def recording_delete(name: str):
        file_or_404(name)
        delete_recording(rec_dir, name)
        return {"deleted": name}

    @app.post("/api/recordings/{name}/mp3")
    async def recording_mp3(name: str):
        """Make an MP3 copy (VBR ~190 kbit/s) next to the original."""
        src = file_or_404(name)
        if src.suffix.lower() == ".mp3":
            raise HTTPException(400, "already an MP3")
        dst = src.with_suffix(".mp3")
        if dst.exists():
            raise HTTPException(409, f"{dst.name} already exists")
        tmp = rec_dir / f".{dst.name}.tmp"
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
            "-i", str(src), "-c:a", "libmp3lame", "-q:a", "2", "-f", "mp3", str(tmp),
            stderr=asyncio.subprocess.PIPE,
        )
        _, err = await proc.communicate()
        if proc.returncode != 0:
            tmp.unlink(missing_ok=True)
            raise HTTPException(500, "MP3 export failed: " + err.decode(errors="replace")[-300:])
        tmp.rename(dst)
        return {"name": dst.name}

    @app.get("/api/recordings/{name}/markers")
    async def recording_markers(name: str):
        path = file_or_404(name)
        side = rec_dir / f"{path.name}.markers.json"
        return Response(side.read_text() if side.exists() else "[]", media_type="application/json")

    # ---- devices / monitor --------------------------------------------------

    @app.get("/api/devices")
    async def devices():
        found = await asyncio.to_thread(dev.list_capture_devices)
        found.append({"id": dev.TEST_DEVICE, "label": "Test tone (no sound card)"})
        return {"devices": found, "selected": engine.device, "hw_params": engine.hw_params}

    @app.put("/api/devices/selected")
    async def device_select(body: DeviceBody):
        if engine.recorder.recording:
            raise HTTPException(409, "Stop the recording before you change the device.")
        known = {d["id"] for d in await asyncio.to_thread(dev.list_capture_devices)} | {dev.TEST_DEVICE}
        if body.device not in known:
            raise HTTPException(400, "unknown device")
        await engine.set_device(body.device)
        engine.store.save(device=body.device)
        return {"selected": engine.device, "hw_params": engine.hw_params, "warnings": engine.warnings}

    @app.get("/api/monitor")
    async def monitor_get():
        return engine.monitor.status()

    @app.put("/api/monitor")
    async def monitor_set(body: MonitorBody):
        if body.enabled and not engine.monitor.available:
            raise HTTPException(409, f"Headphone monitor is not available: {engine.monitor.unavailable_reason()}")
        await engine.monitor.set_enabled(body.enabled)
        engine.store.save(monitor=body.enabled)
        return engine.monitor.status()

    # ---- live level ---------------------------------------------------------

    @app.websocket("/ws/level")
    async def ws_level(ws: WebSocket):
        await ws.accept()
        await engine.meter.client_connected()
        try:
            while True:
                await ws.send_json(engine.meter.current())
                await asyncio.sleep(0.1)
        except Exception:  # client went away
            pass
        finally:
            await engine.meter.client_disconnected()

    # Web page (last, so it does not hide the API routes).
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
    return app


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
app = create_app()
