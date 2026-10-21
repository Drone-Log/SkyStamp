import math
from datetime import datetime, timedelta

import numpy as np
import pytest
from PIL import Image

from skystamp import map3d, maptiles
from skystamp.styles import STYLES, OverlayOptions
from skystamp.telemetry import Sample, Telemetry


def _tel():
    t0 = datetime(2026, 10, 10, 16, 43, 32)
    return Telemetry(samples=[Sample(t=i * 0.5, timestamp=t0 + timedelta(seconds=i / 2), lat=44.37 + i * 1e-5,
                                     lon=26.15 + (i % 7) * 1e-5, rel_alt=2 + i, gps_fix=True)
                              for i in range(40)])


@pytest.fixture(autouse=True)
def no_buildings(monkeypatch):
    """Tests stay offline: no buildings unless a test adds some."""
    monkeypatch.setattr(maptiles, "buildings_area", lambda *a: None)


@pytest.fixture
def hill(monkeypatch):
    """Offline terrain: a 60 m hill north-east of the take-off point."""
    def fake(x0, y0, x1, y1, size):
        c = np.linspace(-1, 1, size, dtype=np.float32)
        x, y = np.meshgrid(c, -c)
        return 300 + 60 * np.exp(-((x - 0.3) ** 2 + (y - 0.3) ** 2) * 8)
    monkeypatch.setattr(maptiles, "elevation_area", fake)


@pytest.mark.parametrize("view", ["3d", "terrain"])
@pytest.mark.parametrize("sid", list(STYLES))
def test_3d_views_render_in_every_style(sid, view, hill):
    st = STYLES[sid](1920, 1080, _tel(), OverlayOptions(map_tiles="none", map_view=view))
    a, b = st.render(3.0), st.render(15.0)
    assert a.size == (1920, 1080) and a.getbbox() is not None
    for x0, y0, x1, y1 in st.boxes.values():
        assert 0 <= x0 < x1 <= 1920 and 0 <= y0 < y1 <= 1080
    if st._map.scene:  # the camera follows the drone, so the map differs between moments
        assert a.tobytes() != b.tobytes()


def _scene(terrain=False, tel=None):
    s = (tel or _tel()).samples
    sc = map3d.Scene3D([(x.t, x.lat, x.lon, x.rel_alt) for x in s], None, terrain)
    sc._load()
    return sc


def _north_then_east():
    """20 s north, then 20 s east, at 30 m."""
    t0 = datetime(2026, 10, 10, 16, 43, 32)
    out = []
    for i in range(400):
        t = i / 10
        n, e = min(t, 20) * 5, max(t - 20, 0) * 5
        out.append(Sample(t=t, timestamp=t0 + timedelta(seconds=t), lat=44.37 + n / 111320,
                          lon=26.15 + e / 79700, rel_alt=30, gps_fix=True))
    return Telemetry(samples=out)


def test_camera_follows_the_direction_of_travel():
    sc = _scene(tel=_north_then_east())
    assert abs(math.degrees(sc.heading(8))) < 5
    assert abs(math.degrees(sc.heading(32)) - 90) < 5
    for t in (8, 32):
        c = sc.camera(t)
        back = c["pos"][:2] - c["target"][:2]
        h = sc.heading(t)
        assert back @ np.array([math.sin(h), math.cos(h)]) < -0.9 * np.linalg.norm(back)  # behind it
        (x, y), = sc.to_box(c["target"][None, :], c, 0, 0, 400)
        assert abs(x - 200) < 1 and abs(y - 200 * (1 - map3d.DRONE_V)) < 1  # same spot every frame


def test_hovering_keeps_the_last_heading():
    tel = _north_then_east()
    last = tel.samples[-1]
    tel.samples += [Sample(t=40 + i / 10, timestamp=last.timestamp + timedelta(seconds=i / 10), lat=last.lat,
                           lon=last.lon, rel_alt=30, gps_fix=True) for i in range(1, 200)]
    sc = _scene(tel=tel)
    assert abs(math.degrees(sc.heading(55)) - 90) < 5


def test_ground_below_sky_above():
    sc = _scene()
    img = sc.ground_image(300, sc.camera(10.0))
    top, bottom = img.getpixel((150, 2)), img.getpixel((150, 297))
    assert top == tuple(map3d.SKY_TOP[False]) or sum(top) < sum(map3d.HAZE[False])
    assert bottom != top


def test_path_behind_the_camera_is_cut_not_mirrored():
    sc = _scene(tel=_north_then_east())
    c = sc.camera(30)
    for run in sc._runs(sc.pts, c, 400):
        assert not np.isnan(np.array(run)).any()
    _, _, depth = sc._project(sc.pts[:1], c)
    assert depth[0] < 0  # take-off is behind the camera by then


def test_terrain_is_relative_to_take_off_and_never_above_the_drone(hill):
    sc = _scene(terrain=True)
    assert sc.heights is not None
    assert abs(float(sc.ground_at(sc.pts[0, 0], sc.pts[0, 1]))) < 0.01
    assert (sc.pts[:, 2] >= sc.ground[:, 2]).all()
    c = sc.camera(10.0)
    assert c["pos"][2] >= float(sc.ground_at(c["pos"][0], c["pos"][1])) + 3.9
    assert sc.ground_image(300, c).getbbox() is not None


def test_offline_terrain_falls_back_to_flat(monkeypatch):
    monkeypatch.setattr(maptiles, "elevation_area", lambda *a: None)
    sc = _scene(terrain=True)
    assert sc.heights is None and sc.ground_image(200, sc.camera(0.0)).getbbox() is not None


def test_buildings_rise_from_the_ground_and_keep_the_camera_above(hill, monkeypatch):
    sc0 = _scene()
    # a 40 m tower 30 m north of take-off, 20 m wide
    x, y = sc0.cx, sc0.cy + 30 * sc0.stretch
    k = 10 * sc0.stretch
    tower = [(x - k, y - k), (x + k, y - k), (x + k, y + k), (x - k, y + k)]
    monkeypatch.setattr(maptiles, "buildings_area", lambda *a: [(tower, 40.0)])
    sc = _scene(terrain=True)
    assert sc.blocks is not None
    tx, ty = 0.0, (y - sc.cy) / sc.stretch
    assert abs(float(sc.surface_at(tx, ty) - sc.ground_at(tx, ty)) - 40) < 0.01
    assert abs(float(sc.surface_at(tx + 30, ty) - sc.ground_at(tx + 30, ty))) < 0.01
    for t in (0.0, 5.0, 10.0, 19.0):
        c = sc.camera(t)
        assert c["pos"][2] >= float(sc.surface_at(c["pos"][0], c["pos"][1])) + 3.9
    assert "OpenStreetMap" in sc.credit()


def test_building_heights_from_tags():
    assert maptiles._height({"building": "yes", "height": "23.5 m"}) == 23.5
    assert maptiles._height({"building": "apartments", "building:levels": "10"}) == 31.0
    assert maptiles._height({"building": "garage"}) == 3.0
    assert maptiles._height({"building": "yes"}) == maptiles.DEFAULT_BUILDING_M


def test_terrarium_decoding(monkeypatch):
    # 1234.5 m → R*256 + G + B/256 - 32768
    v = 1234.5 + 32768
    rgb = (int(v // 256), int(v % 256), int(round((v % 1) * 256)))
    monkeypatch.setattr(maptiles, "_tile", lambda *a: Image.new("RGB", (256, 256), rgb))
    x, y = maptiles.mercator(45.4, 25.47)
    h = maptiles.elevation_area(x - 500, y - 500, x + 500, y + 500, 32)
    assert h.shape == (32, 32) and np.allclose(h, 1234.5, atol=0.01)
