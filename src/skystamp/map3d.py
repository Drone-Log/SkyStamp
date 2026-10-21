"""3D flight-path map: a chase camera behind the drone, looking the way it
flies, over the map and optionally over real terrain and buildings.

Units are local metres around the flight (x east, y north, z up, 0 = the
ground at take-off). The ground is rendered column by column, near to far,
like a classic height-map renderer: each screen column samples the map and
the terrain along its line of sight and keeps whatever rises above what is
already drawn, so nearer hills cover the ones behind them. Distance fades
into haze, and a sky fills the rest. The path, its shadow and the drone are
drawn on top with Pillow every frame.
"""

from __future__ import annotations

import bisect
import math

import numpy as np
from PIL import Image, ImageDraw

from . import maptiles

TEXTURE = 2048  # map texture side, px
HEIGHT_GRID = 256  # terrain samples per side
MAX_GROUND_PX = 480  # the ground is rendered at most this size, then scaled
ZOOM = 1.3  # focal length in half-widths: ~75° field of view
DRONE_V = -0.3  # the drone sits a bit below the middle, to show more of what is ahead
NEAR_M = 0.5  # closer than this to the camera is not drawn
HEADING_SPAN_S = 2.0  # heading = where the drone went from t - span to t + span
HEADING_MIN_MOVE_M = 3.0  # moving less than this over the span: keep the last heading
HEADING_SMOOTH_S = 1.2  # camera turns are smoothed over about this long
BUILDING_CELL_M = 0.75  # building heights are rasterised at about this resolution…
BUILDING_GRID_MAX = 2048  # …up to this many cells per side
BUILDINGS_REACH_M = 600.0  # buildings are loaded this far around the path (the rest is in the haze)
MIN_RADIUS_M = 40.0  # path radius below this counts as this (sets the camera distance)
SKY_TOP = {True: (52, 92, 140), False: (16, 20, 28)}  # with / without a map background
WALL = (176, 174, 168)
HAZE = {True: (178, 196, 212), False: (40, 46, 58)}


class Scene3D:
    def __init__(self, samples, tiles: str | None, terrain: bool):
        """``samples``: (t, lat, lon, rel_alt) with a GPS position."""
        lat0 = samples[0][1]
        self.stretch = 1 / math.cos(math.radians(lat0))  # mercator metres per real metre
        merc = [maptiles.mercator(lat, lon) for _, lat, lon, _ in samples]
        xs, ys = [p[0] for p in merc], [p[1] for p in merc]
        self.cx, self.cy = (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2
        local = np.array([((x - self.cx) / self.stretch, (y - self.cy) / self.stretch, alt or 0.0)
                          for (x, y), (_, _, _, alt) in zip(merc, samples)], dtype=np.float64)
        r_path = max(float(np.hypot(local[:, 0], local[:, 1]).max()), MIN_RADIUS_M)
        self.r_path = r_path
        # DJI writes up to 60 samples a second: keep one every few tens of cm
        keep, last, gap = [0], local[0], r_path / 300
        for i in range(1, len(local)):
            if np.abs(local[i] - last).max() >= gap:
                keep.append(i)
                last = local[i]
        self.times = [samples[i][0] for i in keep]
        self.pts = local[keep]

        # Camera: further back and steeper the higher the flight, so the ground stays in view
        alt = float(np.percentile(np.maximum(local[:, 2], 0), 75))
        self.chase = float(np.clip(18 + 0.55 * alt + 0.04 * r_path, 22, 160))
        self.pitch = math.radians(float(np.clip(24 + 0.1 * alt, 24, 40)))
        self.haze_end = float(np.clip(8 * self.chase, 260, 1400))
        self.radius = r_path + self.chase + self.haze_end * 1.05  # map area around the flight
        self._headings()

        self.tiles = tiles
        self.terrain = terrain
        self.heights = None  # metres above take-off ground, HEIGHT_GRID² (row 0 = north)
        self.blocks = None  # building heights above the ground, square grid over the disc of radius blocks_r
        self._loaded = False

    # -- data ---------------------------------------------------------------
    def _load(self):
        """Download map / terrain on first use."""
        self._loaded = True
        g = self.radius * self.stretch
        box = (self.cx - g, self.cy - g, self.cx + g, self.cy + g)
        tex = maptiles.render_area(self.tiles, *box, TEXTURE, TEXTURE) if self.tiles else None
        self.has_tiles = tex is not None
        if tex is None:
            tex = self._grid_texture()
        else:
            tex = Image.blend(tex, Image.new("RGB", tex.size, (0, 0, 0)), 0.08)
        if self.terrain:
            h = maptiles.elevation_area(*box, HEIGHT_GRID)
            if h is not None:
                home = self._sample(h, self.pts[0, 0], self.pts[0, 1])
                self.heights = h - home
                tex = self._hillshade(tex)
            self._load_buildings()
        self.texture = tex
        # Mip levels for distant ground, so it doesn't shimmer
        self._mips = [np.asarray(tex, dtype=np.uint8)]
        while len(self._mips) < 5:
            n = self._mips[-1].shape[0] // 2
            self._mips.append(np.asarray(Image.fromarray(self._mips[-1]).resize((n, n), Image.BOX)))
        ground = self.ground_at(self.pts[:, 0], self.pts[:, 1])
        self.ground = np.column_stack([self.pts[:, :2], ground])
        self.pts[:, 2] = np.maximum(self.pts[:, 2], ground + 0.5)  # never under the terrain

    def _load_buildings(self):
        r = min(self.radius, self.r_path + BUILDINGS_REACH_M)
        g = r * self.stretch
        got = maptiles.buildings_area(self.cx - g, self.cy - g, self.cx + g, self.cy + g)
        if not got:
            return
        n = min(BUILDING_GRID_MAX, math.ceil(2 * r / BUILDING_CELL_M))
        img = Image.new("F", (n, n), 0.0)
        d = ImageDraw.Draw(img)
        k = n / (2 * r)
        for ring, h in sorted(got, key=lambda b: b[1]):  # taller ones last, so they win where outlines overlap
            pts = [(((x - self.cx) / self.stretch + r) * k, (r - (y - self.cy) / self.stretch) * k) for x, y in ring]
            d.polygon(pts, fill=float(h))
        self.blocks = np.asarray(img, dtype=np.float32)
        self.blocks_r = r

    def _wall_light(self, x, y):
        """Brightness of the building wall at the top edge (x, y), from the way it faces."""
        c = BUILDING_CELL_M * 4  # wide enough to smooth the stair steps of slanted outlines
        gx = self.surface_at(x + c, y) - self.surface_at(x - c, y)
        gy = self.surface_at(x, y + c) - self.surface_at(x, y - c)
        norm = np.hypot(gx, gy)
        nx, ny = -gx / np.maximum(norm, 1e-6), -gy / np.maximum(norm, 1e-6)  # outwards, away from the roof
        light = nx * -0.6 + ny * 0.8  # same north-west light as the hills
        return np.where(norm > 0, 0.72 + 0.28 * light, 0.72)

    def surface_at(self, x, y):
        """Ground plus buildings: what the camera sees and must stay above."""
        z = self.ground_at(x, y)
        if self.blocks is None:
            return z
        n = self.blocks.shape[0]
        col = ((np.asarray(x) + self.blocks_r) / (2 * self.blocks_r) * n).astype(int)
        row = ((self.blocks_r - np.asarray(y)) / (2 * self.blocks_r) * n).astype(int)
        inside = (col >= 0) & (col < n) & (row >= 0) & (row < n)
        return z + np.where(inside, self.blocks[np.clip(row, 0, n - 1), np.clip(col, 0, n - 1)], 0)

    def _grid_texture(self) -> Image.Image:
        img = Image.new("RGB", (TEXTURE, TEXTURE), (30, 34, 42))
        d = ImageDraw.Draw(img)
        span = 2 * self.radius
        step = next(s for s in (5, 10, 25, 50, 100, 250, 500, 1000, 2500) if span / s <= 40)
        px = TEXTURE / span
        k = math.ceil(-self.radius / step)
        while k * step <= self.radius:
            p = (k * step + self.radius) * px
            d.line([(p, 0), (p, TEXTURE)], fill=(66, 76, 92), width=3)
            d.line([(0, p), (TEXTURE, p)], fill=(66, 76, 92), width=3)
            k += 1
        return img

    def _hillshade(self, tex: Image.Image) -> Image.Image:
        """Light the map from the north-west so the relief reads even on a flat image."""
        h = np.asarray(Image.fromarray(self.heights).resize((TEXTURE, TEXTURE), Image.BILINEAR))
        cell = 2 * self.radius / TEXTURE
        dzdy, dzdx = np.gradient(h, cell)  # rows go south, so dzdy is d/d(-y)
        nx, ny, nz = -dzdx, dzdy, np.ones_like(h)
        light = np.array([-0.5, 0.5, 0.707])
        shade = (nx * light[0] + ny * light[1] + nz * light[2]) / np.sqrt(nx * nx + ny * ny + 1)
        factor = np.clip(0.35 + 0.65 * shade / light[2], 0.45, 1.25)
        rgb = np.asarray(tex, dtype=np.float32) * factor[..., None]
        return Image.fromarray(np.clip(rgb, 0, 255).astype(np.uint8))

    def _sample(self, h, x, y):
        """Bilinear height at local metres (arrays or scalars)."""
        n = h.shape[0]
        col = (np.asarray(x) + self.radius) / (2 * self.radius) * (n - 1)
        row = (self.radius - np.asarray(y)) / (2 * self.radius) * (n - 1)
        c0 = np.clip(np.floor(col).astype(int), 0, n - 2)
        r0 = np.clip(np.floor(row).astype(int), 0, n - 2)
        fc, fr = np.clip(col - c0, 0, 1), np.clip(row - r0, 0, 1)
        top = h[r0, c0] * (1 - fc) + h[r0, c0 + 1] * fc
        bot = h[r0 + 1, c0] * (1 - fc) + h[r0 + 1, c0 + 1] * fc
        return top * (1 - fr) + bot * fr

    def ground_at(self, x, y):
        if self.heights is None:
            return np.zeros_like(np.asarray(x, dtype=np.float64))
        return self._sample(self.heights, x, y)

    def _colors(self, x, y, footprint):
        """Map colour at local metres, from the mip level matching ``footprint`` (m per px)."""
        texel = 2 * self.radius / TEXTURE
        level = np.clip(np.log2(np.maximum(footprint / texel, 1)), 0, len(self._mips) - 1).astype(int)
        out = np.empty((len(x), 3), dtype=np.float32)
        for lv, tex in enumerate(self._mips):
            m = level == lv
            if not m.any():
                continue
            n = tex.shape[0]
            col = (x[m] + self.radius) / (2 * self.radius) * n - 0.5
            row = (self.radius - y[m]) / (2 * self.radius) * n - 0.5
            c0 = np.clip(np.floor(col).astype(int), 0, n - 2)
            r0 = np.clip(np.floor(row).astype(int), 0, n - 2)
            fc = np.clip(col - c0, 0, 1)[:, None]
            fr = np.clip(row - r0, 0, 1)[:, None]
            top = tex[r0, c0] * (1 - fc) + tex[r0, c0 + 1] * fc
            bot = tex[r0 + 1, c0] * (1 - fc) + tex[r0 + 1, c0 + 1] * fc
            out[m] = top * (1 - fr) + bot * fr
        return out

    # -- camera ---------------------------------------------------------------
    def _headings(self):
        """Smoothed direction of travel (radians, clockwise from north) on a 10 Hz grid."""
        t0, t1 = self.times[0], self.times[-1]
        self._ht = np.arange(t0, t1 + 0.1, 0.1)
        before = self.position(self._ht - HEADING_SPAN_S)
        after = self.position(self._ht + HEADING_SPAN_S)
        dx, dy = after[:, 0] - before[:, 0], after[:, 1] - before[:, 1]
        moving = np.hypot(dx, dy) >= HEADING_MIN_MOVE_M
        if not moving.any():
            self._h = np.zeros_like(self._ht)  # hovering only: look north, like the 2D map
            return
        h = np.arctan2(dx, dy)
        idx = np.where(moving, np.arange(len(h)), 0)
        np.maximum.accumulate(idx, out=idx)  # hold the last heading while hovering
        first = int(np.argmax(moving))
        idx[:first] = first  # before the first move: the first heading
        h = np.unwrap(h[idx])
        sigma = HEADING_SMOOTH_S / 0.1
        k = np.exp(-0.5 * (np.arange(-3 * sigma, 3 * sigma + 1) / sigma) ** 2)
        pad = len(k) // 2
        self._h = np.convolve(np.pad(h, pad, mode="edge"), k / k.sum(), mode="valid")

    def position(self, t):
        """Drone position at time(s) t, between the kept samples."""
        t = np.asarray(t, dtype=np.float64)
        return np.stack([np.interp(t, self.times, self.pts[:, i]) for i in range(3)], axis=-1)

    def heading(self, t: float) -> float:
        return float(np.interp(t, self._ht, self._h))

    def camera(self, t: float) -> dict:
        target = self.position(t)
        h = self.heading(t)
        back = np.array([-math.sin(h) * math.cos(self.pitch), -math.cos(h) * math.cos(self.pitch),
                         math.sin(self.pitch)])
        cam = target + self.chase * back
        cam[2] = max(cam[2], float(self.surface_at(cam[0], cam[1])) + 4)  # don't dig into a hill or a roof
        fwd = target - cam
        fwd /= np.linalg.norm(fwd)
        right = np.array([fwd[1], -fwd[0], 0.0])
        right /= np.linalg.norm(right)
        up = np.cross(right, fwd)
        return {"pos": cam, "fwd": fwd, "right": right, "up": up, "target": target,
                "v_off": -DRONE_V / ZOOM}

    def _project(self, p, c):
        """Normalised screen coords (u right, v up; ±1 = box edge) and depth."""
        q = np.asarray(p, dtype=np.float64) - c["pos"]
        depth = q @ c["fwd"]
        safe = np.where(depth > NEAR_M, depth, np.nan)
        return q @ c["right"] / safe * ZOOM, (q @ c["up"] / safe - c["v_off"]) * ZOOM, depth

    def to_box(self, p, c, x0, y0, side):
        """Pixel coordinates; NaN for points behind the camera."""
        u, v, _ = self._project(p, c)
        half = side / 2
        return np.column_stack([x0 + half + u * half, y0 + half - v * half])

    def _runs(self, p, c, side):
        """Screen polylines of a 3D polyline, cut where it passes behind the camera."""
        q = np.asarray(p, dtype=np.float64)
        depth = (q - c["pos"]) @ c["fwd"]
        ok = depth > NEAR_M
        runs, cur = [], []
        for i in range(len(q)):
            if ok[i]:
                if not cur and i and not ok[i - 1]:  # coming back into view: start at the near plane
                    a = (NEAR_M - depth[i - 1]) / (depth[i] - depth[i - 1])
                    cur.append(q[i - 1] + (q[i] - q[i - 1]) * (a + 1e-6))
                cur.append(q[i])
            elif cur:
                a = (NEAR_M - depth[i - 1]) / (depth[i] - depth[i - 1])
                cur.append(q[i - 1] + (q[i] - q[i - 1]) * (a - 1e-6))
                runs.append(cur)
                cur = []
        if cur:
            runs.append(cur)
        return [[tuple(xy) for xy in self.to_box(np.array(r), c, 0, 0, side)] for r in runs if len(r) > 1]

    # -- ground -------------------------------------------------------------
    def ground_image(self, side: int, c: dict) -> Image.Image:
        size = min(side, MAX_GROUND_PX)
        img = self._render_ground(size, c)
        return img if size == side else img.resize((side, side), Image.BICUBIC)

    def _render_ground(self, size: int, c: dict) -> Image.Image:
        cam, fwd, right, up = c["pos"], c["fwd"], c["right"], c["up"]
        sky_top = np.array(SKY_TOP[self.has_tiles], dtype=np.float32)
        haze = np.array(HAZE[self.has_tiles], dtype=np.float32)

        # Rows on screen where the horizon is (fwd + b·up level), and the sky above it
        b_horizon = -fwd[2] / up[2]
        y_horizon = size / 2 - (b_horizon - c["v_off"]) * ZOOM * size / 2
        rows = np.arange(size, dtype=np.float32)
        k = np.clip((y_horizon - rows) / (size * 0.7), 0, 1)[:, None]
        sky = haze * (1 - k) + sky_top * k
        img = np.broadcast_to(sky[:, None, :], (size, size, 3)).copy()

        # Lines of sight for every column, from below the bottom edge up to the
        # horizon, meeting a level plane at the height of the ground around here
        z_ref = float(min(self.ground_at(cam[0], cam[1]), self.ground_at(*c["target"][:2])))
        z_ref = min(z_ref, cam[2] - 1)
        n = size * 2
        b_lo = (-1.3 / ZOOM) + c["v_off"]
        b_hi = b_horizon - 1e-4
        # Spread the samples evenly on screen up to the haze distance, not up to the horizon
        bs = np.linspace(b_lo, b_hi, 4000)
        dirs = fwd[None, :] + bs[:, None] * up[None, :]
        far = (z_ref - cam[2]) / dirs[:, 2] * np.hypot(dirs[:, 0], dirs[:, 1])
        b_hi = bs[min(int(np.searchsorted(far, self.haze_end)), len(bs) - 1)]
        b = np.linspace(b_lo, b_hi, n)
        a = ((np.arange(size) + 0.5) / size * 2 - 1) / ZOOM
        d = fwd[None, None, :] + a[:, None, None] * right[None, None, :] + b[None, :, None] * up[None, None, :]
        s = (z_ref - cam[2]) / d[..., 2]  # (size, n): columns × samples, near to far
        x = cam[0] + s * d[..., 0]
        y = cam[1] + s * d[..., 1]
        z = self.surface_at(x, y) if self.heights is not None else np.zeros_like(x)
        # A sample much higher than the one before it in its column is the front of a building:
        # it covers the wall's height on screen, so it gets the wall's shade
        wall = np.zeros_like(z, dtype=bool)
        wall[:, 1:] = z[:, 1:] - z[:, :-1] > 1.0
        q = np.stack([x - cam[0], y - cam[1], z - cam[2]], axis=-1)
        depth = q @ fwd
        dist = np.linalg.norm(q, axis=-1)
        sy = size / 2 - (q @ up / np.maximum(depth, NEAR_M) - c["v_off"]) * ZOOM * size / 2
        gone = (depth <= NEAR_M) | (np.hypot(q[..., 0], q[..., 1]) > self.haze_end)
        sy = np.clip(np.where(gone, size + 1, sy), -1, size + 1)

        # Each pixel shows the nearest sample whose top reaches up to it
        top = np.minimum.accumulate(sy, axis=1)
        off = np.arange(size)[:, None] * (size + 4)
        hit = np.searchsorted((-top + off).ravel(), (-rows[None, :] + off).ravel(), side="left")
        hit = hit.reshape(size, size) - np.arange(size)[:, None] * n  # (column, row) → sample
        seen = hit < n
        cols, rws = np.nonzero(seen)
        idx = hit[cols, rws]
        px = x[cols, idx]
        py = y[cols, idx]
        dd = dist[cols, idx]
        footprint = dd * 2 / (ZOOM * size) / np.maximum(-d[cols, idx, 2] / np.linalg.norm(d[cols, idx], axis=-1), 0.15)
        rgb = self._colors(px, py, footprint)
        w = wall[cols, idx]
        if w.any():  # one colour per face, lit by its direction: the map under it would only paint stripes
            rgb[w] = np.array(WALL, dtype=np.float32) * self._wall_light(px[w], py[w])[:, None]
        f = np.clip((dd - 0.3 * self.haze_end) / (0.7 * self.haze_end), 0, 1) ** 1.5
        img[rws, cols] = rgb * (1 - f[:, None]) + haze * f[:, None]
        return Image.fromarray(np.clip(img, 0, 255).astype(np.uint8))

    # -- frame ----------------------------------------------------------------
    def draw(self, frame: Image.Image, d: ImageDraw.ImageDraw, box, t: float, style) -> None:
        if not self._loaded:
            self._load()
        x0, y0, x1, y1 = (round(v) for v in box)
        side = min(x1 - x0, y1 - y0)
        c = self.camera(t)
        ground = self.ground_image(side, c)
        layer = Image.new("RGBA", (side, side), (0, 0, 0, 0))
        ld = ImageDraw.Draw(layer)

        n = max(1, bisect.bisect_right(self.times, t))
        now = c["target"]
        flown = np.vstack([self.pts[:n], now[None, :]])
        below = np.array([now[0], now[1], float(self.ground_at(now[0], now[1]))])
        flown_low = np.vstack([self.ground[:n], below[None, :]])
        for run in self._runs(self.ground, c, side):
            ld.line(run, fill=(0, 0, 0, 120), width=style.px(3), joint="curve")
        path = self.to_box(self.pts, c, 0, 0, side)
        shadow = self.to_box(self.ground, c, 0, 0, side)
        curtain = (*style.accent[:3], 80)
        for i in range(0, n, max(1, len(path) // 80)):
            if not (np.isnan(path[i]).any() or np.isnan(shadow[i]).any()):
                ld.line([tuple(shadow[i]), tuple(path[i])], fill=curtain, width=style.px(1.5))
        for run in self._runs(self.pts, c, side):
            ld.line(run, fill=(0, 0, 0, 110), width=style.px(5), joint="curve")  # keeps it readable on sky
            ld.line(run, fill=(255, 255, 255, 150), width=style.px(2.5), joint="curve")
        for run in self._runs(flown, c, side):
            ld.line(run, fill=style.accent, width=style.px(4), joint="curve")

        home = self.to_box(self.ground[:1], c, 0, 0, side)[0]
        if not np.isnan(home).any():
            hx, hy = home
            r = style.px(5)
            ld.rectangle((hx - r, hy - r, hx + r, hy + r), outline=(255, 255, 255, 230), width=style.px(2))
            style.text(ld, (hx + r + style.px(4), hy), "H", style.font(15, "Bold"), anchor="lm")

        (x, y), (gx, gy) = self.to_box(np.array([now, flown_low[-1]]), c, 0, 0, side)
        if not np.isnan(gy):
            ld.line([(gx, gy), (x, y)], fill=(255, 255, 255, 210), width=style.px(2))
        r = style.px(8)
        ld.ellipse((x - r - style.px(2), y - r - style.px(2), x + r + style.px(2), y + r + style.px(2)),
                   fill=(0, 0, 0, 160))
        ld.ellipse((x - r, y - r, x + r, y + r), fill=style.accent, outline=(255, 255, 255, 255), width=style.px(2))

        mask = Image.new("L", (side, side), 0)
        radius = style.px(14)
        ImageDraw.Draw(mask).rounded_rectangle((0, 0, side - 1, side - 1), radius=radius, fill=255)
        ground = ground.convert("RGBA")
        ground.alpha_composite(layer)
        ground.putalpha(mask)
        frame.alpha_composite(ground, (x0, y0))
        d.rounded_rectangle((x0, y0, x0 + side, y0 + side), radius=radius, outline=(255, 255, 255, 200),
                            width=style.px(2))

    def credit(self) -> str | None:
        if not self._loaded:
            return None
        credits = [maptiles.PROVIDERS[self.tiles].attribution] if self.has_tiles else []
        if self.heights is not None:
            credits.append(maptiles.ELEVATION.attribution)
        if self.blocks is not None and not (self.has_tiles and self.tiles == "osm"):
            credits.append(maptiles.BUILDINGS.attribution)
        return " · ".join(credits) or None
