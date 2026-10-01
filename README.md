# audio-recorder

A small self-hosted web app that records guitar / bass from a USB sound card on a
headless Linux server. You control it from the browser on your phone.

- Big **Record / Stop** button, timer, file name, live file size
- **Live input level** (peak dBFS, 10× per second) with peak hold and **CLIP** warning
- **Headphone monitor** switch (sends the input to the card's headphone jack)
- Recordings list (newest first): **play** (with seeking), **download**, **rename**, **delete**
- FLAC (default) or WAV; file names like `2026-10-01_20-40-05_bass-riff.flac`
- Device dropdown (from `arecord -l`); the choice is saved
- Safety: maximum length, auto stop when the disk is almost full, crash-safe files
- Extras: markers during recording, "Make MP3", waveform view
- **Share** to Messenger, Instagram, WhatsApp… with the phone's share menu (MP3 or MP4 video)

Defaults fit the **Fifine SC1** (48 kHz, 16-bit, 2 channels). Everything can be changed in `.env`.

---

## 1. Pass the USB sound card into Proxmox

You can use a **VM** or an **LXC container**. LXC has less USB delay; this is better
if you want to use the software headphone monitor.

### Option A: VM (USB passthrough)

On the **Proxmox host**, find the card:

```bash
lsusb
# Bus 001 Device 007: ID 1234:abcd  ... <- vendor:product of the card
```

Pass it to the VM (replace `<VMID>` and `<vendor:product>`):

```bash
qm set <VMID> -usb0 host=<vendor:product>,usb3=1
```

Restart the VM. If you hear crackling, try without `usb3=1`.

### Option B: LXC container (pass `/dev/snd`)

On the **Proxmox host**, check that the card is there and find the device number:

```bash
cat /proc/asound/cards
ls -l /dev/snd        # sound devices have major number 116
```

Add these lines to `/etc/pve/lxc/<CTID>.conf`:

```
lxc.cgroup2.devices.allow: c 116:* rwm
lxc.mount.entry: /dev/snd dev/snd none bind,optional,create=dir
```

To run Docker inside the LXC, enable nesting: `features: nesting=1,keyctl=1`.

**Unprivileged container:** the files in `/dev/snd` belong to `root:audio` on the
host. In the container they show as `nobody:nogroup`, so the app cannot open them.
The simplest fix is a udev rule on the host:

```bash
echo 'SUBSYSTEM=="sound", MODE="0666"' > /etc/udev/rules.d/99-sound-lxc.rules
udevadm control --reload && udevadm trigger
```

Restart the container.

## 2. Check the card inside the VM / LXC

```bash
sudo apt install alsa-utils
arecord -l
# card 1: SC1 [FIFINE SC1], device 0: USB Audio [USB Audio]   <- the name may be different

# What does the card support?
arecord -D hw:1,0 --dump-hw-params -d 1 /dev/null

# Test recording: 5 seconds, then play it on the card's headphone jack
arecord -D hw:1,0 -f S16_LE -r 48000 -c 2 -d 5 test.wav
aplay -D hw:1,0 test.wav
```

If the test recording is silent, check the gain knob and the input switch (instrument / mic) on the card.

## 3. Start

The image is built by GitHub Actions ([.github/workflows/deploy.yml](.github/workflows/deploy.yml))
and pushed to `ghcr.io/pvlkrc/audio-recorder` on every push to `main` (`:latest`)
and for every tag `v*` (e.g. `v1.0.0` -> `:1.0.0`).

On the server you only need `docker-compose.yml` and `.env`:

```bash
git clone git@github.com:pvlkrc/audio-recorder.git && cd audio-recorder
cp .env.example .env      # optional, change what you need
docker compose pull
docker compose up -d
```

Open `http://<server-ip>:8080` on your phone.

Quick try without a sound card: `AUDIO_DEVICE=test docker compose up -d`.

**Update** to the newest image: `docker compose pull && docker compose up -d`.
To stay on one version, set `IMAGE_TAG=1.0.0` in `.env`.

**Private package:** a new package on ghcr.io is private. Either make it public
(GitHub -> your profile -> Packages -> audio-recorder -> Package settings -> Change visibility),
or log in on the server once with a token that has `read:packages`:
`echo <TOKEN> | docker login ghcr.io -u pvlkrc --password-stdin`.

**Build locally** in place of pulling:
`docker compose -f docker-compose.yml -f docker-compose.build.yml up -d --build`.

Recordings are saved in `./recordings` (you can point Navidrome at this folder).
The container starts as root only to give `./recordings` and `./data` to the app user
(uid 1000, or `PUID` / `PGID`); the app itself does not run as root.
Settings (selected device, monitor on/off) are saved in `./data`.

## 4. Headphones: hear what you play

You have two ways to hear yourself while you play and record:

| | Direct monitor (on the card) | Software monitor (switch in the app) |
|---|---|---|
| Delay | **none** | about 10–20 ms (more in a VM) |
| How | monitor knob / switch on the card | 🎧 switch in the web page |
| Works while recording | yes | yes |

**Tip:** use the card's **direct monitor** if you can. It has no delay, so it feels
like playing into an amp. Use the software monitor only if the card has no direct monitor.

**Do not use both at the same time** — you will hear the sound twice (echo / flanger sound).

The software monitor uses `alsaloop`. It sends the input to the playback device of the
same card (`PLAYBACK_DEVICE=auto`). Lower `MONITOR_LATENCY_US` = less delay, but more risk of crackling.

## 5. Share to Messenger / Instagram

Each recording has a **📤 Share** button:

| Choice | What you get | Good for |
|---|---|---|
| 🎵 Audio (MP3) | the recording as MP3 | Messenger, WhatsApp, e-mail, ... |
| 🎬 Video (MP4) | a still picture (waveform, name, date) with the sound | **Instagram** (its messages do not take audio files), Messenger, WhatsApp |

1. Tap a choice. The server makes the file (a long video takes a moment).
2. Tap **Share**. The phone's share menu opens; choose Instagram / Messenger and the person.

These files are kept for one day in `./data/share` (not in `./recordings`, so Navidrome does not see them).

**The share menu needs HTTPS.** Browsers open it only on `https://` pages (or `localhost`).
On `http://<ip>:8080` the file is downloaded instead, and you share it from the Files app.

## 6. Settings (`.env`)

| Variable | Default | Meaning |
|---|---|---|
| `AUDIO_DEVICE` | `auto` | `auto` = first capture card, `test` = sine tone, or e.g. `hw:CARD=SC1,DEV=0`. A device chosen in the web page wins. |
| `FORMAT` | `flac` | `flac` or `wav` |
| `SAMPLE_RATE` | `48000` | Hz |
| `CHANNELS` | `2` | channels read from the card |
| `BIT_DEPTH` | `16` | `16` or `24` (the SC1 has 16-bit) |
| `RECORD_MONO` | `off` | `left` / `right` / `mix` = save a mono file (guitar is often on one channel only) |
| `USE_DSNOOP` | `true` | share the input between recorder, meter and monitor |
| `PLAYBACK_DEVICE` | `auto` | headphone output for the software monitor |
| `MONITOR_DEFAULT` | `off` | software monitor on at the first start |
| `MONITOR_LATENCY_US` | `10000` | software monitor delay (µs) |
| `MAX_DURATION_MIN` | `120` | recording stops after this time |
| `MIN_FREE_DISK_MB` | `1000` | recording does not start / stops below this free space |
| `TZ` | `Europe/Prague` | time zone for file names |
| `PUID` / `PGID` | `1000` | owner of the recording files on the host (check with `id`) |
| `PORT` | `8080` | web port |

The app writes its own small ALSA config (`./data/alsa-home/.asoundrc`) with a shared input
called `rec_in` (ALSA `dsnoop`). Because of this, the level meter, the recording and the
headphone monitor can all use the card at the same time.

## 7. Crash safety

- A running recording is written to a hidden file `.<name>.flac.part`. Navidrome does not see it.
- **Stop** sends `q` to ffmpeg (then SIGINT, and only at the end SIGKILL), so the file is closed properly.
- `docker stop` / restart: the app stops the recording cleanly first.
- If the app dies, the kernel tells ffmpeg to stop (SIGINT), so ffmpeg finishes the file.
- If everything is killed hard (power loss, `docker kill`), the next start **repairs** the `.part` file
  (WAV: fixes the header; FLAC: writes the file again so the length is correct) and gives it its normal name.

## 8. Troubleshooting

**"The audio device is busy"**
- Another program uses the card. Check with `fuser -v /dev/snd/*` (in the VM / LXC and, for LXC, on the host).
- In LXC, the Proxmox host normally does not use the card. If you installed PulseAudio / PipeWire somewhere, stop it.
- An old recording from the command line (`arecord`) still runs.

**Wrong sample rate / "Invalid argument" / no sound**
- Run `arecord -D hw:1,0 --dump-hw-params -d 1 /dev/null` and set `SAMPLE_RATE`, `CHANNELS` and `BIT_DEPTH`
  to values the card supports. The web page shows a ⚠ warning when they do not match.
- Last resort: set `AUDIO_DEVICE=plughw:1,0` (ALSA converts the format, a little more CPU).

**Permission denied on `/dev/snd`**
- The container must be in the `audio` group (`group_add: [audio]` in `docker-compose.yml`).
- If the group number is different: check `ls -ln /dev/snd` and use the number, e.g. `group_add: ["29"]`.
- Unprivileged LXC: see the udev rule in section 1.

**Crackling / dropouts**
- VM: USB passthrough can cause crackling. Try with and without `usb3=1`, or use LXC.
- Give the VM / LXC at least 2 CPU cores. Use a short, good USB cable; no passive USB hub.
- Software monitor crackles: raise `MONITOR_LATENCY_US` (e.g. `20000`), or use the card's direct monitor.

**The level meter shows "no signal"**
- Check the gain knob on the card and the cable.
- If the card is not shared (`USE_DSNOOP=false`), the meter only works while recording.

**Time in file names is wrong** — set `TZ` in `.env`.

## 9. API

| Method | Path | |
|---|---|---|
| GET | `/api/status` | state, time, file, size, level settings, disk space |
| POST | `/api/record/start` | body `{"name": "bass-riff"}` (optional) |
| POST | `/api/record/stop` | |
| POST | `/api/record/marker` | body `{"note": "..."}` (optional) |
| GET | `/api/recordings` | list, newest first |
| GET | `/api/recordings/{file}` | stream (HTTP Range for seeking) |
| GET | `/api/recordings/{file}/download` | download |
| PATCH | `/api/recordings/{file}` | rename, body `{"name": "new-tag"}` (the date stays) |
| DELETE | `/api/recordings/{file}` | delete |
| POST | `/api/recordings/{file}/mp3` | make an MP3 copy |
| GET | `/api/recordings/{file}/share/mp3` | MP3 for sharing |
| GET | `/api/recordings/{file}/share/mp4` | video (still picture + sound) for sharing |
| GET | `/api/recordings/{file}/markers` | markers of a recording |
| GET | `/api/devices` | capture devices + selected |
| PUT | `/api/devices/selected` | body `{"device": "hw:CARD=SC1,DEV=0"}` |
| GET / PUT | `/api/monitor` | body `{"enabled": true}` |
| WS | `/ws/level` | `{"peak_db": [-12.3, -14.0], "clip": false, "active": true}` |

## 10. Development and tests (no sound card needed)

`AUDIO_DEVICE=test` uses an ffmpeg test tone in place of a sound card.

```bash
# Tests in Docker (includes ffmpeg):
docker build --target test -t audio-recorder:test . && docker run --rm audio-recorder:test

# Or locally (needs ffmpeg; tests that need ffmpeg are skipped without it):
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/pytest

# Run locally with the test tone:
AUDIO_DEVICE=test RECORDINGS_DIR=./rec DATA_DIR=./data .venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8080
```

A real ALSA device without hardware: load the loopback driver on the host
(`sudo modprobe snd-aloop`). Play a file into `hw:Loopback,0` (`aplay -D hw:Loopback,0 song.wav`)
and record from `hw:Loopback,1` in the app.
