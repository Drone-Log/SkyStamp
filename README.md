# SkyStamp

Burn your DJI drone's flight data (altitude, speed, distance, GPS, camera settings, flight path) into your videos.
Pick the video, pick the `.SRT` file the drone saved next to it, choose a style, then click **Create video**.

**Website: [getskystamp.app](https://getskystamp.app)** · Works on **Windows, macOS and Linux**. ffmpeg is bundled, so there's nothing else to install.

## Features

- **Video + SRT → final video.** The SRT is found automatically when it sits next to the video.
- **Live preview** of the frame under the timeline slider, with the overlay drawn on top.
- **Styles:** HUD (drone OSD look + mini-map), Classic (data in the corners + speed box), Cinematic (lower-third panel), Minimal (one line of data), **or your own**: drag and drop elements in the built-in style editor.
- **Mini-map over a real map:** street map (OpenStreetMap) or satellite (Esri), downloaded once per video and
  cached. Without internet, only the path is drawn.
- **Customise:** which data to show, metric/imperial units, text size, accent color, position.
- **Output:** MP4 H.264 (plays everywhere), MP4 H.265 (smaller), or a **transparent ProRes 4444 overlay** to place
  on top of your video in DaVinci Resolve / Premiere / Final Cut.
- **GPS signal indicator** (green GPS / red NO GPS) and **speed measured by the drone**, read from the metadata DJI embeds
  in the MP4 (falls back to GPS-computed speed when absent).
- **Satellite count & battery** (optional): add the flight log from the DJI Fly app and SkyStamp shows the number
  of GPS satellites (`GPS 29`) and the battery level, frame by frame. See [Flight logs](#flight-logs-satellites--battery).
- **Hardware encoding** (Apple VideoToolbox, NVIDIA NVENC, Intel QSV, AMD AMF) when available, with automatic
  fallback to software.
- Keeps the original resolution, frame rate and audio.
- Reads both SRT formats: the newer `[latitude: …] [rel_alt: …]` one (Neo, Mini 3/4, Air 3, Avata…) and the
  older `GPS (…) H 5.5m` one (Mini 2, Air 2…).

## Install

Download the file for your system from [Releases](../../releases):

| System | File | First launch |
|---|---|---|
| Windows | `SkyStamp-windows-x64.zip` | Unzip, run `SkyStamp.exe`. If SmartScreen shows up: *More info → Run anyway*. |
| macOS (Apple Silicon / Intel) | `SkyStamp-macos-*.dmg` | Drag to Applications. Signed and notarized by Apple. |
| Linux | `SkyStamp-linux-x86_64.AppImage` | `chmod +x SkyStamp-*.AppImage` and run it. |

Or, with Python 3.10+:

```bash
pip install git+https://github.com/Drone-Log/SkyStamp
skystamp            # desktop app
skystamp-cli VIDEO.MP4 --style cinematic -o out.mp4
```

## Flight logs (satellites & battery)

The SRT file doesn't record how many GPS satellites the drone sees, but the DJI Fly app's flight log does.

1. **Copy the logs from your phone.** iPhone: Finder → your iPhone → *Files* → *DJI Fly* → drag `FlightRecords`
   to your Mac. Android: `Android/data/dji.go.v5/files/FlightRecord`. DJI RC 2: `DJI/dji.go.v5/FlightRecord`.
2. **Get a free DJI API key** (needed because DJI encrypts these logs): sign in at
   [developer.dji.com](https://developer.dji.com/user/apps/#all) → *Create App* → App Type **Open API** →
   activate it from the email → copy the **App Key**.
3. In SkyStamp click **DJI API key…** and paste it. It stays on your computer.

The log is matched to the video automatically by time (put the `FlightRecords` folder next to your videos or
choose the log with **Choose flight log…**). The decoding is done by
[dji-log-parser](https://github.com/lvauvillier/dji-log-parser), which asks DJI's key service for the
decryption keys of that log; the result is cached, so each log is sent only once. Serial numbers in the log are
never stored.

Command line: `skystamp-cli VIDEO.MP4 --flight-log FlightRecord_….txt` with the key in `$DJI_API_KEY` or
`~/.config/dji/api_key`.

## Command line

```
skystamp-cli DJI_0018_D.MP4                       # SRT found automatically → DJI_0018_D_skystamp.mp4
skystamp-cli in.MP4 --srt in.SRT -o out.mp4 --style minimal --position top-right
skystamp-cli in.MP4 --format overlay              # transparent .mov for editors
skystamp-cli in.MP4 --fields altitude,speed,map --units imperial --scale 1.3 --accent "#FF8800"
```

Run `skystamp-cli -h` for every option.

## Custom styles

Pick a style and click **Customize…** (or *More → New empty style*):

- **click** an element on the preview to select it, **drag** to move it (it snaps to the edges and the centre),
  **scroll** to resize it;
- in the side panel choose what it shows, its layout, labels, background and colour, or **Add…** /
  **Remove** elements: flight data, text & GPS signal, a big number, the mini-map;
- **Save style**: it appears in the list (marked ★) and works in the command line as `--style user:<name>`.

Styles are small `.skystyle` files (JSON) you can share with *More → Export / Import*. They live in
`~/Library/Application Support/SkyStamp/styles` (macOS), `%APPDATA%\SkyStamp\styles` (Windows) or
`~/.local/share/SkyStamp/styles` (Linux). The format is documented in
[`src/skystamp/styles/engine.py`](src/skystamp/styles/engine.py); the built-in styles are in
[`src/skystamp/assets/styles/`](src/skystamp/assets/styles).

## Development

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest
python -m skystamp                 # run the app
python packaging/build.py          # standalone app in dist/
```

Releases are built by GitHub Actions (`.github/workflows/release.yml`) when a `v*` tag is pushed.

## Privacy

SkyStamp has no servers, accounts, analytics or crash reporting. Videos, SRT files and flight logs are processed
on your computer and never uploaded. The only network connections, both optional, are:

- **map tiles** for the mini-map, from OpenStreetMap or Esri (*Map background → None* turns this off);
- **DJI's key service**, once per DJI Fly flight log you add, to get the keys that decrypt it (uses your DJI API key).

SkyStamp is provided as is, without warranty (MIT License). You're responsible for following the drone and
privacy rules where you fly and publish.

## How it works

`telemetry.py` parses the SRT (one entry per video frame) and computes speed, heading and distances from GPS.
`video.py` draws each overlay frame with Pillow and streams it to ffmpeg, which composites it onto the
original video and encodes it. The data refreshes 10×/s by default, while the video keeps its full frame rate.

## Credits

- Inspired by [dji-telemetry](https://github.com/jetervaz/dji-telemetry) and [GPStitch](https://github.com/Romancha/GPStitch). No code was copied.
- DJI Fly log decoding: [dji-log-parser](https://github.com/lvauvillier/dji-log-parser) by Luc Vauvillier (MIT), bundled in release builds.
- Map data © [OpenStreetMap contributors](https://www.openstreetmap.org/copyright); satellite imagery © Esri, Maxar, Earthstar Geographics. Attribution is drawn on the map.
- Font: [Barlow](https://github.com/jpt/barlow) (SIL Open Font License, `assets/fonts/OFL.txt`).
- Video processing by [FFmpeg](https://ffmpeg.org), bundled via [imageio-ffmpeg](https://github.com/imageio/imageio-ffmpeg) (GPL build; distributed as a separate program).
