"""Read the DJI metadata track ("djmd", protobuf) embedded in newer DJI MP4s.

It carries per-frame flight data the SRT file doesn't have. We use:

- GPS fix status (has the drone a satellite fix or not)
- velocity measured by the drone (more precise than differentiating GPS)

The schema isn't published; field numbers below were identified on DJI Neo 2
footage (``dvtm_NEO2.proto``) and are read defensively: anything missing or
unexpected just means "no extra data" and the SRT values are used.
"""

from __future__ import annotations

import bisect
import math
import re
import struct
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass
class MetaFrame:
    t: float  # seconds from the first frame
    gps_fix: bool | None = None
    vel_n: float | None = None  # m/s, north
    vel_e: float | None = None  # m/s, east
    vel_d: float | None = None  # m/s, down


def _varint(b: bytes, i: int) -> tuple[int, int]:
    r = s = 0
    while True:
        c = b[i]
        i += 1
        r |= (c & 0x7F) << s
        s += 7
        if not c & 0x80:
            return r, i


def _fields(b: bytes):
    """Yield (field, wire_type, value) for one protobuf message level."""
    i, n = 0, len(b)
    while i < n:
        key, i = _varint(b, i)
        f, wt = key >> 3, key & 7
        if wt == 0:
            v, i = _varint(b, i)
        elif wt == 1:
            v, i = b[i:i + 8], i + 8
        elif wt == 2:
            ln, i = _varint(b, i)
            v, i = b[i:i + ln], i + ln
        elif wt == 5:
            v, i = b[i:i + 4], i + 4
        else:
            raise ValueError(f"wire type {wt}")
        if i > n:
            raise ValueError("truncated")
        yield f, wt, v


def _sub(b: bytes, *path: int) -> bytes | None:
    for p in path:
        nxt = None
        for f, wt, v in _fields(b):
            if f == p and wt == 2:
                nxt = v
                break
        if nxt is None:
            return None
        b = nxt
    return b


def _parse_frame(frame: bytes) -> tuple[int | None, MetaFrame]:
    m = MetaFrame(t=0.0)
    ts = None
    drone = _sub(frame, 4)
    if drone is None:
        return None, m
    for f, wt, v in _fields(drone):
        if f == 1 and wt == 2:  # header: field 6 = timestamp (µs)
            for f2, wt2, v2 in _fields(v):
                if f2 == 6 and wt2 == 0:
                    ts = v2
        elif f == 2 and wt == 2:  # velocity, floats (proto3 omits zeros)
            vel = {1: 0.0, 2: 0.0, 3: 0.0}
            for f2, wt2, v2 in _fields(v):
                if wt2 == 5 and f2 in vel:
                    vel[f2] = struct.unpack("<f", v2)[0]
            m.vel_n, m.vel_e, m.vel_d = vel[1], vel[2], vel[3]
        elif f == 4 and wt == 2:  # GPS: field 4 = fix, field 3 = no fix
            status = {f2 for f2, wt2, _ in _fields(v) if wt2 == 0}
            if 4 in status:
                m.gps_fix = True
            elif 3 in status:
                m.gps_fix = False
    return ts, m


def parse_bytes(data: bytes) -> list[MetaFrame]:
    """Parse the concatenated packets of the djmd track.

    Each packet is a message with field 1 (small header) and field 3 (frame
    data), so concatenated packets can be walked at the top level.
    """
    frames: list[MetaFrame] = []
    ts0 = None
    try:
        for f, wt, v in _fields(data):
            if f != 3 or wt != 2:
                continue
            ts, m = _parse_frame(v)
            if ts is None:
                continue
            ts0 = ts if ts0 is None else ts0
            m.t = (ts - ts0) / 1_000_000
            frames.append(m)
    except (ValueError, IndexError, struct.error):
        pass  # keep what was parsed before the damage
    return frames


def _run(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, stdin=subprocess.DEVNULL,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def read_video_meta(video: str | Path) -> list[MetaFrame]:
    """Extract and parse the djmd track; [] if the video doesn't have one."""
    from .video import ffmpeg_path

    ffmpeg = ffmpeg_path()
    err = _run([ffmpeg, "-hide_banner", "-i", str(video)]).stderr.decode(errors="replace")
    m = re.search(r"Stream #0:(\d+)\S*: Data: \w+ \(djmd", err)
    if not m:
        return []
    res = _run([ffmpeg, "-v", "error", "-i", str(video), "-map", f"0:{m.group(1)}", "-c", "copy",
                "-f", "data", "-"])
    return parse_bytes(res.stdout)


def h_speed(m: MetaFrame) -> float | None:
    if m.vel_n is None or m.vel_e is None:
        return None
    return math.hypot(m.vel_n, m.vel_e)


def attach_video_meta(tel, video: str | Path) -> bool:
    """Enrich SRT telemetry with GPS fix status and drone-measured velocity.

    Returns True if the video had usable metadata.
    """
    try:
        frames = read_video_meta(video)
    except Exception:  # noqa: BLE001 - optional data, never fatal
        return False
    if not frames or not tel.samples:
        return False
    times = [f.t for f in frames]
    for s in tel.samples:
        i = bisect.bisect_right(times, s.t) - 1
        m = frames[max(0, i)]
        if abs(m.t - s.t) > 1.0:
            continue
        if m.gps_fix is not None:
            s.gps_fix = m.gps_fix
        hs = h_speed(m)
        if hs is not None:
            s.h_speed = hs if hs >= 0.15 else 0.0  # ignore hover noise
            s.v_speed = -m.vel_d if m.vel_d is not None else s.v_speed
            s.speed_source = "drone"
            if hs >= 1.0:
                s.heading = (math.degrees(math.atan2(m.vel_e, m.vel_n)) + 360) % 360
    return True
