"""Command line: skystamp-cli VIDEO [--srt FILE] -o OUTPUT [--style hud] ..."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .styles import FIELDS, MAP_VIEWS, POSITIONS, STYLES, OverlayOptions
from . import flightlog
from .djmd import attach_video_meta
from .telemetry import parse_srt
from .video import FORMATS, render, resolve_style


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="skystamp-cli", description="Burn DJI SRT telemetry into a video.")
    p.add_argument("video", type=Path)
    p.add_argument("--srt", type=Path, help="SRT file (default: next to the video)")
    p.add_argument("-o", "--output", type=Path, help="output file (default: <video>_skystamp.mp4)")
    p.add_argument("--style", default="hud",
                   help=f"built-in ({', '.join(STYLES)}), one of your saved styles (user:<name>) or a .skystyle file")
    p.add_argument("--format", choices=list(FORMATS), default="h264")
    p.add_argument("--quality", choices=["high", "medium", "low"], default="high")
    p.add_argument("--no-hw", action="store_true", help="don't use the hardware encoder")
    p.add_argument("--units", choices=["metric", "imperial"], default="metric")
    p.add_argument("--fields", help=f"comma-separated, from: {','.join(FIELDS)}")
    p.add_argument("--position", choices=POSITIONS, default="bottom-left")
    p.add_argument("--scale", type=float, default=1.0, help="text size multiplier")
    p.add_argument("--accent", default="#38BDF8")
    p.add_argument("--map", choices=["osm", "satellite", "none"], default="osm",
                   help="mini-map background (downloaded once, cached)")
    p.add_argument("--map-view", choices=list(MAP_VIEWS), default="2d",
                   help="2d (from above), 3d (camera behind the drone) or terrain (3D with relief, heights downloaded once)")
    p.add_argument("--rate", type=float, default=10, help="data refreshes per second")
    p.add_argument("--offset", type=float, default=0.0, help="shift telemetry by N seconds")
    p.add_argument("--flight-log", type=Path,
                   help="DJI Fly FlightRecord_*.txt: adds satellite count and battery (default: found by time)")
    p.add_argument("--dji-key", help="DJI API key for encrypted logs (default: $DJI_API_KEY or ~/.config/dji/api_key)")
    a = p.parse_args(argv)

    srt = a.srt or next((a.video.with_suffix(e) for e in (".SRT", ".srt") if a.video.with_suffix(e).exists()), None)
    if not srt:
        p.error("no SRT next to the video; pass --srt")
    out = a.output or a.video.with_name(a.video.stem + ("_skystamp.mov" if a.format == "overlay" else "_skystamp.mp4"))
    opts = OverlayOptions(units=a.units, scale=a.scale, accent=a.accent, position=a.position,
                          map_tiles=a.map, map_view=a.map_view)
    if a.fields:
        opts.fields = [f.strip() for f in a.fields.split(",") if f.strip() in FIELDS]

    def progress(frac: float, msg: str) -> None:
        bar = "#" * int(frac * 30)
        print(f"\r[{bar:<30}] {frac * 100:5.1f}%  {msg:<40}", end="", file=sys.stderr, flush=True)

    tel = parse_srt(srt)
    attach_video_meta(tel, a.video)
    log_path = a.flight_log
    if log_path is None:
        start = next((s.timestamp for s in tel.samples if s.timestamp), None)
        if start:
            log_path = flightlog.find_log(start, [a.video.parent, a.video.parent.parent])
    if log_path:
        try:
            log = flightlog.load(log_path, a.dji_key or flightlog.api_key_from_env())
            matched = flightlog.attach(tel, log)
            print(f"Flight log {log_path.name}: {'matched' if matched else 'does not match this video'}",
                  file=sys.stderr)
        except flightlog.FlightLogError as e:
            print(f"Flight log skipped: {e}", file=sys.stderr)
    try:
        style = resolve_style(a.style)
    except ValueError as e:
        p.error(str(e))
    render(a.video, tel, out, style, opts, a.format, a.quality, not a.no_hw,
           a.rate, a.offset, progress)
    print(f"\nSaved {out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
