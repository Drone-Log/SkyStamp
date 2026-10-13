"""The styles the app offers: built-ins shipped with SkyStamp plus the user's own.

User styles are ``.skystyle`` files (JSON, see ``engine.py``) in the user data
folder, so they survive updates and can be shared by copying the file.
"""

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path

from .engine import SpecStyle, normalize

BUILTIN_ORDER = ["hud", "classic", "cinematic", "minimal"]
EXT = ".skystyle"


@dataclass
class StyleDef:
    id: str
    spec: dict
    builtin: bool = True
    path: Path | None = None
    name: str = field(init=False)
    description: str = field(init=False)

    def __post_init__(self):
        self.spec = normalize(self.spec)
        self.name = self.spec["name"]
        self.description = self.spec["description"]

    def __call__(self, width, height, telemetry, options) -> SpecStyle:
        return SpecStyle(width, height, telemetry, options, self.spec)


def user_dir() -> Path:
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    elif os.name == "nt":
        base = Path(os.environ.get("APPDATA", Path.home()))
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return base / "SkyStamp" / "styles"


def builtin_styles() -> dict[str, StyleDef]:
    out = {}
    folder = resources.files("skystamp") / "assets" / "styles"
    for sid in BUILTIN_ORDER:
        out[sid] = StyleDef(sid, json.loads((folder / f"{sid}.json").read_text()))
    return out


def load_file(path: str | Path) -> StyleDef:
    path = Path(path)
    try:
        spec = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise ValueError(f"Not a valid SkyStamp style: {path.name} ({e})") from e
    if not isinstance(spec, dict) or "elements" not in spec:
        raise ValueError(f"Not a valid SkyStamp style: {path.name}")
    return StyleDef(f"user:{path.stem}", spec, builtin=False, path=path)


def user_styles() -> dict[str, StyleDef]:
    out = {}
    folder = user_dir()
    if folder.is_dir():
        for p in sorted(folder.glob(f"*{EXT}")):
            try:
                st = load_file(p)
            except ValueError:
                continue
            out[st.id] = st
    return out


def all_styles() -> dict[str, StyleDef]:
    return {**builtin_styles(), **user_styles()}


def _slug(name: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "-", name).strip("-").lower()
    return slug or "style"


def save_user_style(spec: dict, previous: StyleDef | None = None) -> StyleDef:
    """Write a user style; renames the file when the name changed."""
    spec = normalize(spec)
    folder = user_dir()
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"{_slug(spec['name'])}{EXT}"
    if previous is not None and previous.path and previous.path != target and previous.path.exists():
        previous.path.unlink()
    elif previous is None or previous.path != target:
        n = 2
        while target.exists():
            target = folder / f"{_slug(spec['name'])}-{n}{EXT}"
            n += 1
    target.write_text(json.dumps(spec, indent=2), encoding="utf-8")
    return load_file(target)


def import_style(path: str | Path) -> StyleDef:
    st = load_file(path)
    return save_user_style(st.spec)


def export_style(style: StyleDef, path: str | Path) -> None:
    Path(path).write_text(json.dumps(style.spec, indent=2), encoding="utf-8")


def delete_user_style(style: StyleDef) -> None:
    if not style.builtin and style.path and style.path.exists():
        style.path.unlink()
