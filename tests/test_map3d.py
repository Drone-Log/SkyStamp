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
    if st._map.scene:  # the camera turns, so the map differs between moments
        assert a.tobytes() != b.tobytes()


def _scene(terrain=False):
    s = _tel().samples
    sc = map3d.Scene3D([(x.t, x.lat, x.lon, x.rel_alt) for x in s], None, terrain)
    sc._load()
    return sc


def test_flat_ground_matches_the_path_projection():
    sc = _scene()
    side, az = 400, 37.0
    img = sc.ground_image(side, az)
    for p in ([0, 0, 0], [sc.radius * 0.5, -sc.radius * 0.3, 0]):
        (x, y), = sc.to_box(np.array([p]), az, 0, 0, side)
        assert img.getpixel((int(x), int(y)))[3] == 255  # map drawn where the path says the ground is
    # outside the disc stays transparent
    assert img.getpixel((2, 2))[3] == 0


def test_terrain_is_relative_to_take_off_and_never_above_the_drone(hill):
    sc = _scene(terrain=True)
    assert sc.heights is not None
    assert abs(float(sc.ground_at(sc.pts[0, 0], sc.pts[0, 1]))) < 0.01
    assert (sc.pts[:, 2] >= sc.ground[:, 2]).all()
    img = sc.ground_image(300, 100.0)
    assert img.getbbox() is not None


def test_offline_terrain_falls_back_to_flat(monkeypatch):
    monkeypatch.setattr(maptiles, "elevation_area", lambda *a: None)
    sc = _scene(terrain=True)
    assert sc.heights is None and sc.ground_image(200, 0.0).getbbox() is not None


def test_terrarium_decoding(monkeypatch):
    # 1234.5 m → R*256 + G + B/256 - 32768
    v = 1234.5 + 32768
    rgb = (int(v // 256), int(v % 256), int(round((v % 1) * 256)))
    monkeypatch.setattr(maptiles, "_tile", lambda *a: Image.new("RGB", (256, 256), rgb))
    x, y = maptiles.mercator(45.4, 25.47)
    h = maptiles.elevation_area(x - 500, y - 500, x + 500, y + 500, 32)
    assert h.shape == (32, 32) and np.allclose(h, 1234.5, atol=0.01)
