"""Value formatting for metric / imperial display."""

from __future__ import annotations

M_TO_FT = 3.28084
MS_TO_KMH = 3.6
MS_TO_MPH = 2.236936

COMPASS = ("N", "NE", "E", "SE", "S", "SW", "W", "NW")


def altitude(m: float | None, units: str) -> tuple[str, str]:
    if m is None:
        return "--", "m" if units == "metric" else "ft"
    if units == "imperial":
        return f"{m * M_TO_FT:.0f}", "ft"
    return f"{m:.1f}", "m"


def distance(m: float, units: str) -> tuple[str, str]:
    if units == "imperial":
        ft = m * M_TO_FT
        if ft >= 5280:
            return f"{ft / 5280:.2f}", "mi"
        return f"{ft:.0f}", "ft"
    if m >= 1000:
        return f"{m / 1000:.2f}", "km"
    return f"{m:.0f}", "m"


def speed(ms: float, units: str) -> tuple[str, str]:
    if units == "imperial":
        return f"{ms * MS_TO_MPH:.1f}", "mph"
    return f"{ms * MS_TO_KMH:.1f}", "km/h"


def vspeed(ms: float, units: str) -> tuple[str, str]:
    if abs(ms) < 0.05:
        ms = 0.0
    if units == "imperial":
        return f"{ms * M_TO_FT:+.1f}", "ft/s"
    return f"{ms:+.1f}", "m/s"


def heading(deg: float | None) -> tuple[str, str]:
    if deg is None:
        return "--", ""
    return f"{deg:.0f}°", COMPASS[int((deg + 22.5) // 45) % 8]


def coords(lat: float | None, lon: float | None) -> str:
    if lat is None or lon is None:
        return "No GPS"
    ns = "N" if lat >= 0 else "S"
    ew = "E" if lon >= 0 else "W"
    return f"{abs(lat):.6f}° {ns}  {abs(lon):.6f}° {ew}"


def shutter(value: str | None) -> str:
    if not value:
        return "--"
    if "/" in value:
        num, den = value.split("/", 1)
        try:
            return f"{num}/{float(den):g}"
        except ValueError:
            return value
    return value
