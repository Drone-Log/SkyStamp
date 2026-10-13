"""Build the macOS installer disk image (icon, arrow, Applications folder).

    python packaging/make_dmg.py dist/SkyStamp.app SkyStamp.dmg
"""

import sys
from pathlib import Path

import dmgbuild

here = Path(__file__).resolve().parent
app, out = Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve()
dmgbuild.build_dmg(str(out), "SkyStamp", str(here / "dmg_settings.py"),
                   defines={"app_path": str(app), "here": str(here)})
print(f"dmg -> {out}")
