# SkyStamp

Burn your DJI drone's flight data (altitude, speed, distance, GPS, camera settings, flight path) into your videos.
Pick the video, pick the `.SRT` file the drone saved next to it, choose a style, then click **Create video**.

Works on **Windows, macOS and Linux**. ffmpeg is bundled, so there's nothing else to install.

## Features

- **Video + SRT → final video.** The SRT is found automatically when it sits next to the video.
- **Live preview** of the frame under the timeline slider, with the overlay drawn on top.
- **Styles:** HUD (drone OSD look + mini-map), Classic (data in the corners + speed box), Cinematic (lower-third panel), Minimal (one line of data).
- **Mini-map over a real map:** street map (OpenStreetMap) or satellite (Esri), downloaded once per video and
  cached. Without internet, only the path is drawn.
- **Customise:** which data to show, metric/imperial units, text size, accent color, position.
- **Output:** MP4 H.264 (plays everywhere), MP4 H.265 (smaller), or a **transparent ProRes 4444 overlay** to place
  on top of your video in DaVinci Resolve / Premiere / Final Cut.
- **GPS signal indicator** (green GPS / red NO GPS) and **speed measured by the drone**, read from the metadata DJI embeds
  in the MP4 (falls back to GPS-computed speed when absent).
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
| macOS (Apple Silicon / Intel) | `SkyStamp-macos-*.dmg` | Drag to Applications. First time: right-click → *Open* (the app isn't notarized yet). |
| Linux | `SkyStamp-linux-x86_64.AppImage` | `chmod +x SkyStamp-*.AppImage` and run it. |

Or, with Python 3.10+:

```bash
pip install git+https://github.com/vld666/SkyStamp
skystamp            # desktop app
skystamp-cli VIDEO.MP4 --style cinematic -o out.mp4
```

## Command line

```
skystamp-cli DJI_0018_D.MP4                       # SRT found automatically → DJI_0018_D_skystamp.mp4
skystamp-cli in.MP4 --srt in.SRT -o out.mp4 --style minimal --position top-right
skystamp-cli in.MP4 --format overlay              # transparent .mov for editors
skystamp-cli in.MP4 --fields altitude,speed,map --units imperial --scale 1.3 --accent "#FF8800"
```

Run `skystamp-cli -h` for every option.

## Adding a style

Each style is one small file in `src/skystamp/styles/`. To add one:

1. Copy `minimal.py` to `mystyle.py`, change `id`, `name` and `description`.
2. Draw in `draw_dynamic(d, img, sample, t)` with Pillow. Helpers from `Style`:
   - `self.metrics(s)`: the enabled numbers (value + unit + label), already converted to the chosen units
   - `self.datetime_text(s)`, `self.camera_text(s)`, `self.gps_text(s)`
   - `self.font(size)` and `self.px(n)`: sizes in "1080p pixels", scaled to any resolution and the text-size setting
   - `self.text(...)`: text with an outline that stays readable on bright skies
   - `self.draw_map_static/dynamic(...)`: the flight path mini-map
3. Anything that never changes (panels, backgrounds) goes in `draw_static`, which is drawn only once.
4. Register it in `styles/__init__.py`. It then shows up in the app and the CLI.

## Development

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest
python -m skystamp                 # run the app
python packaging/build.py          # standalone app in dist/
```

Releases are built by GitHub Actions (`.github/workflows/release.yml`) when a `v*` tag is pushed.

## How it works

`telemetry.py` parses the SRT (one entry per video frame) and computes speed, heading and distances from GPS.
`video.py` draws each overlay frame with Pillow and streams it to ffmpeg, which composites it onto the
original video and encodes it. The data refreshes 10×/s by default, while the video keeps its full frame rate.

## Credits

- Inspired by [dji-telemetry](https://github.com/jetervaz/dji-telemetry) and [GPStitch](https://github.com/Romancha/GPStitch). No code was copied.
- Map data © [OpenStreetMap contributors](https://www.openstreetmap.org/copyright); satellite imagery © Esri, Maxar, Earthstar Geographics. Attribution is drawn on the map.
- Font: [Barlow](https://github.com/jpt/barlow) (SIL Open Font License, `assets/fonts/OFL.txt`).
- Video processing by [FFmpeg](https://ffmpeg.org), bundled via [imageio-ffmpeg](https://github.com/imageio/imageio-ffmpeg) (GPL build; distributed as a separate program).
