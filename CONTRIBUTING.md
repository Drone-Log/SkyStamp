# Contributing to SkyStamp

Thanks for helping! Bug reports, new overlay styles, support for more DJI models and translations are all welcome.

## Reporting a problem

Open an [issue](https://github.com/Drone-Log/SkyStamp/issues) and include:

- your drone model and OS
- what you did and what happened (screenshots help)
- if it's about reading data: a few blocks from the `.SRT` file (blur or remove the GPS coordinates if you prefer)

Security problems: please report them privately (see [SECURITY.md](SECURITY.md)), not in a public issue.

## Running it locally

Python 3.10+ is required.

```bash
git clone https://github.com/<your-username>/SkyStamp && cd SkyStamp
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev]"

python -m skystamp            # desktop app
skystamp-cli -h               # command line
pytest                        # unit tests
python tests/smoke.py         # end-to-end: renders a tiny test video with every style
```

ffmpeg comes with the `imageio-ffmpeg` package, so nothing else needs installing.

## Making a change

1. Fork the repo and create a branch: `git checkout -b fix-srt-mini2`.
2. Keep the change focused. One fix or feature per pull request.
3. Add or update a test when you change parsing or calculations (`tests/`).
4. Run `pytest` and `python tests/smoke.py`.
5. Open a pull request against `main` and describe what changed and how you tested it.

CI runs the tests on Windows, macOS and Linux for every pull request. `main` only accepts changes through
pull requests with passing checks.

## Where things live

| Path | What |
|---|---|
| `src/skystamp/telemetry.py` | SRT parsing, speed/distance/heading calculations |
| `src/skystamp/djmd.py` | DJI metadata embedded in the MP4 (GPS status, drone-measured speed) |
| `src/skystamp/styles/` | Overlay styles, one file each |
| `src/skystamp/flightlog.py` | DJI Fly flight logs: satellites, battery (via dji-log-parser) |
| `src/skystamp/maptiles.py` | Street / satellite map tiles for the mini-map |
| `src/skystamp/video.py` | ffmpeg: probing, preview frames, rendering |
| `src/skystamp/gui.py` / `cli.py` | Desktop app / command line |
| `docs/` | Website ([getskystamp.app](https://getskystamp.app)) |

**Adding a style:** see "Adding a style" in the [README](README.md#adding-a-style).

**Supporting another drone:** the most useful thing is a short sample `.SRT` (and, if possible, a short clip)
from that model attached to an issue.

## Releases

Maintainers tag `vX.Y.Z` on `main`. GitHub Actions builds Windows, macOS (signed and notarized) and Linux
packages and publishes the release.

By contributing you agree that your contributions are licensed under the [MIT License](LICENSE).
