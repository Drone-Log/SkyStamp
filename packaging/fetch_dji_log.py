"""Download the pinned dji-log binary for this OS into packaging/vendor/ (used by CI before building)."""

from pathlib import Path

from skystamp import flightlog

target = Path(__file__).resolve().parent / "vendor" / "dji-log" / flightlog._EXE
flightlog._download(target)
print(f"dji-log {flightlog.PARSER_VERSION} -> {target}")
