"""DJI Fly flight logs (``FlightRecord_*.txt`` / ``DJIFlightRecord_*.txt``).

These logs, kept by the DJI Fly app on the phone or remote, have what neither
the SRT nor the MP4 carry: GPS satellite count, battery level, the real home
point. Since log version 13 they are encrypted. Decrypting needs a (free) DJI
developer API key that each user creates for themselves, plus the open-source
``dji-log`` tool (https://github.com/lvauvillier/dji-log-parser, MIT), which
asks DJI's keychain API for the decryption keys.

SkyStamp bundles ``dji-log`` in its release builds; when running from source it
is downloaded once (pinned version, SHA-256 checked) into the cache folder.
"""

from __future__ import annotations

import bisect
import hashlib
import io
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import urllib.request
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .maptiles import _SSL, _USER_AGENT, cache_dir

PARSER_VERSION = "0.5.7"
_RELEASE = f"https://github.com/lvauvillier/dji-log-parser/releases/download/v{PARSER_VERSION}/"
# (archive name, sha256) per platform
_ASSETS = {
    ("darwin", "arm64"): ("dji-log-aarch64-apple-darwin.tar.gz",
                          "b8901777033b2f24e0ace4d822ba8691a69f7546ec0d043b53c80720b18d5ea7"),
    ("darwin", "x86_64"): ("dji-log-x86_64-apple-darwin.tar.gz",
                           "a56637c0ae63efbbc3a7382bf9998c9fd928796eaee03cb9b4cc1e680d9f0bb7"),
    ("linux", "x86_64"): ("dji-log-x86_64-unknown-linux-musl.tar.gz",
                          "6ce9a1d63f28850a4c81a89c846de58c691f1bf40cb9a761ac8dae90b6992301"),
    ("linux", "aarch64"): ("dji-log-aarch64-unknown-linux-musl.tar.gz",
                           "d840f66e86e1b186d9115e45377edc45f1a14dd968f08788f699766ae1bfd48a"),
    ("win32", "amd64"): ("dji-log-x86_64-pc-windows-msvc.zip",
                         "c036d9314bdfa72a9c13e93b956cca89747637c4d5c2a0cabed64a7544242dfd"),
}
_EXE = "dji-log.exe" if os.name == "nt" else "dji-log"
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_LOG_NAME = re.compile(r"(?:DJI)?FlightRecord_(\d{4}-\d{2}-\d{2})_\[(\d{2})-(\d{2})-(\d{2})\]\.txt$", re.I)

API_KEY_HELP = "https://developer.dji.com/user/apps/#all"


class FlightLogError(Exception):
    """Something the user can act on (wrong key, no internet, unsupported log)."""


@dataclass
class LogFrame:
    utc: datetime
    satellites: int | None
    gps_level: int | None
    gps_used: bool | None
    battery: float | None  # percent
    height: float | None  # m above home
    lat: float | None
    lon: float | None


@dataclass
class FlightLog:
    path: Path
    aircraft: str
    frames: list[LogFrame] = field(default_factory=list)
    home: tuple[float, float] | None = None
    _times: list[datetime] = field(default_factory=list, repr=False)

    @property
    def start(self) -> datetime:
        return self.frames[0].utc

    @property
    def end(self) -> datetime:
        return self.frames[-1].utc

    def at(self, utc: datetime) -> LogFrame | None:
        """Nearest frame to ``utc`` (None if more than 2 s away)."""
        if not self._times:
            self._times = [f.utc for f in self.frames]
        i = bisect.bisect_left(self._times, utc)
        best = None
        for j in (i - 1, i):
            if 0 <= j < len(self.frames):
                if best is None or abs(self.frames[j].utc - utc) < abs(best.utc - utc):
                    best = self.frames[j]
        if best is None or abs(best.utc - utc) > timedelta(seconds=2):
            return None
        return best


# --- parser binary ---------------------------------------------------------

def _platform_key() -> tuple[str, str]:
    machine = platform.machine().lower()
    machine = {"aarch64": "aarch64", "arm64": "arm64" if sys.platform == "darwin" else "aarch64",
               "x86_64": "x86_64", "amd64": "amd64" if os.name == "nt" else "x86_64"}.get(machine, machine)
    return sys.platform if sys.platform != "linux2" else "linux", machine


def parser_path(download: bool = True) -> Path:
    """The dji-log binary: bundled, $SKYSTAMP_DJI_LOG, PATH, or cached download."""
    candidates = []
    if getattr(sys, "frozen", False):
        base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
        candidates += [base / "dji-log" / _EXE, base / _EXE]
    if os.environ.get("SKYSTAMP_DJI_LOG"):
        candidates.append(Path(os.environ["SKYSTAMP_DJI_LOG"]))
    cached = cache_dir().parent / "bin" / PARSER_VERSION / _EXE
    candidates.append(cached)
    for c in candidates:
        if c.is_file():
            return c
    found = shutil.which("dji-log")
    if found:
        return Path(found)
    if not download:
        raise FlightLogError("The flight-log reader (dji-log) is not installed.")
    return _download(cached)


def _download(target: Path) -> Path:
    asset = _ASSETS.get(_platform_key())
    if not asset:
        raise FlightLogError(f"Reading flight logs isn't supported on this system ({_platform_key()}).")
    name, sha = asset
    try:
        req = urllib.request.Request(_RELEASE + name, headers={"User-Agent": _USER_AGENT})
        with urllib.request.urlopen(req, timeout=60, context=_SSL) as r:
            data = r.read()
    except Exception as e:  # noqa: BLE001
        raise FlightLogError(f"Couldn't download the flight-log reader: {e}") from e
    if hashlib.sha256(data).hexdigest() != sha:
        raise FlightLogError("Downloaded flight-log reader failed its integrity check.")
    target.parent.mkdir(parents=True, exist_ok=True)
    if name.endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            target.write_bytes(z.read(_EXE))
    else:
        with tarfile.open(fileobj=io.BytesIO(data)) as t:
            member = next(m for m in t.getmembers() if Path(m.name).name == _EXE)
            target.write_bytes(t.extractfile(member).read())
    target.chmod(0o755)
    return target


# --- API key -----------------------------------------------------------------

def api_key_from_env() -> str | None:
    """$DJI_API_KEY, or the file ~/.config/dji/api_key."""
    key = os.environ.get("DJI_API_KEY", "").strip()
    if key:
        return key
    f = Path.home() / ".config" / "dji" / "api_key"
    try:
        return f.read_text().strip() or None
    except OSError:
        return None


# --- parsing -----------------------------------------------------------------

def _json_cache(path: Path) -> Path:
    digest = hashlib.sha256(path.read_bytes()).hexdigest()[:24]
    return cache_dir().parent / "flightlogs" / f"{digest}-{PARSER_VERSION}.json"


def load(path: str | Path, api_key: str | None) -> FlightLog:
    """Decrypt (if needed) and read a DJI Fly flight log.

    The decoded result is cached, so DJI's key service is asked only once per log.
    """
    path = Path(path)
    cache = _json_cache(path)
    if cache.is_file():
        raw = json.loads(cache.read_text())
    else:
        raw = _run_parser(path, api_key)
        cache.parent.mkdir(parents=True, exist_ok=True)
        # Only what we use: serial numbers etc. never touch the disk
        slim = {"details": {"aircraftName": raw.get("details", {}).get("aircraftName", "")},
                "frames": [_slim(f) for f in raw.get("frames", [])]}
        cache.write_text(json.dumps(slim))
        raw = slim
    return _to_log(path, raw)


def _run_parser(path: Path, api_key: str | None) -> dict:
    exe = parser_path()
    args = [str(exe), str(path)]
    if api_key:
        args[1:1] = ["--api-key", api_key]
    res = subprocess.run(args, capture_output=True, stdin=subprocess.DEVNULL, creationflags=_NO_WINDOW,
                         timeout=180)
    if res.returncode != 0 or not res.stdout.strip():
        msg = res.stderr.decode(errors="replace").strip()
        low = msg.lower()
        if not api_key and ("api" in low or "key" in low or "keychain" in low):
            raise FlightLogError("This flight log is encrypted. Add your DJI API key to read it.")
        if "api" in low or "key" in low or "keychain" in low or "401" in low or "403" in low:
            raise FlightLogError("DJI rejected the API key, or its key service couldn't be reached. "
                                 "Check the key and your internet connection.")
        raise FlightLogError(f"Couldn't read the flight log: {msg[:300] or 'unknown error'}")
    try:
        return json.loads(res.stdout)
    except json.JSONDecodeError as e:
        raise FlightLogError("The flight-log reader returned unexpected output.") from e


def _slim(f: dict) -> dict:
    osd, bat, home = f.get("osd", {}), f.get("battery", {}), f.get("home", {})
    return {
        "t": f.get("custom", {}).get("dateTime"),
        "sats": osd.get("gpsNum"), "lvl": osd.get("gpsLevel"),
        "used": osd.get("isGpdUsed", osd.get("isGPSUsed")),
        "bat": bat.get("chargeLevel"), "h": osd.get("height"),
        "lat": osd.get("latitude"), "lon": osd.get("longitude"),
        "hlat": home.get("latitude") if home.get("isHomeRecord") else None,
        "hlon": home.get("longitude") if home.get("isHomeRecord") else None,
    }


def _parse_utc(value: str | None) -> datetime | None:
    if not value or value.startswith("1970"):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def _to_log(path: Path, raw: dict) -> FlightLog:
    log = FlightLog(path=path, aircraft=raw.get("details", {}).get("aircraftName", ""))
    for f in raw.get("frames", []):
        utc = _parse_utc(f.get("t"))
        if utc is None:
            continue
        lat, lon = f.get("lat"), f.get("lon")
        if lat is not None and lon is not None and abs(lat) < 1e-9 and abs(lon) < 1e-9:
            lat = lon = None
        log.frames.append(LogFrame(utc=utc, satellites=f.get("sats"), gps_level=f.get("lvl"),
                                   gps_used=f.get("used"), battery=f.get("bat"), height=f.get("h"),
                                   lat=lat, lon=lon))
        if f.get("hlat") and f.get("hlon"):
            log.home = (f["hlat"], f["hlon"])
    log.frames.sort(key=lambda x: x.utc)
    if not log.frames:
        raise FlightLogError("No timed data in this flight log.")
    return log


# --- finding & matching --------------------------------------------------------

def log_start_from_name(path: Path) -> datetime | None:
    """Local start time encoded in the file name, e.g. FlightRecord_2026-10-10_[16-42-38].txt."""
    m = _LOG_NAME.search(path.name)
    if not m:
        return None
    return datetime.fromisoformat(f"{m.group(1)} {m.group(2)}:{m.group(3)}:{m.group(4)}")


def find_log(video_start_local: datetime, folders: list[Path], max_flight: timedelta = timedelta(hours=1)
             ) -> Path | None:
    """The flight log whose (local) start is the latest one before the video started."""
    best: tuple[datetime, Path] | None = None
    for folder in folders:
        if not folder.is_dir():
            continue
        for p in folder.rglob("*.txt"):
            start = log_start_from_name(p)
            if start is None or not (start - timedelta(minutes=2) <= video_start_local <= start + max_flight):
                continue
            if best is None or start > best[0]:
                best = (start, p)
    return best[1] if best else None


def utc_offset(tel, log: FlightLog) -> timedelta | None:
    """Offset between the SRT's local clock and UTC, found by matching altitudes.

    Time zones are whole quarter hours, so try each one that puts the clip inside
    the logged flight and keep the one whose altitudes agree best.
    """
    samples = [s for s in tel.samples if s.timestamp is not None and s.rel_alt is not None]
    if not samples:
        return None
    probe = samples[:: max(1, len(samples) // 60)]
    best: tuple[float, timedelta] | None = None
    for q in range(-12 * 4, 14 * 4 + 1):
        off = timedelta(minutes=15 * q)
        err, n = 0.0, 0
        for s in probe:
            f = log.at(s.timestamp.replace(tzinfo=timezone.utc) - off)
            if f is not None and f.height is not None:
                err += abs(f.height - s.rel_alt)
                n += 1
        if n >= len(probe) * 0.6:
            score = err / n
            if best is None or score < best[0]:
                best = (score, off)
    return best[1] if best and best[0] < 5.0 else None


def attach(tel, log: FlightLog) -> bool:
    """Add satellites, GPS level and battery from the log to the SRT samples."""
    off = utc_offset(tel, log)
    if off is None:
        return False
    hit = False
    for s in tel.samples:
        if s.timestamp is None:
            continue
        f = log.at(s.timestamp.replace(tzinfo=timezone.utc) - off)
        if f is None:
            continue
        s.satellites = f.satellites
        s.battery = f.battery
        if f.gps_used is not None and s.gps_fix is None:
            s.gps_fix = f.gps_used
        hit = True
    return hit
