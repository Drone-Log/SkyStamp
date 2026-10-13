from datetime import datetime, timedelta, timezone
from pathlib import Path

from skystamp import flightlog as fl
from skystamp.telemetry import Sample, Telemetry


def _log(start_utc: datetime, seconds: int = 120) -> fl.FlightLog:
    frames = []
    for i in range(seconds * 10):  # 10 Hz like DJI Fly
        t = start_utc + timedelta(milliseconds=100 * i)
        frames.append({"t": t.isoformat().replace("+00:00", "Z"), "sats": 20 + (i // 100) % 10, "lvl": 5,
                       "used": True, "bat": 80 - i / 100, "h": min(50.0, i / 10), "lat": 44.0, "lon": 26.0,
                       "hlat": 44.0, "hlon": 26.0})
    return fl._to_log(Path("FlightRecord_x.txt"), {"details": {"aircraftName": "DJI Neo 2"}, "frames": frames})


def _tel(start_local: datetime, from_s: int, to_s: int) -> Telemetry:
    samples = []
    for k, sec in enumerate(range(from_s, to_s)):
        samples.append(Sample(t=float(k), timestamp=start_local + timedelta(seconds=sec), rel_alt=min(50.0, sec)))
    return Telemetry(samples=samples)


def test_name_parsing_and_find(tmp_path):
    (tmp_path / "FlightRecord_2026-10-10_[16-42-38].txt").write_text("x")
    (tmp_path / "DJIFlightRecord_2026-10-10_[15-00-00].txt").write_text("x")
    assert fl.log_start_from_name(Path("DJIFlightRecord_2026-10-10_[15-00-00].txt")) == datetime(2026, 10, 10, 15)
    found = fl.find_log(datetime(2026, 10, 10, 16, 43, 32), [tmp_path])
    assert found.name == "FlightRecord_2026-10-10_[16-42-38].txt"
    assert fl.find_log(datetime(2026, 10, 11, 9, 0), [tmp_path]) is None


def test_offset_detection_and_attach():
    log = _log(datetime(2026, 10, 10, 13, 42, 38, tzinfo=timezone.utc))
    # Video recorded 20 s into the flight, SRT clock is UTC+3
    tel = _tel(datetime(2026, 10, 10, 16, 42, 38), 20, 80)
    assert fl.utc_offset(tel, log) == timedelta(hours=3)
    assert fl.attach(tel, log)
    s = tel.samples[0]
    assert s.satellites == 22 and abs(s.battery - 78) < 0.1


def test_wrong_flight_does_not_attach():
    log = _log(datetime(2026, 10, 10, 13, 42, 38, tzinfo=timezone.utc))
    tel = _tel(datetime(2026, 10, 12, 10, 0, 0), 0, 30)
    assert not fl.attach(tel, log)
    assert tel.samples[0].satellites is None


def test_zero_time_frames_are_skipped():
    log = fl._to_log(Path("x.txt"), {"frames": [{"t": "1970-01-01T00:00:00Z", "sats": 5},
                                                {"t": "2026-10-10T13:42:38.645Z", "sats": 13}]})
    assert len(log.frames) == 1 and log.frames[0].satellites == 13
