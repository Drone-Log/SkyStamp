"""Parse DJI SRT telemetry files and derive flight metrics.

DJI drones write one subtitle block per video frame. Newer models (Neo, Mini 3/4,
Air 3, Avata...) use a bracketed key/value format::

    [iso: 460] [shutter: 1/1600.0] [fnum: 2.2] [latitude: 44.378130]
    [longitude: 26.154778] [rel_alt: 33.600 abs_alt: 105.734] ...

Older models (Mini 2, Mavic Air 2...) use a compact line::

    F/2.8, SS 1000, ISO 100, EV 0, GPS (26.15, 44.37, 19), D 10.2m, H 5.5m, HS 2.3m/s, VS 0.0m/s

Both are handled. Speeds, distances and heading are computed from GPS.
"""

from __future__ import annotations

import bisect
import math
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

_TIMECODE = re.compile(r"(\d+):(\d+):(\d+)[,.](\d+)\s*-->\s*(\d+):(\d+):(\d+)[,.](\d+)")
_KEYVAL = re.compile(r"(\w+)\s*:\s*([-+]?\d+(?:\.\d+)?(?:/\d+(?:\.\d+)?)?)")
_DATETIME = re.compile(r"(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2})(?:[.,:](\d+))?")
_TAGS = re.compile(r"<[^>]+>")

# Legacy compact format (Mini 2, Air 2, ...)
_LEGACY_GPS = re.compile(r"GPS\s*\(\s*([-\d.]+)\s*,\s*([-\d.]+)\s*,\s*([-\d.]+)\s*\)")
_LEGACY = {
    "fnum": re.compile(r"F/([\d.]+)"),
    "shutter": re.compile(r"SS\s+([\d./]+)"),
    "iso": re.compile(r"ISO\s+(\d+)"),
    "ev": re.compile(r"EV\s+([-+\d./]+)"),
    "rel_alt": re.compile(r"\bH\s+([-\d.]+)\s*m"),
    "dist_home": re.compile(r"\bD\s+([-\d.]+)\s*m"),
    "hs": re.compile(r"\bHS\s+([-\d.]+)\s*m/s"),
    "vs": re.compile(r"\bVS\s+([-\d.]+)\s*m/s"),
}

EARTH_RADIUS_M = 6_371_000.0


@dataclass
class Sample:
    """Telemetry for one point in time (seconds from video start)."""

    t: float
    lat: float | None = None
    lon: float | None = None
    rel_alt: float | None = None
    abs_alt: float | None = None
    iso: float | None = None
    shutter: str | None = None
    fnum: float | None = None
    ev: float | None = None
    ct: float | None = None
    focal_len: float | None = None
    timestamp: datetime | None = None
    # Derived
    h_speed: float = 0.0  # m/s
    v_speed: float = 0.0  # m/s
    dist_home: float = 0.0  # m, straight line from first GPS fix
    dist_total: float = 0.0  # m, travelled so far
    heading: float | None = None  # degrees, 0 = north
    gps_fix: bool | None = None  # satellite fix, from the video metadata or GPS presence
    speed_source: str = "gps"  # "gps" (computed) or "drone" (measured, from video metadata)
    satellites: int | None = None  # from a DJI Fly flight log
    battery: float | None = None  # percent, from a DJI Fly flight log


@dataclass
class Telemetry:
    samples: list[Sample] = field(default_factory=list)
    _times: list[float] = field(default_factory=list, repr=False)

    @property
    def duration(self) -> float:
        return self.samples[-1].t if self.samples else 0.0

    @property
    def has_gps(self) -> bool:
        return any(s.lat is not None for s in self.samples)

    @property
    def max_alt(self) -> float:
        return max((s.rel_alt or 0.0 for s in self.samples), default=0.0)

    @property
    def max_speed(self) -> float:
        return max((s.h_speed for s in self.samples), default=0.0)

    @property
    def max_dist_home(self) -> float:
        return max((s.dist_home for s in self.samples), default=0.0)

    @property
    def total_distance(self) -> float:
        return self.samples[-1].dist_total if self.samples else 0.0

    def at(self, t: float) -> Sample:
        """Sample active at time ``t`` (last one that started at or before t)."""
        if not self._times:
            self._times = [s.t for s in self.samples]
        i = bisect.bisect_right(self._times, t) - 1
        return self.samples[max(0, min(i, len(self.samples) - 1))]

    def track(self) -> list[tuple[float, float]]:
        """(lat, lon) points, de-duplicated, for drawing the flight path."""
        out: list[tuple[float, float]] = []
        for s in self.samples:
            if s.lat is None or s.lon is None:
                continue
            p = (s.lat, s.lon)
            if not out or out[-1] != p:
                out.append(p)
        return out


def haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(a)))


def bearing(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    x = math.sin(dl) * math.cos(p2)
    y = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(x, y)) + 360) % 360


def _num(value: str) -> float | None:
    try:
        return float(value)
    except ValueError:
        return None


def _parse_block(t: float, text: str) -> Sample:
    s = Sample(t=t)
    text = _TAGS.sub(" ", text)

    m = _DATETIME.search(text)
    if m:
        frac = (m.group(3) or "0")[:6].ljust(6, "0")
        try:
            s.timestamp = datetime.fromisoformat(f"{m.group(1)} {m.group(2)}.{frac}")
        except ValueError:
            pass

    vals = {k.lower(): v for k, v in _KEYVAL.findall(text)}
    if "latitude" in vals or "lat" in vals:
        s.lat = _num(vals.get("latitude", vals.get("lat", "")))
        s.lon = _num(vals.get("longitude", vals.get("lon", vals.get("longtitude", ""))))
        s.rel_alt = _num(vals.get("rel_alt", vals.get("altitude", "")) or "")
        s.abs_alt = _num(vals.get("abs_alt", "") or "")
        s.iso = _num(vals.get("iso", "") or "")
        s.shutter = vals.get("shutter")
        s.fnum = _num(vals.get("fnum", "") or "")
        s.ev = _num(vals.get("ev", "") or "")
        s.ct = _num(vals.get("ct", "") or "")
        s.focal_len = _num(vals.get("focal_len", "") or "")
    else:
        g = _LEGACY_GPS.search(text)
        if g:
            s.lon, s.lat = _num(g.group(1)), _num(g.group(2))
        for key, rx in _LEGACY.items():
            m = rx.search(text)
            if not m:
                continue
            if key == "shutter":
                s.shutter = m.group(1)
            elif key in ("dist_home", "hs", "vs"):
                continue  # recomputed from GPS below
            else:
                setattr(s, key, _num(m.group(1)))

    # fnum is sometimes stored as e.g. 280 meaning f/2.8
    if s.fnum is not None and s.fnum > 50:
        s.fnum /= 100
    # (0, 0) means "no GPS fix"
    if s.lat is not None and s.lon is not None and abs(s.lat) < 1e-9 and abs(s.lon) < 1e-9:
        s.lat = s.lon = None
    s.gps_fix = s.lat is not None
    return s


def parse_srt_text(content: str) -> Telemetry:
    samples: list[Sample] = []
    for block in re.split(r"\r?\n\s*\r?\n", content.strip()):
        lines = block.strip().splitlines()
        for i, line in enumerate(lines):
            m = _TIMECODE.search(line)
            if m:
                h, mi, se, ms = (int(x) for x in m.groups()[:4])
                t = h * 3600 + mi * 60 + se + ms / (10 ** len(m.group(4)))
                samples.append(_parse_block(t, "\n".join(lines[i + 1 :])))
                break
    samples.sort(key=lambda s: s.t)
    _derive(samples)
    return Telemetry(samples=samples)


def parse_srt(path: str | Path) -> Telemetry:
    raw = Path(path).read_bytes()
    for enc in ("utf-8-sig", "utf-16", "latin-1"):
        try:
            return parse_srt_text(raw.decode(enc))
        except UnicodeDecodeError:
            continue
    raise ValueError(f"Cannot decode {path}")


def _derive(samples: list[Sample], window: float = 1.0) -> None:
    """Compute speeds, distances and heading.

    GPS updates slower than the frame rate (values repeat), so speeds use a
    centred time window instead of frame-to-frame deltas.
    """
    gps = [s for s in samples if s.lat is not None and s.lon is not None]
    if not gps:
        return
    home = gps[0]
    times = [s.t for s in gps]
    half = window / 2

    for s in samples:
        if s.lat is not None:
            s.dist_home = haversine(home.lat, home.lon, s.lat, s.lon)

    # Horizontal / vertical speed and heading over a centred window
    last_heading: float | None = None
    n = len(gps)
    for i, s in enumerate(gps):
        # Always span at least one neighbour on each side (sparse logs)
        ia = min(bisect.bisect_left(times, s.t - half), max(0, i - 1))
        ib = max(bisect.bisect_right(times, s.t + half) - 1, min(n - 1, i + 1))
        a, b = gps[ia], gps[ib]
        dt = b.t - a.t
        if dt > 0:
            d = haversine(a.lat, a.lon, b.lat, b.lon)
            s.h_speed = d / dt
            if a.rel_alt is not None and b.rel_alt is not None:
                s.v_speed = (b.rel_alt - a.rel_alt) / dt
            if d > 0.5:
                last_heading = bearing(a.lat, a.lon, b.lat, b.lon)
        s.heading = last_heading

    # Travelled distance, accumulated on ~0.5 s steps to ignore GPS jitter
    total, prev = 0.0, gps[0]
    for s in gps:
        if s.t - prev.t >= 0.5:
            total += haversine(prev.lat, prev.lon, s.lat, s.lon)
            prev = s
        s.dist_total = total

    # Fill samples without GPS from the previous one
    last: Sample | None = None
    for s in samples:
        if s.lat is not None:
            last = s
        elif last is not None:
            s.h_speed, s.v_speed = last.h_speed, last.v_speed
            s.dist_home, s.dist_total, s.heading = last.dist_home, last.dist_total, last.heading
