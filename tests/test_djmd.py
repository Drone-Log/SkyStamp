import struct

from skystamp.djmd import h_speed, parse_bytes


def _v(n):
    out = b""
    while True:
        b = n & 0x7F
        n >>= 7
        out += bytes([b | (0x80 if n else 0)])
        if not n:
            return out


def _f(field, payload):  # length-delimited
    return _v(field << 3 | 2) + _v(len(payload)) + payload


def _i(field, n):  # varint
    return _v(field << 3) + _v(n)


def _fl(field, x):  # float32
    return _v(field << 3 | 5) + struct.pack("<f", x)


def _packet(ts_us, fix, vn, ve, vd):
    drone = (_f(1, _i(6, ts_us))
             + _f(2, _fl(1, vn) + _fl(2, ve) + _fl(3, vd))
             + _f(4, _i(4 if fix else 3, 1)))
    return _f(1, _i(2, 1)) + _f(3, _f(4, drone))


def test_parses_concatenated_packets():
    data = _packet(5_000_000, True, 3.0, 4.0, -1.0) + _packet(5_500_000, False, 0, 0, 0)
    a, b = parse_bytes(data)
    assert a.t == 0 and b.t == 0.5
    assert a.gps_fix is True and b.gps_fix is False
    assert abs(h_speed(a) - 5.0) < 1e-6 and a.vel_d == -1.0


def test_garbage_is_harmless():
    assert parse_bytes(b"\xff\xff\xff") == []
