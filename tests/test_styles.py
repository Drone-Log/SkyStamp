import json
from datetime import datetime, timedelta

import pytest

from skystamp.editor import anchor_for, move_element
from skystamp.styles import STYLES, OverlayOptions, SpecStyle, get_style
from skystamp.styles import library as lib
from skystamp.styles.engine import normalize
from skystamp.telemetry import Sample, Telemetry


def _tel():
    t0 = datetime(2026, 10, 10, 16, 43, 32)
    return Telemetry(samples=[Sample(t=i * 0.5, timestamp=t0 + timedelta(seconds=i / 2), lat=44.37 + i * 1e-5,
                                     lon=26.15, rel_alt=10 + i, gps_fix=True, satellites=20, battery=50 - i,
                                     iso=100, shutter="1/1000.0", fnum=2.2, ev=0) for i in range(20)])


def test_normalize_fills_defaults_and_drops_junk():
    spec = normalize({"name": "X", "elements": [
        {"type": "data", "x": 2, "y": -1, "anchor": "zz", "fields": ["altitude", "nope"], "evil": 1},
        {"type": "unknown"},
    ]})
    assert len(spec["elements"]) == 1
    el = spec["elements"][0]
    assert el["x"] == 1.0 and el["y"] == 0.0 and el["anchor"] == "tl"
    assert el["fields"] == ["altitude"] and "evil" not in el and el["labels"] == "above"


@pytest.mark.parametrize("sid", list(STYLES))
def test_builtin_styles_render_every_element(sid):
    st = STYLES[sid](1920, 1080, _tel(), OverlayOptions(map_tiles="none"))
    img = st.render(3.0)
    assert img.size == (1920, 1080) and img.getbbox() is not None
    assert st.boxes, sid
    for x0, y0, x1, y1 in st.boxes.values():
        assert 0 <= x0 < x1 <= 1920 and 0 <= y0 < y1 <= 1080


def test_layout_is_resolution_independent():
    spec = STYLES["classic"].spec
    small = SpecStyle(960, 540, _tel(), OverlayOptions(map_tiles="none"), spec)
    big = SpecStyle(3840, 2160, _tel(), OverlayOptions(map_tiles="none"), spec)
    small.render(1.0), big.render(1.0)
    for i in small.boxes:
        a, b = small.boxes[i], big.boxes[i]
        assert abs(a[0] * 4 - b[0]) < 40 and abs(a[1] * 4 - b[1]) < 40


def test_anchor_and_snapping():
    assert anchor_for(10, 10, 1920, 1080) == "tl"
    assert anchor_for(960, 540, 1920, 1080) == "c"
    assert anchor_for(1900, 1000, 1920, 1080) == "br"
    el = {"type": "info", "x": 0, "y": 0, "anchor": "tl", "size": 24, "position_option": True}
    # dropped near the top centre: centred, snapped to x=0.5, y=0
    moved, guides = move_element(el, (900, 50, 1020, 90), 1920, 1080, margin=48)
    assert moved["anchor"] == "t" and moved["x"] == 0.5 and moved["y"] == 0.0
    assert ("v", 960.0) in guides and "position_option" not in moved
    # far from any guide: free position
    moved, guides = move_element(el, (300, 300, 400, 340), 1920, 1080, margin=48)
    assert guides == [] and 0 < moved["x"] < 0.5


def test_user_style_save_import_export_delete(tmp_path, monkeypatch):
    monkeypatch.setattr(lib, "user_dir", lambda: tmp_path / "styles")
    spec = dict(STYLES["hud"].spec, name="My HUD")
    saved = lib.save_user_style(spec)
    assert saved.id == "user:my-hud" and not saved.builtin
    assert get_style("user:my-hud").name == "My HUD"
    # rename on save replaces the old file
    renamed = lib.save_user_style(dict(saved.spec, name="Renamed"), previous=saved)
    assert [p.name for p in (tmp_path / "styles").iterdir()] == ["renamed.skystyle"]
    out = tmp_path / "shared.skystyle"
    lib.export_style(renamed, out)
    assert json.loads(out.read_text())["name"] == "Renamed"
    imported = lib.import_style(out)  # same name: kept as a second copy
    assert imported.id == "user:renamed-2"
    assert get_style(str(out)).name == "Renamed"  # a path works too
    lib.delete_user_style(imported)
    assert "user:renamed-2" not in lib.user_styles()


def test_invalid_style_file(tmp_path):
    bad = tmp_path / "bad.skystyle"
    bad.write_text("{not json")
    with pytest.raises(ValueError):
        lib.load_file(bad)
