from __future__ import annotations

from .base import Style


class MinimalStyle(Style):
    id = "minimal"
    name = "Minimal"
    description = "One clean line of data. Choose where it sits on the frame."

    def draw_dynamic(self, d, img, s, t):
        big, small = self.font(34, "SemiBold"), self.font(22, "Medium")
        dim = (255, 255, 255, 200)
        gap = self.px(26)

        # Build the main line as (text, font, color) runs
        runs = []
        for m in self.metrics(s):
            if runs:
                runs.append(("·", small, dim, gap))
            runs.append((m.short + " ", small, dim, 0))
            runs.append((m.value, big, self.fg, 0))
            runs.append((m.unit, small, dim, self.px(3)))
        extra = "    ".join(x for x in (self.datetime_text(s), self.camera_text(s), self.gps_text(s)) if x)
        sub_f = self.font(20, "Medium")
        badge_w = self.gps_badge_width(s, 20)
        gap_w = self.px(18) if badge_w and extra else 0
        sub_w = badge_w + gap_w + (self.text_width(extra, sub_f) if extra else 0)

        def run_width(r):
            text, fnt, _, pad = r
            return self.text_width(text, fnt) + (2 * pad if text == "·" else pad)

        width = sum(run_width(r) for r in runs)
        line_h = self.px(44)
        sub_h = self.px(30) if sub_w else 0
        block_h = (line_h if runs else 0) + sub_h

        pos = self.opt.position
        m = self.margin
        if pos.startswith("top"):
            y = m
        else:
            y = self.h - m - block_h

        def x_for(w):
            if pos.endswith("center"):
                return (self.w - w) // 2
            if pos.endswith("right"):
                return self.w - m - w
            return m

        if runs:
            x = x_for(width)
            base = y + line_h - self.px(8)
            for text, fnt, color, pad in runs:
                if text == "·":
                    x += pad
                    self.text(d, (x, base), text, fnt, color, anchor="ls")
                    x += self.text_width(text, fnt) + pad
                    continue
                x += pad
                self.text(d, (x, base), text, fnt, color, anchor="ls")
                x += self.text_width(text, fnt)
            y += line_h
        if sub_w:
            x = x_for(sub_w)
            self.gps_badge(d, x, y + self.px(22) - self.px(17), s, 20)
            if extra:
                self.text(d, (x + badge_w + gap_w, y + self.px(22)), extra, sub_f, dim, anchor="ls")
