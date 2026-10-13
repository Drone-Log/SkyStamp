from .base import DEFAULT_FIELDS, FIELDS, POSITIONS, OverlayOptions, Style
from .cinematic import CinematicStyle
from .classic import ClassicStyle
from .hud import HudStyle
from .minimal import MinimalStyle

STYLES: dict[str, type[Style]] = {s.id: s for s in (HudStyle, ClassicStyle, CinematicStyle, MinimalStyle)}

__all__ = ["STYLES", "FIELDS", "DEFAULT_FIELDS", "POSITIONS", "OverlayOptions", "Style"]
