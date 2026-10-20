"""Background map tiles (OpenStreetMap / satellite) for the flight-path mini-map,
and elevation tiles for the 3D terrain view.

Only the handful of tiles around the flight are downloaded, once per video,
and cached on disk. Without internet the map simply has no background.
"""

from __future__ import annotations

import io
import math
import os
import ssl
import sys
import threading
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

from . import __version__

TILE = 256
EARTH_R = 6_378_137.0
WORLD = 2 * math.pi * EARTH_R  # web-mercator world width in metres


@dataclass(frozen=True)
class Provider:
    name: str
    url: str
    attribution: str
    max_zoom: int


PROVIDERS: dict[str, Provider] = {
    "osm": Provider("Street map (OpenStreetMap)", "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
                    "© OpenStreetMap", 19),
    "satellite": Provider("Satellite (Esri)",
                          "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
                          "Esri, Maxar, Earthstar", 19),
}

# Terrarium-encoded heights (AWS Terrain Tiles, open data, no key):
# metres = R * 256 + G + B / 256 - 32768
ELEVATION = Provider("Terrain (AWS Terrain Tiles)",
                     "https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png",
                     "Terrain: AWS, Mapzen", 15)
_SOURCES = {**PROVIDERS, "terrain": ELEVATION}

_USER_AGENT = f"SkyStamp/{__version__} (+https://github.com/Drone-Log/SkyStamp)"
_lock = threading.Lock()


def _ssl_context() -> ssl.SSLContext:
    # Bundled CA certificates: the system ones aren't visible to python.org /
    # PyInstaller builds on macOS
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except Exception:  # noqa: BLE001
        return ssl.create_default_context()


_SSL = _ssl_context()
_mem: dict[tuple, Image.Image | None] = {}


def mercator(lat: float, lon: float) -> tuple[float, float]:
    """lat/lon → web-mercator metres (x east, y north)."""
    lat = max(-85.05, min(85.05, lat))
    return (EARTH_R * math.radians(lon),
            EARTH_R * math.log(math.tan(math.pi / 4 + math.radians(lat) / 2)))


def cache_dir() -> Path:
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Caches"
    elif os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home()))
    else:
        base = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    return base / "SkyStamp" / "tiles"


def _tile(provider: str, z: int, x: int, y: int) -> Image.Image | None:
    key = (provider, z, x, y)
    with _lock:
        if key in _mem:
            return _mem[key]
    path = cache_dir() / provider / str(z) / str(x) / f"{y}.img"
    img = None
    try:
        if path.is_file():
            img = Image.open(path).convert("RGB")
        else:
            url = _SOURCES[provider].url.format(z=z, x=x, y=y)
            req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
            with urllib.request.urlopen(req, timeout=10, context=_SSL) as r:
                data = r.read()
            img = Image.open(io.BytesIO(data)).convert("RGB")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
    except Exception:  # noqa: BLE001 - offline / blocked: no background
        img = None
    with _lock:
        _mem[key] = img
    return img


def render_area(provider: str, x0: float, y0: float, x1: float, y1: float,
                width: int, height: int) -> Image.Image | None:
    """Map image for the mercator box (x0, y0)-(x1, y1) (y north-up), sized width×height px.

    Returns None if no tile could be loaded.
    """
    # Zoom whose tiles are close to the output sharpness (up to 1.4× upscale is
    # invisible on video and needs ~4× fewer tiles)
    got = _mosaic(provider, x0, y0, x1, y1, width, 1.4)
    if got is None:
        return None
    canvas, crop = got
    return canvas.resize((width, height), Image.LANCZOS, box=crop)


def elevation_area(x0: float, y0: float, x1: float, y1: float, size: int):
    """Terrain heights in metres for the mercator box, as a size×size float32
    numpy array (row 0 = north). None if offline."""
    import numpy as np

    got = _mosaic("terrain", x0, y0, x1, y1, size, 1.0)
    if got is None:
        return None
    canvas, crop = got
    rgb = np.asarray(canvas, dtype=np.float32)
    metres = rgb[..., 0] * 256 + rgb[..., 1] + rgb[..., 2] / 256 - 32768
    hole = metres < -1000  # a tile that failed to load (the canvas colour decodes to ~-22 km)
    if hole.any():
        metres[hole] = float(np.median(metres[~hole]))
    # Resample the decoded heights (resampling the encoded colours would mix bytes)
    img = Image.fromarray(metres.astype(np.float32))
    return np.asarray(img.resize((size, size), Image.BILINEAR, box=crop), dtype=np.float32)


def _mosaic(provider, x0, y0, x1, y1, width, upscale):
    """Tiles covering the mercator box pasted together, plus the box's pixel crop."""
    prov = _SOURCES[provider]
    span = max(x1 - x0, 1e-6)
    z = math.ceil(math.log2(width / span * WORLD / TILE / upscale))
    z = max(1, min(prov.max_zoom, z))
    n = 2 ** z
    scale = TILE * n / WORLD  # tile pixels per mercator metre

    def to_px(mx, my):
        return (mx + WORLD / 2) * scale, (WORLD / 2 - my) * scale

    px0, py0 = to_px(x0, y1)  # top-left
    px1, py1 = to_px(x1, y0)  # bottom-right
    tx0, ty0 = int(px0 // TILE), int(py0 // TILE)
    tx1, ty1 = int(px1 // TILE), int(py1 // TILE)
    if (tx1 - tx0 + 1) * (ty1 - ty0 + 1) > 64:  # safety: never bulk-download
        return None

    canvas = Image.new("RGB", ((tx1 - tx0 + 1) * TILE, (ty1 - ty0 + 1) * TILE), (40, 44, 52))
    got = False
    for tx in range(tx0, tx1 + 1):
        for ty in range(ty0, ty1 + 1):
            if not 0 <= ty < n:
                continue
            tile = _tile(provider, z, tx % n, ty)
            if tile is not None:
                canvas.paste(tile, ((tx - tx0) * TILE, (ty - ty0) * TILE))
                got = True
    if not got:
        return None
    return canvas, (px0 - tx0 * TILE, py0 - ty0 * TILE, px1 - tx0 * TILE, py1 - ty0 * TILE)
