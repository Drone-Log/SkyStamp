"""3D flight-path map: the path at its real height above a disc of map, seen by
a camera that slowly circles the flight, optionally over real terrain.

Units are local metres around the flight (x east, y north, z up, 0 = the
ground at take-off). The flat ground is one perspective warp of the map
texture; terrain is drawn as a grid of coloured points projected back to
front, so nearer hills cover the ones behind them. The path, its shadow and
the drone are drawn on top with Pillow every frame.
"""

from __future__ import annotations

import bisect
import math

import numpy as np
from PIL import Image, ImageDraw

from . import maptiles

ORBIT_DEG_PER_S = 4.0  # one full turn every 90 s
START_AZIMUTH = 180.0  # camera south of the flight, looking north (like the 2D map)
TILT_DEG = 40.0  # camera elevation above the horizon
TEXTURE = 1024  # map texture side, px
HEIGHT_GRID = 256  # terrain samples per side
MAX_TERRAIN_PX = 400  # terrain is drawn at most this size, then scaled
MIN_RADIUS_M = 40.0  # without a background map: stay close to the path
MIN_RADIUS_TILES_M = 75.0  # with one: show some surroundings


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
        r_path = float(np.hypot(local[:, 0], local[:, 1]).max())
        self.radius = max(r_path * 1.3, MIN_RADIUS_TILES_M if tiles else MIN_RADIUS_M)
        # DJI writes up to 60 samples a second: keep one every few tens of cm
        keep, last, gap = [0], local[0], self.radius / 300
        for i in range(1, len(local)):
            if np.abs(local[i] - last).max() >= gap:
                keep.append(i)
                last = local[i]
        self.times = [samples[i][0] for i in keep]
        pts = local[keep]
        self.tiles = tiles
        self.terrain = terrain
        self.heights = None  # metres above take-off ground, HEIGHT_GRID² (row 0 = north)
        self._loaded = False
        self.pts = pts
        self._grids: dict[int, dict] = {}

    # -- data ---------------------------------------------------------------
    def _load(self):
        """Download map / terrain on first use, then fix the camera framing."""
        self._loaded = True
        g = self.radius * self.stretch
        box = (self.cx - g, self.cy - g, self.cx + g, self.cy + g)
        tex = maptiles.render_area(self.tiles, *box, TEXTURE, TEXTURE) if self.tiles else None
        self.has_tiles = tex is not None
        if tex is None:
            tex = self._grid_texture()
        else:
            tex = Image.blend(tex, Image.new("RGB", tex.size, (0, 0, 0)), 0.12)
        if self.terrain:
            h = maptiles.elevation_area(*box, HEIGHT_GRID)
            if h is not None:
                home = self._sample(h, self.pts[0, 0], self.pts[0, 1])
                self.heights = h - home
                tex = self._hillshade(tex)
        self.texture = tex
        ground = self.ground_at(self.pts[:, 0], self.pts[:, 1])
        self.ground = np.column_stack([self.pts[:, :2], ground])
        self.pts[:, 2] = np.maximum(self.pts[:, 2], ground + 0.5)  # never under the terrain
        self._frame_camera()

    def _grid_texture(self) -> Image.Image:
        img = Image.new("RGB", (TEXTURE, TEXTURE), (30, 34, 42))
        d = ImageDraw.Draw(img)
        span = 2 * self.radius
        step = next(s for s in (5, 10, 25, 50, 100, 250, 500, 1000, 2500) if span / s <= 16)
        px = TEXTURE / span
        k = math.ceil(-self.radius / step)
        while k * step <= self.radius:
            p = (k * step + self.radius) * px
            d.line([(p, 0), (p, TEXTURE)], fill=(62, 70, 84), width=2)
            d.line([(0, p), (TEXTURE, p)], fill=(62, 70, 84), width=2)
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

    # -- camera ---------------------------------------------------------------
    def _frame_camera(self):
        """Zoom and vertical offset so the disc and the path fit at every angle."""
        top = float(self.pts[:, 2].max())
        self.target = np.array([0.0, 0.0, top * 0.35])
        a = np.linspace(0, 2 * np.pi, 64, endpoint=False)
        rim = np.column_stack([self.radius * np.cos(a), self.radius * np.sin(a)])
        rim = np.column_stack([rim, self.ground_at(rim[:, 0], rim[:, 1])])
        fit = np.vstack([rim, self.pts, self.ground])
        reach = float(np.linalg.norm(fit - self.target, axis=1).max())
        self.distance = 2.4 * reach
        self.zoom, self.v_off = 1.0, 0.0
        us, vs = [], []
        for az in range(0, 360, 15):
            u, v, _ = self._project(fit, az)
            us.append(np.abs(u).max())
            vs += [v.min(), v.max()]
        vmin, vmax = min(vs), max(vs)
        self.zoom = min(0.92 / max(us), 1.84 / max(vmax - vmin, 1e-6))
        self.v_off = (vmin + vmax) / 2

    def azimuth(self, t: float) -> float:
        return (START_AZIMUTH + ORBIT_DEG_PER_S * t) % 360

    def _basis(self, az: float):
        a, tilt = math.radians(az), math.radians(TILT_DEG)
        back = np.array([math.cos(tilt) * math.sin(a), math.cos(tilt) * math.cos(a), math.sin(tilt)])
        cam = self.target + self.distance * back
        fwd = -back
        right = np.array([fwd[1], -fwd[0], 0.0])
        right /= np.linalg.norm(right)
        up = np.cross(right, fwd)
        return cam, fwd, right, up

    def _project(self, p, az):
        """Normalised screen coords (u right, v up; ±1 = box edge) and depth."""
        cam, fwd, right, up = self._basis(az)
        q = np.asarray(p, dtype=np.float64) - cam
        depth = q @ fwd
        return q @ right / depth * self.zoom, (q @ up / depth - self.v_off) * self.zoom, depth

    def to_box(self, p, az, x0, y0, side):
        u, v, _ = self._project(p, az)
        half = side / 2
        return np.column_stack([x0 + half + u * half, y0 + half - v * half])

    # -- ground -------------------------------------------------------------
    def ground_image(self, side: int, az: float) -> Image.Image:
        if self.heights is None:
            return self._flat_ground(side, az)
        size = min(side, MAX_TERRAIN_PX)
        img = self._terrain_ground(size, az)
        return img if size == side else img.resize((side, side), Image.BICUBIC)

    def _disc_texture(self) -> Image.Image:
        if not hasattr(self, "_disc"):
            mask = Image.new("L", (TEXTURE * 2, TEXTURE * 2), 0)
            ImageDraw.Draw(mask).ellipse((0, 0, TEXTURE * 2 - 1, TEXTURE * 2 - 1), fill=255)
            self._disc = self.texture.convert("RGBA")
            self._disc.putalpha(mask.resize((TEXTURE, TEXTURE), Image.LANCZOS))
        return self._disc

    def _flat_ground(self, side: int, az: float) -> Image.Image:
        """The map plane is a homography of the texture: one warp per frame."""
        r = self.radius
        corners = np.array([[-r, r, 0], [r, r, 0], [r, -r, 0], [-r, -r, 0]])
        screen = self.to_box(corners, az, 0, 0, side)
        tex = [(0, 0), (TEXTURE, 0), (TEXTURE, TEXTURE), (0, TEXTURE)]
        rows, rhs = [], []
        for (X, Y), (x, y) in zip(screen, tex):  # output pixel (X, Y) → texture pixel (x, y)
            rows.append([X, Y, 1, 0, 0, 0, -x * X, -x * Y])
            rows.append([0, 0, 0, X, Y, 1, -y * X, -y * Y])
            rhs += [x, y]
        coeffs = np.linalg.solve(np.array(rows, dtype=np.float64), np.array(rhs, dtype=np.float64))
        return self._disc_texture().transform((side, side), Image.PERSPECTIVE, tuple(coeffs), Image.BICUBIC)

    def _terrain_grid(self, n: int) -> dict:
        """Points of the terrain inside the disc, at n×n resolution."""
        if n not in self._grids:
            r = self.radius
            c = (np.arange(n) + 0.5) / n * 2 * r - r
            x, y = np.meshgrid(c, -c)  # row 0 = north
            z = np.asarray(Image.fromarray(self.heights).resize((n, n), Image.BILINEAR), dtype=np.float64)
            rgb = np.asarray(self.texture.resize((n, n), Image.LANCZOS), dtype=np.uint8)
            self._grids[n] = {"x": x, "y": y, "z": z, "rgb": rgb, "in": x * x + y * y <= r * r}
        return self._grids[n]

    def _terrain_ground(self, size: int, az: float) -> Image.Image:
        g = self._terrain_grid(min(2 * size, 800))
        cam, fwd, _, _ = self._basis(az)
        # Back to front: far rows first, far points first within a row, so
        # with numpy's in-order assignment nearer points overwrite farther ones
        flip_rows = fwd[1] < 0  # rows run north→south: looking south, the far rows are the last ones
        flip_cols = fwd[0] > 0  # columns run west→east: looking east, the far ones are the last ones
        order = (slice(None, None, -1) if flip_rows else slice(None),
                 slice(None, None, -1) if flip_cols else slice(None))
        transpose = abs(fwd[0]) > abs(fwd[1])  # sweep along the axis we look along

        def arr(k):
            a = g[k][order]
            return a.swapaxes(0, 1) if transpose else a

        keep = arr("in").reshape(-1)
        p = np.column_stack([arr("x").reshape(-1)[keep], arr("y").reshape(-1)[keep], arr("z").reshape(-1)[keep]])
        rgb = arr("rgb").reshape(-1, 3)[keep]
        xy = self.to_box(p, az, 0, 0, size)
        px, py = xy[:, 0].astype(np.int64), xy[:, 1].astype(np.int64)
        ok = (px >= 0) & (px < size) & (py >= 0) & (py < size)
        buf = np.zeros((size * size, 4), dtype=np.uint8)
        idx = py[ok] * size + px[ok]
        buf[idx, :3] = rgb[ok]
        buf[idx, 3] = 255
        img = buf.reshape(size, size, 4)
        for _ in range(2):  # close pinholes where the grid is sparser than the screen
            hole = img[..., 3] == 0
            for dy, dx in ((0, 1), (1, 0), (0, -1), (-1, 0)):
                nb = np.roll(img, (dy, dx), axis=(0, 1))
                fill = hole & (nb[..., 3] > 0)
                img[fill] = nb[fill]
                hole &= ~fill
        return Image.fromarray(img)

    # -- frame ----------------------------------------------------------------
    def draw(self, frame: Image.Image, d: ImageDraw.ImageDraw, box, t: float, style) -> None:
        if not self._loaded:
            self._load()
        x0, y0, x1, y1 = (round(v) for v in box)
        side = min(x1 - x0, y1 - y0)
        az = self.azimuth(t)
        ground = self.ground_image(side, az)
        frame.alpha_composite(ground, (x0, y0))

        n = max(1, bisect.bisect_right(self.times, t))
        path = self.to_box(self.pts, az, x0, y0, side)
        shadow = self.to_box(self.ground, az, x0, y0, side)
        pts, low = [tuple(p) for p in path], [tuple(p) for p in shadow]
        if len(low) > 1:
            d.line(low, fill=(0, 0, 0, 120), width=style.px(3), joint="curve")
            d.line(pts, fill=(0, 0, 0, 110), width=style.px(5), joint="curve")  # keeps it readable on sky
        step = max(1, len(pts) // 60)
        curtain = (*style.accent[:3], 70)
        for i in range(0, n, step):
            d.line([low[i], pts[i]], fill=curtain, width=style.px(1.5))
        if len(pts) > 1:
            d.line(pts, fill=(255, 255, 255, 150), width=style.px(2.5), joint="curve")
        if n > 1:
            d.line(pts[:n], fill=style.accent, width=style.px(4), joint="curve")

        hx, hy = low[0]
        r = style.px(5)
        d.rectangle((hx - r, hy - r, hx + r, hy + r), outline=(255, 255, 255, 230), width=style.px(2))
        style.text(d, (hx + r + style.px(4), hy), "H", style.font(15, "Bold"), anchor="lm")

        x, y = pts[n - 1]
        d.line([low[n - 1], (x, y)], fill=(255, 255, 255, 210), width=style.px(2))
        r = style.px(8)
        d.ellipse((x - r - style.px(2), y - r - style.px(2), x + r + style.px(2), y + r + style.px(2)),
                  fill=(0, 0, 0, 160))
        d.ellipse((x - r, y - r, x + r, y + r), fill=style.accent, outline=(255, 255, 255, 255), width=style.px(2))

        credits = [maptiles.PROVIDERS[self.tiles].attribution] if self.has_tiles else []
        if self.heights is not None:
            credits.append(maptiles.ELEVATION.attribution)
        if credits:
            style.text(d, (x1 - style.px(8), y1 - style.px(6)), " · ".join(credits),
                       style.font(11, "Medium"), (255, 255, 255, 230), anchor="rs")
