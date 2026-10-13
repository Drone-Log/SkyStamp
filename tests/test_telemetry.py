from skystamp.telemetry import parse_srt_text

NEW = """1
00:00:00,000 --> 00:00:00,033
<font size="28">FrameCnt: 1, DiffTime: 33ms
2026-10-10 16:43:33.147
[iso: 460] [shutter: 1/1600.0] [fnum: 2.2] [ev: 0] [latitude: 44.378130] [longitude: 26.154778] [rel_alt: 33.600 abs_alt: 105.734]</font>

2
00:00:01,000 --> 00:00:01,033
<font size="28">FrameCnt: 31, DiffTime: 33ms
2026-10-10 16:43:34.147
[iso: 460] [shutter: 1/1600.0] [fnum: 2.2] [ev: 0] [latitude: 44.378220] [longitude: 26.154778] [rel_alt: 35.600 abs_alt: 107.734]</font>
"""

LEGACY = """1
00:00:00,000 --> 00:00:00,033
F/2.8, SS 1000, ISO 100, EV 0, DZOOM 1.000, GPS (26.1547, 44.3781, 19), D 10.2m, H 5.5m, HS 2.3m/s, VS 0.0m/s
"""

NO_FIX = NEW.replace("44.378130", "0.000000").replace("26.154778", "0.000000", 1)


def test_new_format():
    tel = parse_srt_text(NEW)
    a, b = tel.samples
    assert a.lat == 44.378130 and a.lon == 26.154778
    assert a.rel_alt == 33.6 and a.iso == 460 and a.shutter == "1/1600.0" and a.fnum == 2.2
    assert a.timestamp.second == 33
    # 0.00009° lat ≈ 10 m in 1 s, 2 m climb
    assert 9.5 < b.dist_home < 10.5
    assert 9.5 < a.h_speed < 10.5
    assert abs(a.v_speed - 2.0) < 0.01
    assert a.heading is not None and (a.heading < 1 or a.heading > 359)  # due north


def test_lookup_by_time():
    tel = parse_srt_text(NEW)
    assert tel.at(0.5).t == 0.0
    assert tel.at(5).t == 1.0
    assert tel.at(-1).t == 0.0


def test_legacy_format():
    s = parse_srt_text(LEGACY).samples[0]
    assert (s.lat, s.lon) == (44.3781, 26.1547)
    assert s.rel_alt == 5.5 and s.iso == 100 and s.fnum == 2.8


def test_zero_coords_mean_no_gps():
    tel = parse_srt_text(NO_FIX)
    assert tel.samples[0].lat is None
    assert tel.samples[0].rel_alt == 33.6


def test_mercator_roundtrip_scale():
    from skystamp.maptiles import mercator

    x0, y0 = mercator(44.0, 26.0)
    x1, y1 = mercator(44.0, 26.001)
    # 0.001° lon at 44°N ≈ 80 m on the ground, stretched by 1/cos(lat) in mercator
    assert abs((x1 - x0) * 0.7193 - 80) < 1
