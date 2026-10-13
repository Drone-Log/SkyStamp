"""Download the pinned dji-log binary for this OS into packaging/vendor/ (used by CI before building)."""

import time
from pathlib import Path

from skystamp import flightlog

target = Path(__file__).resolve().parent / "vendor" / "dji-log" / flightlog._EXE
for attempt in range(1, 6):  # CI runners sometimes time out talking to GitHub
    try:
        flightlog._download(target)
        break
    except flightlog.FlightLogError as e:
        if attempt == 5:
            raise
        print(f"attempt {attempt} failed ({e}), retrying…")
        time.sleep(10 * attempt)
print(f"dji-log {flightlog.PARSER_VERSION} -> {target}")
