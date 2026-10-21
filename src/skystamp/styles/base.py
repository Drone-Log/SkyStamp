"""Shared building blocks for overlay styles.

A style draws the telemetry for one moment onto a transparent RGBA image the
size of the video. Anything that never changes (panels, labels, the full
flight path) goes in ``draw_static`` and is drawn once; ``draw_dynamic`` runs
for every overlay frame on a copy of it.
"""

from __future__ import annotations

import bisect
import math
from dataclasses import dataclass, field
from functools import lru_cache
from importlib import resources

from PIL import Image, ImageDraw, ImageFont

from .. import maptiles
from .. import units as U
from ..telemetry import Sample, Telemetry

FIELDS: dict[str, str] = {
    "altitude": "Altitude",
    "speed": "Speed",
    "vspeed": "Vertical speed",
    "home": "Distance from home",
    "distance": "Distance flown",
    "heading": "Heading",
    "datetime": "Date & time",
    "camera": "Camera settings",
    "gps_status": "GPS signal",
    "battery": "Battery %",
    "gps": "GPS coordinates",
    "map": "Flight path map",
}
DEFAULT_FIELDS = ["altitude", "speed", "vspeed", "home", "datetime", "camera", "gps_status", "battery", "gps",
                  "map"]

MAP_VIEWS = {"2d": "2D (from above)", "3d": "3D (follows the drone)", "terrain": "3D with terrain"}

POSITIONS = ["bottom-left", "bottom-center", "bottom-right", "top-left", "top-center", "top-right"]


@dataclass
class OverlayOptions:
    units: str = "metric"  # or "imperial"
    fields: list[str] = field(default_factory=lambda: list(DEFAULT_FIELDS))
    scale: float = 1.0  # text size multiplier
    accent: str = "#38BDF8"
    text_color: str = "#FFFFFF"
    position: str = "bottom-left"  # used by styles that have a single block
    opacity: float = 1.0  # overall overlay opacity
    map_tiles: str = "osm"  # background of the mini-map: "osm", "satellite" or "none"
    map_view: str = "2d"  # "2d" (from above), "3d" (camera behind the drone, flat ground) or "terrain" (3D with relief)

    def has(self, key: str) -> bool:
        return key in self.fields


@lru_cache(maxsize=64)
def font(size: int, weight: str = "SemiBold") -> ImageFont.FreeTypeFont:
    ref = resources.files("skystamp") / "assets" / "fonts" / f"BarlowSemiCondensed-{weight}.ttf"
    with resources.as_file(ref) as path:
        return ImageFont.truetype(str(path), max(6, int(size)))


def hex_rgba(color: str, alpha: int = 255) -> tuple[int, int, int, int]:
    c = color.lstrip("#")
    if len(c) == 3:
        c = "".join(ch * 2 for ch in c)
    return int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16), alpha


@dataclass
class Metric:
    key: str
    label: str
    short: str
    value: str
    unit: str


class Style:
    id = "base"
    name = "Base"
    description = ""

    def __init__(self, width: int, height: int, telemetry: Telemetry, options: OverlayOptions):
        self.w, self.h = width, height
        self.tel = telemetry
        self.opt = options
        # 1 unit = 1px on a 1080p frame, so layouts look the same at any resolution
        self.u = min(width, height) / 1080 * options.scale
        self.margin = round(min(width, height) * 0.045)
        self.fg = hex_rgba(options.text_color)
        self.accent = hex_rgba(options.accent)
        self.shadow = (0, 0, 0, 170)
        self._static: Image.Image | None = None
        self._map = _TrackMap(telemetry, options.map_tiles if options.has("map") else "none", options.map_view)
        self.frame: Image.Image | None = None  # the overlay frame being drawn

    # -- public ---------------------------------------------------------
    def render(self, t: float) -> Image.Image:
        if self._static is None:
            self._static = Image.new("RGBA", (self.w, self.h), (0, 0, 0, 0))
            self.draw_static(ImageDraw.Draw(self._static), self._static)
        img = self._static.copy()
        self.frame = img
        self.draw_dynamic(ImageDraw.Draw(img), img, self.tel.at(t), t)
        if self.opt.opacity < 1.0:
            alpha = img.getchannel("A").point(lambda a: int(a * self.opt.opacity))
            img.putalpha(alpha)
        return img

    # -- for subclasses -------------------------------------------------
    def draw_static(self, d: ImageDraw.ImageDraw, img: Image.Image) -> None:
        pass

    def draw_dynamic(self, d: ImageDraw.ImageDraw, img: Image.Image, s: Sample, t: float) -> None:
        raise NotImplementedError

    # -- helpers --------------------------------------------------------
    def px(self, v: float) -> int:
        return max(1, round(v * self.u))

    def font(self, size: float, weight: str = "SemiBold") -> ImageFont.FreeTypeFont:
        return font(self.px(size), weight)

    def metrics(self, s: Sample) -> list[Metric]:
        """The numeric metrics enabled by the user, in display order."""
        u = self.opt.units
        out: list[Metric] = []
        if self.opt.has("altitude"):
            out.append(Metric("altitude", "ALTITUDE", "H", *U.altitude(s.rel_alt, u)))
        if self.opt.has("speed"):
            out.append(Metric("speed", "SPEED", "H.S", *U.speed(s.h_speed, u)))
        if self.opt.has("vspeed"):
            out.append(Metric("vspeed", "VERT. SPEED", "V.S", *U.vspeed(s.v_speed, u)))
        if self.opt.has("home"):
            out.append(Metric("home", "FROM HOME", "D", *U.distance(s.dist_home, u)))
        if self.opt.has("distance"):
            out.append(Metric("distance", "FLOWN", "DIST", *U.distance(s.dist_total, u)))
        if self.opt.has("heading"):
            out.append(Metric("heading", "HEADING", "HDG", *U.heading(s.heading)))
        if self.opt.has("battery") and s.battery is not None:
            out.append(Metric("battery", "BATTERY", "BAT", f"{s.battery:.0f}", "%"))
        return out

    def datetime_text(self, s: Sample) -> str | None:
        if not self.opt.has("datetime") or s.timestamp is None:
            return None
        return s.timestamp.strftime("%Y-%m-%d  %H:%M:%S")

    def camera_text(self, s: Sample) -> str | None:
        if not self.opt.has("camera"):
            return None
        parts = []
        if s.iso is not None:
            parts.append(f"ISO {s.iso:.0f}")
        if s.shutter:
            parts.append(U.shutter(s.shutter))
        if s.fnum is not None:
            parts.append(f"f/{s.fnum:g}")
        if s.ev is not None:
            parts.append(f"EV {s.ev:+g}" if s.ev else "EV 0")
        return "   ".join(parts) or None

    def gps_text(self, s: Sample) -> str | None:
        if not self.opt.has("gps"):
            return None
        if s.lat is None and self.opt.has("gps_status"):
            return None  # the red "NO GPS" badge already says it
        return U.coords(s.lat, s.lon)

    def gps_badge_width(self, s: Sample, size: float = 24) -> int:
        if not self.opt.has("gps_status"):
            return 0
        return self.px(size) + self.px(4) + self.text_width(self._badge_label(s), self.font(size * 0.9, "Bold"))

    @staticmethod
    def _badge_label(s: Sample) -> str:
        if s.gps_fix is False:
            return "NO GPS" if not s.satellites else f"NO GPS {s.satellites}"
        return f"GPS {s.satellites}" if s.satellites is not None else "GPS"

    def gps_badge(self, d, x: int, y: int, s: Sample, size: float = 24) -> int:
        """Satellite icon + "GPS", green with a fix, red without. (x, y) is the
        top-left corner; returns the width drawn (0 if the field is off)."""
        if not self.opt.has("gps_status"):
            return 0
        color = {True: (74, 222, 128, 255), False: (248, 113, 113, 255)}.get(s.gps_fix, (200, 200, 200, 255))
        h = self.px(size)
        cx, cy = x + h // 2, y + h // 2
        outline = self.shadow
        lw = max(1, h // 14)
        # Satellite: body, two solar panels, small dish
        b, pw, ph = h * 0.18, h * 0.26, h * 0.36
        for box in ((cx - b - pw - lw, cy - ph / 2, cx - b - lw, cy + ph / 2),
                    (cx + b + lw, cy - ph / 2, cx + b + pw + lw, cy + ph / 2)):
            d.rectangle(box, fill=color, outline=outline, width=max(1, lw // 2))
        d.line([(cx - b - lw, cy), (cx + b + lw, cy)], fill=color, width=lw)
        d.rectangle((cx - b, cy - b, cx + b, cy + b), fill=color, outline=outline, width=max(1, lw // 2))
        d.arc((cx - b, cy + b * 0.6, cx + b, cy + b * 2.6), 200, 340, fill=color, width=lw)
        f = self.font(size * 0.9, "Bold")
        label = self._badge_label(s)
        self.text(d, (x + h + self.px(4), cy), label, f, color, anchor="lm")
        return h + self.px(4) + self.text_width(label, f)

    def text(self, d, xy, text, fnt, fill=None, anchor="la", stroke=True):
        """Text with a thin dark outline so it stays readable on bright skies."""
        d.text(
            xy,
            text,
            font=fnt,
            fill=fill or self.fg,
            anchor=anchor,
            stroke_width=max(1, round(fnt.size / 16)) if stroke else 0,
            stroke_fill=self.shadow,
        )

    def text_width(self, text: str, fnt) -> int:
        return math.ceil(fnt.getlength(text))

    def draw_map_static(self, d, box, bg=True):
        self._map.draw_static(d, self._static, box, self, bg)

    def draw_map_dynamic(self, d, box, t):
        self._map.draw_dynamic(d, box, t, self)


class _TrackMap:
    """Flight path mini-map, optionally over street / satellite map tiles."""

    MIN_EXTENT_M = 30.0  # without a background map: zoom close on the path
    MIN_EXTENT_TILES_M = 250.0  # with one: show some surroundings

    def __init__(self, tel: Telemetry, tiles: str, view: str = "2d"):
        pts = [(s.t, s.lat, s.lon) for s in tel.samples if s.lat is not None and s.lon is not None]
        self.ok = bool(pts)
        self.tiles = tiles if tiles in maptiles.PROVIDERS else None
        self.scene = None
        if not self.ok:
            return
        if view in ("3d", "terrain"):
            from ..map3d import Scene3D

            self.scene = Scene3D([(s.t, s.lat, s.lon, s.rel_alt) for s in tel.samples
                                  if s.lat is not None and s.lon is not None], self.tiles, view == "terrain")
        self.times: list[float] = []
        self.xy: list[tuple[float, float]] = []  # web-mercator metres
        for t, lat, lon in pts:
            p = maptiles.mercator(lat, lon)
            if not self.xy or self.xy[-1] != p:
                self.times.append(t)
                self.xy.append(p)
        xs, ys = [p[0] for p in self.xy], [p[1] for p in self.xy]
        self.cx, self.cy = (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2
        # Mercator stretches distances by 1/cos(lat)
        stretch = 1 / math.cos(math.radians(pts[0][1]))
        min_extent = (self.MIN_EXTENT_TILES_M if self.tiles else self.MIN_EXTENT_M) * stretch
        self.extent = max(max(xs) - min(xs), max(ys) - min(ys), min_extent)

    def _scale(self, box) -> float:
        x0, y0, x1, y1 = box
        pad = (x1 - x0) * 0.14
        return (min(x1 - x0, y1 - y0) - 2 * pad) / self.extent  # px per mercator metre

    def _project(self, box):
        x0, y0, x1, y1 = box
        k = self._scale(box)
        mx, my = (x0 + x1) / 2, (y0 + y1) / 2
        return lambda p: (mx + (p[0] - self.cx) * k, my - (p[1] - self.cy) * k)

    def _background(self, img, box, style: Style) -> bool:
        x0, y0, x1, y1 = (round(v) for v in box)
        w, h = x1 - x0, y1 - y0
        k = self._scale(box)
        bg = maptiles.render_area(self.tiles, self.cx - w / 2 / k, self.cy - h / 2 / k,
                                  self.cx + w / 2 / k, self.cy + h / 2 / k, w, h)
        if bg is None:
            return False
        # Slightly darker so the path and text stay readable
        bg = Image.blend(bg, Image.new("RGB", bg.size, (0, 0, 0)), 0.18).convert("RGBA")
        mask = Image.new("L", (w, h), 0)
        ImageDraw.Draw(mask).rounded_rectangle((0, 0, w - 1, h - 1), radius=style.px(14), fill=255)
        img.paste(bg, (x0, y0), mask)
        return True

    def draw_static(self, d, img, box, style: Style, bg: bool):
        if not self.ok:
            return
        if self.scene:  # the 3D camera moves: everything is drawn per frame
            return
        has_tiles = bool(self.tiles) and self._background(img, box, style)
        if bg and not has_tiles:
            d.rounded_rectangle(box, radius=style.px(14), fill=(0, 0, 0, 125),
                                outline=(255, 255, 255, 110), width=style.px(1.5))
        elif has_tiles:
            d.rounded_rectangle(box, radius=style.px(14), outline=(255, 255, 255, 200),
                                width=style.px(2))
            credit = maptiles.PROVIDERS[self.tiles].attribution
            style.text(d, (box[2] - style.px(8), box[3] - style.px(6)), credit,
                       style.font(11, "Medium"), (255, 255, 255, 230), anchor="rs")
        f = self._project(box)
        pts = [f(p) for p in self.xy]
        if len(pts) > 1:
            d.line(pts, fill=(255, 255, 255, 170 if has_tiles else 110), width=style.px(3), joint="curve")
        hx, hy = pts[0]
        r = style.px(6)
        d.rectangle((hx - r, hy - r, hx + r, hy + r), outline=(255, 255, 255, 230), width=style.px(2))
        style.text(d, (hx + r + style.px(4), hy), "H", style.font(16, "Bold"), anchor="lm")

    def draw_dynamic(self, d, box, t, style: Style):
        if not self.ok:
            return
        if self.scene:
            self.scene.draw(style.frame, d, box, t, style)
            return
        f = self._project(box)
        n = max(1, bisect.bisect_right(self.times, t))
        pts = [f(p) for p in self.xy[:n]]
        if len(pts) > 1:
            d.line(pts, fill=style.accent, width=style.px(4), joint="curve")
        x, y = pts[-1]
        r = style.px(8)
        d.ellipse((x - r - style.px(2), y - r - style.px(2), x + r + style.px(2), y + r + style.px(2)),
                  fill=(0, 0, 0, 160))
        d.ellipse((x - r, y - r, x + r, y + r), fill=style.accent, outline=(255, 255, 255, 255),
                  width=style.px(2))
