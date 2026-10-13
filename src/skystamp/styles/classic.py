from __future__ import annotations

from .. import units as U
from .base import Style


class ClassicStyle(Style):
    id = "classic"
    name = "Classic"
    description = "Flight data top-left, camera top-right, speed box at the bottom, GPS and time in the corners."

    LABELS = {"altitude": "ALT", "speed": "H.SPD", "vspeed": "V.SPD", "home": "DIST",
              "distance": "FLOWN", "heading": "HDG", "battery": "BAT"}

    def _box(self):
        w, h = self.px(130), self.px(120)
        cx = self.w // 2
        y1 = self.h - self.margin
        return (cx - w // 2, y1 - h, cx + w // 2, y1)

    def draw_static(self, d, img):
        if self.opt.has("speed"):
            d.rounded_rectangle(self._box(), radius=self.px(12), fill=(0, 0, 0, 130),
                                outline=(255, 255, 255, 200), width=self.px(2))

    def draw_dynamic(self, d, img, s, t):
        m = self.margin
        f = self.font(30, "SemiBold")
        line = self.px(40)

        # Top-left: flight data, one per line
        y = m
        for metric in self.metrics(s):
            # "57.2m" but "12.5 km/h" / "SE 135°"
            sep = " " if "/" in metric.unit or metric.key == "heading" or metric.unit == "mph" else ""
            text = f"{metric.value}{sep}{metric.unit}" if metric.key != "heading" \
                else f"{metric.unit} {metric.value}".strip()
            self.text(d, (m, y), f"{self.LABELS[metric.key]}: {text}", f)
            y += line

        # Top-right: camera settings, one per line
        if self.opt.has("camera"):
            lines = []
            if s.iso is not None:
                lines.append(f"ISO {s.iso:.0f}")
            if s.shutter:
                lines.append(f"{U.shutter(s.shutter)}s")
            if s.fnum is not None:
                lines.append(f"f/{s.fnum:g}")
            if s.ev is not None:
                lines.append(f"EV {s.ev:+g}" if s.ev else "EV 0")
            y = m
            for text in lines:
                self.text(d, (self.w - m, y), text, f, anchor="ra")
                y += line

        # Bottom-center: speed box
        if self.opt.has("speed"):
            x0, y0, x1, y1 = self._box()
            value, unit = U.speed(s.h_speed, self.opt.units)
            cx = (x0 + x1) // 2
            self.text(d, (cx, y0 + self.px(64)), f"{float(value):.0f}", self.font(56, "Bold"),
                      anchor="ms", stroke=False)
            self.text(d, (cx, y1 - self.px(16)), unit.replace("/", ""), self.font(24, "Medium"),
                      (255, 255, 255, 210), anchor="ms", stroke=False)

        # Bottom corners: GPS and time
        bf = self.font(26, "Medium")
        gx = m
        badge = self.gps_badge(d, m, self.h - m - self.px(26), s, 26)
        if badge:
            gx += badge + self.px(18)
        if self.opt.has("gps") and (s.lat is not None or not badge):
            if s.lat is not None and s.lon is not None:
                gps = (f"{abs(s.lat):.6f}{'N' if s.lat >= 0 else 'S'}  "
                       f"{abs(s.lon):.6f}{'E' if s.lon >= 0 else 'W'}")
            else:
                gps = "No GPS"
            self.text(d, (gx, self.h - m), gps, bf, anchor="ls")
        if self.opt.has("datetime") and s.timestamp is not None:
            self.text(d, (self.w - m, self.h - m), s.timestamp.strftime("%H:%M:%S"), bf, anchor="rs")
