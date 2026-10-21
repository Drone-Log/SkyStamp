"""Styles described as data: a list of elements placed on the frame.

A style file (``.skystyle``, JSON) looks like::

    {
      "format": 1,
      "name": "My style",
      "description": "…",
      "corners": false,                      # viewfinder brackets in the corners
      "elements": [
        {"type": "data", "x": 0, "y": 1, "anchor": "bl", "size": 48,
         "fields": ["altitude", "speed"], "direction": "row", "labels": "above"},
        {"type": "info", "x": 0, "y": 0, "anchor": "tl", "size": 26, "items": ["datetime", "gps"]},
        {"type": "map", "x": 1, "y": 1, "anchor": "br", "size": 280},
        {"type": "speedbox", "x": 0.5, "y": 1, "anchor": "b", "size": 56}
      ]
    }

Positions are fractions of the *safe area* (the frame minus a small margin), so
``x: 0, y: 1`` is the bottom-left corner on any resolution or orientation.
``anchor`` says which point of the element sits at that position. Sizes are
pixels on a 1080p frame and scale with the video and the text-size setting.
Every value gets the width of its widest reading in the whole flight, so
nothing shifts as the numbers change, and an element too big for the safe
area is scaled down to fit.

Element types
-------------
data      numbers: ``fields`` (altitude, speed, vspeed, home, distance, heading,
          battery), ``direction`` row|column, ``labels`` above|below|prefix|short|none,
          ``panel`` none|dark|bar, optional ``header``/``footer`` info items and
          ``map: true`` (mini-map inside the panel)
info      text items: gps, datetime, date, time, camera, coords; ``direction``,
          ``separator``
speedbox  one big boxed number (``field``, default speed)
map       flight-path mini-map; ``size`` is its side, ``caption: "coords"``
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from typing import Callable

from PIL import ImageDraw

from .. import units as U
from .base import Metric, Style, hex_rgba

FORMAT = 1
ANCHORS = ["tl", "t", "tr", "l", "c", "r", "bl", "b", "br"]
DATA_FIELDS = ["altitude", "speed", "vspeed", "home", "distance", "heading", "battery"]
INFO_ITEMS = ["gps", "datetime", "date", "time", "camera", "coords"]
PREFIX_LABELS = {"altitude": "ALT", "speed": "H.SPD", "vspeed": "V.SPD", "home": "DIST", "distance": "FLOWN",
                 "heading": "HDG", "battery": "BAT"}

ELEMENT_DEFAULTS: dict[str, dict] = {
    "data": {"fields": ["altitude", "speed", "vspeed", "home"], "direction": "row", "labels": "above",
             "panel": "none", "size": 48, "header": [], "footer": [], "map": False, "separator": False},
    "info": {"items": ["datetime"], "direction": "row", "separator": "   ", "size": 24},
    "speedbox": {"field": "speed", "size": 56, "panel": "dark"},
    "map": {"size": 280, "caption": None},
}
TYPE_NAMES = {"data": "Flight data", "info": "Text / GPS", "speedbox": "Big number", "map": "Mini-map"}


def normalize(spec: dict) -> dict:
    """Fill in defaults and drop anything unknown, so old or hand-written files still load."""
    out = {"format": FORMAT, "name": str(spec.get("name") or "Untitled"),
           "description": str(spec.get("description") or ""), "corners": bool(spec.get("corners", False)),
           "elements": []}
    for el in spec.get("elements", []):
        t = el.get("type")
        if t not in ELEMENT_DEFAULTS:
            continue
        e = copy.deepcopy(ELEMENT_DEFAULTS[t])
        e.update({k: copy.deepcopy(v) for k, v in el.items() if k in ELEMENT_DEFAULTS[t] or k in
                  ("type", "x", "y", "anchor", "color", "position_option")})
        e["type"] = t
        e["x"] = min(1.0, max(0.0, float(e.get("x", 0.0))))
        e["y"] = min(1.0, max(0.0, float(e.get("y", 0.0))))
        e["anchor"] = e.get("anchor") if e.get("anchor") in ANCHORS else "tl"
        e["size"] = min(400.0, max(6.0, float(e.get("size", 24))))
        if t == "data":
            e["fields"] = [f for f in e["fields"] if f in DATA_FIELDS]
            e["header"] = [i for i in e["header"] if i in INFO_ITEMS]
            e["footer"] = [i for i in e["footer"] if i in INFO_ITEMS]
        if t == "info":
            e["items"] = [i for i in e["items"] if i in INFO_ITEMS]
        out["elements"].append(e)
    return out


@dataclass
class Block:
    """A measured element, ready to be drawn at a top-left position."""
    w: int
    h: int
    draw: Callable[[ImageDraw.ImageDraw, int, int], None]


class SpecStyle(Style):
    """Renders a style spec. After each render, ``boxes`` holds every element's
    rectangle (index -> (x0, y0, x1, y1)), used by the editor for hit-testing."""

    def __init__(self, width, height, telemetry, options, spec: dict):
        super().__init__(width, height, telemetry, options)
        self.spec = normalize(spec)
        self.elements = self.spec["elements"]
        self.boxes: dict[int, tuple[int, int, int, int]] = {}
        self._has_battery = any(s.battery is not None for s in telemetry.samples)
        self._widths: dict[tuple, int] = {}  # widest reading in the flight, per value and font
        self._fits: dict[str, dict] = {}  # element (as JSON) -> the element scaled to fit the frame

    # -- geometry ----------------------------------------------------------
    _POSITIONS = {"top-left": ("tl", 0, 0), "top-center": ("t", 0.5, 0), "top-right": ("tr", 1, 0),
                  "bottom-left": ("bl", 0, 1), "bottom-center": ("b", 0.5, 1), "bottom-right": ("br", 1, 1)}

    def _resolved(self, el: dict) -> tuple[str, float, float]:
        if el.get("position_option") and self.opt.position in self._POSITIONS:
            return self._POSITIONS[self.opt.position]  # Minimal follows the "Position" setting
        return el["anchor"], el["x"], el["y"]

    def _place(self, el: dict, w: int, h: int) -> tuple[int, int]:
        a, fx, fy = self._resolved(el)
        m = self.margin
        X = m + fx * (self.w - 2 * m)
        Y = m + fy * (self.h - 2 * m)
        ax = 0.0 if a in ("tl", "l", "bl") else 1.0 if a in ("tr", "r", "br") else 0.5
        ay = 0.0 if a in ("tl", "t", "tr") else 1.0 if a in ("bl", "b", "br") else 0.5
        return round(X - ax * w), round(Y - ay * h)

    def h_align(self, el: dict) -> str:
        a = self._resolved(el)[0]
        return "l" if a in ("tl", "l", "bl") else "r" if a in ("tr", "r", "br") else "c"

    # -- render --------------------------------------------------------------
    def draw_static(self, d, img):
        if self.spec["corners"]:
            m, L, w = self.margin // 2, self.px(40), self.px(3)
            col = (255, 255, 255, 140)
            for cx, cy, sx, sy in ((m, m, 1, 1), (self.w - m, m, -1, 1),
                                   (m, self.h - m, 1, -1), (self.w - m, self.h - m, -1, -1)):
                d.line([(cx, cy + sy * L), (cx, cy), (cx + sx * L, cy)], fill=col, width=w)
        for i, el in enumerate(self.elements):
            if el["type"] == "map" and self.opt.has("map") and self._map.ok:
                box = self._map_box(el)
                self.draw_map_static(d, box)

    def draw_dynamic(self, d, img, s, t):
        self.boxes = {}
        for i, el in enumerate(self.elements):
            if el["type"] == "map":
                cap = self._map_caption(el, s)
                if self.opt.has("map") and self._map.ok:
                    box = self._map_box(el)
                    self.draw_map_dynamic(d, box, t)
                    if cap:
                        cx, cy = self._caption_pos(el, box, cap)
                        cap.draw(d, cx, cy)
                        box = (min(box[0], cx), box[1], max(box[2], cx + cap.w), cy + cap.h)
                    self.boxes[i] = box
                elif cap:  # no map to show: the caption takes its place
                    x, y = self._place(el, cap.w, cap.h)
                    cap.draw(d, x, y)
                    self.boxes[i] = (x, y, x + cap.w, y + cap.h)
                continue
            block = self._block(self._fitted(el), s, t)
            if block is None or block.w <= 0:
                continue
            x, y = self._place(el, block.w, block.h)
            block.draw(d, x, y)
            self.boxes[i] = (x, y, x + block.w, y + block.h)

    # -- fixed widths ------------------------------------------------------------
    def _widest(self, key: tuple, reading: Callable) -> int:
        """Widest ``reading(sample)`` in the flight, computed once. A reading is
        a tuple of (text, font) pieces set side by side, or None.

        Measuring text is slow at 4K, so every distinct reading is first
        estimated from its characters' widths and only the widest few are
        measured exactly.
        """
        if key not in self._widths:
            samples = self.tel.samples
            step = max(1, len(samples) // 3000)  # DJI writes up to 60 a second; the widths change slowly
            readings = {r for s in samples[::step] if (r := reading(s))}
            chars: dict[tuple, float] = {}

            def char_w(c, f):
                if (c, id(f)) not in chars:
                    chars[c, id(f)] = f.getlength(c)
                return chars[c, id(f)]

            def rough(r):
                return sum(char_w(c, f) for txt, f in r for c in txt)

            widest = sorted(readings, key=rough)[-5:]
            self._widths[key] = max((sum(self.text_width(txt, f) for txt, f in r) for r in widest), default=0)
        return self._widths[key]

    def _fitted(self, el: dict) -> dict:
        """``el``, scaled down once if its widest frame would leave the safe area."""
        key = json.dumps(el, sort_keys=True)
        if key not in self._fits:
            out = el
            samples = self.tel.samples
            probe = [samples[i * (len(samples) - 1) // 10] for i in range(11)] if samples else []
            sizes = [b for b in (self._block(el, s, s.t) for s in probe) if b is not None]
            if sizes:
                w, h = max(b.w for b in sizes), max(b.h for b in sizes)
                k = min((self.w - 2 * self.margin) / max(w, 1), (self.h - 2 * self.margin) / max(h, 1))
                if k < 1:
                    out = dict(el, size=el["size"] * k * 0.98)
            self._fits[key] = out
        return self._fits[key]

    # -- elements ------------------------------------------------------------
    def _block(self, el, s, t) -> Block | None:
        if el["type"] == "data":
            return self._data(el, s, t)
        if el["type"] == "info":
            return self._info(el, el["items"], el["size"], s, el["direction"], el.get("separator", "   "))
        if el["type"] == "speedbox":
            return self._speedbox(el, s)
        return None

    def _metric(self, key: str, s) -> Metric | None:
        if not self.opt.has(key):
            return None
        u = self.opt.units
        if key == "altitude":
            return Metric(key, "ALTITUDE", "H", *U.altitude(s.rel_alt, u))
        if key == "speed":
            return Metric(key, "SPEED", "H.S", *U.speed(s.h_speed, u))
        if key == "vspeed":
            return Metric(key, "VERT. SPEED", "V.S", *U.vspeed(s.v_speed, u))
        if key == "home":
            return Metric(key, "FROM HOME", "D", *U.distance(s.dist_home, u))
        if key == "distance":
            return Metric(key, "FLOWN", "DIST", *U.distance(s.dist_total, u))
        if key == "heading":
            return Metric(key, "HEADING", "HDG", *U.heading(s.heading))
        if key == "battery" and s.battery is not None:
            return Metric(key, "BATTERY", "BAT", f"{s.battery:.0f}", "%")
        return None

    def _color(self, el):
        return hex_rgba(el["color"]) if el.get("color") else self.fg

    def _data(self, el, s, t) -> Block | None:
        size, mode, panel = el["size"], el["labels"], el["panel"]
        stroke = panel == "none"
        fg, dim, acc = self._color(el), (255, 255, 255, 205), self.accent
        vf, uf, lf = self.font(size, "Bold" if panel != "none" else "SemiBold"), self.font(size * 0.5, "Medium"), \
            self.font(size * 0.38, "SemiBold")
        cells: list[Block] = []
        for key in el["fields"]:
            m = self._metric(key, s)
            if m is None:
                continue
            cells.append(self._cell(m, mode, size, vf, uf, lf, fg, dim, acc, stroke))
        if not cells and not el["header"] and not el["footer"]:
            return None
        row = el["direction"] == "row"
        gap = self.px(size * (0.8 if mode in ("above", "below") else 0.55)) if row else self.px(size * 0.2)
        sep_f = self.font(size * 0.5, "Medium")
        sep_w = self.text_width("·", sep_f) + gap if (row and el.get("separator")) else 0
        if row:
            cw = sum(c.w for c in cells) + max(0, len(cells) - 1) * (gap + sep_w)
            ch = max((c.h for c in cells), default=0)
        else:
            cw = max((c.w for c in cells), default=0)
            ch = sum(c.h for c in cells) + max(0, len(cells) - 1) * gap
        map_side = ch if (el.get("map") and self.opt.has("map") and self._map.ok and ch) else 0
        map_gap = self.px(20) if map_side else 0
        pad = self.px(size * 0.5) if panel != "none" else 0
        bar = self.px(8) if panel == "bar" else 0
        inner_w = cw + map_side + map_gap
        body_w, body_h = inner_w + 2 * pad + bar * 3, ch + 2 * pad

        head = self._info(el, el["header"], max(14.0, size * 0.4), s, "row", "   |   ") if el["header"] else None
        foot = self._info(el, el["footer"], max(14.0, size * 0.58), s, "row", "    ") if el["footer"] else None
        vgap = self.px(size * 0.25)
        total_w = max(body_w, head.w if head else 0, foot.w if foot else 0)
        total_h = body_h + (head.h + vgap if head else 0) + (foot.h + vgap if foot else 0)
        align = self.h_align(el)

        def ax(w, x):
            return x if align == "l" else x + (total_w - w) // 2 if align == "c" else x + total_w - w

        def draw(d, x, y):
            cy = y
            if head:
                head.draw(d, ax(head.w, x), cy)
                cy += head.h + vgap
            bx = ax(body_w, x)
            if panel != "none":
                d.rounded_rectangle((bx, cy, bx + body_w, cy + body_h), radius=self.px(18), fill=(10, 14, 20, 175))
            if bar:
                d.rounded_rectangle((bx, cy, bx + bar, cy + body_h), radius=self.px(4), fill=self.accent)
            px_, py_ = bx + pad + bar * 3, cy + pad
            for i, c in enumerate(cells):
                if row:
                    if i and sep_w:
                        self.text(d, (px_ + gap // 2, py_ + c.h - self.px(size * 0.18)), "·", sep_f, dim,
                                  anchor="ls", stroke=stroke)
                        px_ += sep_w
                    c.draw(d, px_, py_ + (ch - c.h))
                    px_ += c.w + gap
                else:
                    cx_ = px_ if align != "r" else px_ + cw - c.w
                    c.draw(d, cx_, py_)
                    py_ += c.h + gap
            if map_side:
                mx = bx + pad + bar * 3 + cw + map_gap
                box = (mx, cy + pad, mx + map_side, cy + pad + map_side)
                self.draw_map_static(d, box, bg=False)
                self.draw_map_dynamic(d, box, t)
            cy += body_h + vgap
            if foot:
                foot.draw(d, ax(foot.w, x), cy)

        return Block(total_w, total_h, draw)

    def _cell(self, m: Metric, mode, size, vf, uf, lf, fg, dim, acc, stroke) -> Block:
        if mode == "prefix":
            f = self.font(size, "SemiBold")

            def prefix_text(m):
                sep = " " if ("/" in m.unit or m.key == "heading" or m.unit == "mph") else ""
                body = f"{m.unit} {m.value}".strip() if m.key == "heading" else f"{m.value}{sep}{m.unit}"
                return f"{PREFIX_LABELS[m.key]}: {body}"

            text = prefix_text(m)
            w = self._widest(("prefix", m.key, f.path, f.size),
                             lambda s: ((prefix_text(r), f),) if (r := self._metric(m.key, s)) else None)
            w, h = max(w, self.text_width(text, f)), self.px(size * 1.3)
            return Block(w, h, lambda d, x, y: self.text(d, (x, y + h - self.px(size * 0.3)), text, f, fg,
                                                         anchor="ls", stroke=stroke))
        gap_u = self.px(size * 0.08)

        vw, uw = self.text_width(m.value, vf), self.text_width(m.unit, uf)
        widest = self._widest(("value", m.key, vf.path, vf.size, uf.size),
                              lambda s: ((r.value, vf), (r.unit, uf)) if (r := self._metric(m.key, s)) else None)
        val_w = max(vw + uw, widest) + (gap_u if m.unit else 0)
        val_h = self.px(size * 1.05)
        if mode == "short":
            sf = self.font(size * 0.62, "Medium")
            lw = self.text_width(m.short + " ", sf)
            w, h = lw + val_w, val_h

            def draw(d, x, y):
                base = y + h - self.px(size * 0.18)
                self.text(d, (x, base), m.short + " ", sf, dim, anchor="ls", stroke=stroke)
                self._value(d, x + lw, base, m, vf, uf, vw, gap_u, fg, dim, stroke)
            return Block(w, h, draw)
        if mode in ("above", "below"):
            label = m.short if mode == "above" else m.label
            lw, lh = self.text_width(label, lf), self.px(size * 0.5)
            w, h = max(val_w, lw), val_h + lh

            def draw(d, x, y):
                if mode == "above":
                    self.text(d, (x, y + lh - self.px(size * 0.1)), label, lf, acc, anchor="ls", stroke=stroke)
                    self._value(d, x, y + h - self.px(size * 0.18), m, vf, uf, vw, gap_u, fg, dim, stroke)
                else:
                    self._value(d, x, y + val_h - self.px(size * 0.18), m, vf, uf, vw, gap_u, fg, dim, stroke)
                    self.text(d, (x, y + h - self.px(size * 0.02)), label, lf, acc, anchor="ls", stroke=stroke)
            return Block(w, h, draw)
        return Block(val_w, val_h, lambda d, x, y: self._value(d, x, y + val_h - self.px(size * 0.18), m, vf, uf,
                                                                vw, gap_u, fg, dim, stroke))

    def _value(self, d, x, base, m, vf, uf, vw, gap_u, fg, dim, stroke):
        self.text(d, (x, base), m.value, vf, fg, anchor="ls", stroke=stroke)
        if m.unit:
            self.text(d, (x + vw + gap_u, base), m.unit, uf, dim, anchor="ls", stroke=stroke)

    def _info_parts(self, items, s, column) -> list[tuple[str, str | tuple]]:
        """(item, part) pairs; in a column, the camera settings are one part per line."""
        parts: list = []
        for it in items:
            if it == "gps":
                if self.opt.has("gps_status"):
                    parts.append((it, ("gps",)))
            elif it == "datetime":
                if self.opt.has("datetime") and s.timestamp:
                    parts.append((it, s.timestamp.strftime("%Y-%m-%d  %H:%M:%S")))
            elif it == "date":
                if self.opt.has("datetime") and s.timestamp:
                    parts.append((it, s.timestamp.strftime("%Y-%m-%d")))
            elif it == "time":
                if self.opt.has("datetime") and s.timestamp:
                    parts.append((it, s.timestamp.strftime("%H:%M:%S")))
            elif it == "camera":
                cam = self.camera_parts(s)
                if cam:
                    parts.extend((it, c) for c in cam) if column else parts.append((it, "   ".join(cam)))
            elif it == "coords":
                txt = self.gps_text(s)
                if txt:
                    parts.append((it, txt))
        return parts

    def camera_parts(self, s) -> list[str]:
        if not self.opt.has("camera"):
            return []
        parts = []
        if s.iso is not None:
            parts.append(f"ISO {s.iso:.0f}")
        if s.shutter:
            parts.append(U.shutter(s.shutter) + "s" if "/" in (s.shutter or "") else U.shutter(s.shutter))
        if s.fnum is not None:
            parts.append(f"f/{s.fnum:g}")
        if s.ev is not None:
            parts.append(f"EV {s.ev:+g}" if s.ev else "EV 0")
        return parts

    def _info(self, el, items, size, s, direction, separator) -> Block | None:
        column = direction == "column"
        parts = self._info_parts(items, s, column)
        if not parts:
            return None
        f = self.font(size, "Medium")
        fg = self._color(el)
        line_h = self.px(size * 1.3)

        badge_f = self.font(size * 0.9, "Bold")
        badge_icon = self.px(size) + self.px(4)

        def part_w(p, smp):
            return self.gps_badge_width(smp, size) if isinstance(p, tuple) else self.text_width(p, f)

        def slot_w(item, n):  # the widest reading in the flight of the item's n-th part
            if item == "gps":
                return badge_icon + self._widest(("badge", badge_f.path, badge_f.size),
                                                 lambda smp: ((self._badge_label(smp), badge_f),))

            def reading(smp):
                got = self._info_parts([item], smp, column)
                return ((got[n][1], f),) if n < len(got) else None
            return self._widest(("info", item, n, column, f.path, f.size), reading)

        nth = [sum(1 for it2, _ in parts[:i] if it2 == it) for i, (it, _) in enumerate(parts)]
        slots = [max(slot_w(it, n), part_w(p, s)) for (it, p), n in zip(parts, nth)]
        parts = [p for _, p in parts]
        sep_w = self.text_width(separator, f)
        align = self.h_align(el)
        if column:
            w = max(slots)
            h = line_h * len(parts)
        else:
            w = sum(slots) + sep_w * (len(parts) - 1)
            h = line_h

        def draw(d, x, y):
            if column:
                for i, p in enumerate(parts):
                    pw = part_w(p, s)
                    px_ = x if align == "l" else x + (w - pw) // 2 if align == "c" else x + w - pw
                    self._part(d, px_, y + i * line_h, p, f, fg, size, s, line_h)
            else:
                px_ = x
                for i, p in enumerate(parts):
                    if i:
                        self.text(d, (px_, y + line_h // 2), separator, f, (255, 255, 255, 200), anchor="lm")
                        px_ += sep_w
                    self._part(d, px_, y, p, f, fg, size, s, line_h)
                    px_ += slots[i]
        return Block(w, h, draw)

    def _part(self, d, x, y, p, f, fg, size, s, line_h):
        if isinstance(p, tuple):
            self.gps_badge(d, x, y + (line_h - self.px(size)) // 2, s, size)
        else:
            self.text(d, (x, y + line_h // 2), p, f, fg, anchor="lm")

    def _speedbox(self, el, s) -> Block | None:
        key = el.get("field", "speed")
        m = self._metric(key, s)
        if m is None:
            return None
        size = el["size"]
        w, h = self.px(size * 2.3), self.px(size * 2.15)
        vf, uf = self.font(size, "Bold"), self.font(size * 0.43, "Medium")
        value = m.value
        if key == "speed":
            value = f"{float(m.value):.0f}"

        def draw(d, x, y):
            if el.get("panel", "dark") != "none":
                d.rounded_rectangle((x, y, x + w, y + h), radius=self.px(12), fill=(0, 0, 0, 130),
                                    outline=(255, 255, 255, 200), width=self.px(2))
            cx = x + w // 2
            self.text(d, (cx, y + self.px(size * 1.15)), value, vf, self._color(el), anchor="ms", stroke=False)
            self.text(d, (cx, y + h - self.px(size * 0.3)), m.unit.replace("/", ""), uf, (255, 255, 255, 210),
                      anchor="ms", stroke=False)
        return Block(w, h, draw)

    # -- map -------------------------------------------------------------------
    def _map_box(self, el):
        side = self.px(el["size"])
        cap_h = self.px(34) if el.get("caption") == "coords" and self.opt.has("gps") else 0
        x, y = self._place(el, side, side + cap_h)
        return (x, y, x + side, y + side)

    def _map_caption(self, el, s) -> Block | None:
        if el.get("caption") != "coords":
            return None
        return self._info(el, ["coords"], 20, s, "row", "")

    def _caption_pos(self, el, box, cap):
        align = self.h_align(el)
        x = box[0] if align == "l" else box[2] - cap.w if align == "r" else (box[0] + box[2] - cap.w) // 2
        return x, box[3] + self.px(6)
