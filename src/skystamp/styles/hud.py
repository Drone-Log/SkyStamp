from __future__ import annotations

from .base import Style


class HudStyle(Style):
    id = "hud"
    name = "HUD"
    description = "Drone-OSD look: data in the corners, flight path mini-map."

    def _map_box(self):
        size = self.px(280)
        x1 = self.w - self.margin
        y1 = self.h - self.margin - (self.px(34) if self.opt.has("gps") else 0)
        return (x1 - size, y1 - size, x1, y1)

    def draw_static(self, d, img):
        if self.opt.has("map"):
            self.draw_map_static(d, self._map_box())
        # Corner brackets give it the viewfinder feel
        m, L, w = self.margin // 2, self.px(40), self.px(3)
        col = (255, 255, 255, 140)
        for cx, cy, sx, sy in ((m, m, 1, 1), (self.w - m, m, -1, 1),
                               (m, self.h - m, 1, -1), (self.w - m, self.h - m, -1, -1)):
            d.line([(cx, cy + sy * L), (cx, cy), (cx + sx * L, cy)], fill=col, width=w)

    def draw_dynamic(self, d, img, s, t):
        m = self.margin
        top = self.font(26, "Medium")

        dt = self.datetime_text(s)
        x = m
        if dt:
            self.text(d, (m, m), dt, top)
            x += self.text_width(dt, top) + self.px(24)
        self.gps_badge(d, x, m, s, 26)
        cam = self.camera_text(s)
        if cam:
            self.text(d, (self.w - m, m), cam, top, anchor="ra")

        # Bottom-left metric row: small label above big value
        label_f, value_f, unit_f = self.font(20, "Medium"), self.font(48, "SemiBold"), self.font(24, "Medium")
        x = m
        y = self.h - m
        for metric in self.metrics(s):
            self.text(d, (x, y - self.px(54)), metric.short, label_f, self.accent, anchor="ls")
            self.text(d, (x, y), metric.value, value_f, anchor="ls")
            vw = self.text_width(metric.value, value_f)
            self.text(d, (x + vw + self.px(4), y), metric.unit, unit_f, (255, 255, 255, 210), anchor="ls")
            x += vw + self.text_width(metric.unit, unit_f) + self.px(40)

        if self.opt.has("map"):
            box = self._map_box()
            self.draw_map_dynamic(d, box, t)
        gps = self.gps_text(s)
        if gps:
            self.text(d, (self.w - m, self.h - m), gps, self.font(20, "Medium"), anchor="rs")
