from __future__ import annotations

from .base import Style


class CinematicStyle(Style):
    id = "cinematic"
    name = "Cinematic"
    description = "Lower-third panel with large numbers, like a broadcast graphic."

    def _layout(self):
        n = len([k for k in ("altitude", "speed", "vspeed", "home", "distance", "heading") if self.opt.has(k)])
        if self.opt.has("battery") and any(s.battery is not None for s in self.tel.samples):
            n += 1
        n = max(1, n)
        col_w = self.px(170)
        has_map = self.opt.has("map")
        map_w = self.px(150) if has_map else 0
        panel_h = self.px(150)
        panel_w = self.px(36) + n * col_w + (map_w + self.px(20) if has_map else 0)
        x0, y1 = self.margin, self.h - self.margin
        return {
            "panel": (x0, y1 - panel_h, x0 + panel_w, y1),
            "col_w": col_w,
            "map": (x0 + panel_w - map_w - self.px(14), y1 - panel_h + self.px(14),
                    x0 + panel_w - self.px(14), y1 - self.px(14)) if has_map else None,
        }

    def draw_static(self, d, img):
        lay = self._layout()
        x0, y0, x1, y1 = lay["panel"]
        d.rounded_rectangle(lay["panel"], radius=self.px(18), fill=(10, 14, 20, 175))
        d.rounded_rectangle((x0, y0, x0 + self.px(8), y1), radius=self.px(4), fill=self.accent)
        if lay["map"]:
            self.draw_map_static(d, lay["map"], bg=False)

    def draw_dynamic(self, d, img, s, t):
        lay = self._layout()
        x0, y0, x1, y1 = lay["panel"]
        value_f, unit_f, label_f = self.font(56, "Bold"), self.font(24, "Medium"), self.font(18, "SemiBold")
        dim = (255, 255, 255, 170)

        x = x0 + self.px(36)
        for metric in self.metrics(s):
            vy = y0 + self.px(92)
            self.text(d, (x, vy), metric.value, value_f, anchor="ls", stroke=False)
            vw = self.text_width(metric.value, value_f)
            self.text(d, (x + vw + self.px(4), vy), metric.unit, unit_f, dim, anchor="ls", stroke=False)
            self.text(d, (x, vy + self.px(32)), metric.label, label_f, self.accent, anchor="ls", stroke=False)
            x += lay["col_w"]

        if lay["map"]:
            self.draw_map_dynamic(d, lay["map"], t)

        # Context line above the panel
        info = [x for x in (self.datetime_text(s), self.gps_text(s), self.camera_text(s)) if x]
        ix = x0 + self.gps_badge(d, x0, y0 - self.px(14) - self.px(22), s, 22)
        if info:
            ix += self.px(16) if ix > x0 else 0
            self.text(d, (ix, y0 - self.px(14)), "   |   ".join(info), self.font(22, "Medium"), anchor="ls")
