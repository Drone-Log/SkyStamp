from .base import DEFAULT_FIELDS, FIELDS, MAP_VIEWS, POSITIONS, OverlayOptions, Style
from .engine import SpecStyle
from .library import StyleDef, all_styles, builtin_styles, load_file

# Built-in styles, by id. The app also offers the user's own styles (see library.all_styles()).
STYLES: dict[str, StyleDef] = builtin_styles()


def get_style(style_id: str) -> StyleDef:
    """A style by id (built-in or "user:<name>") or by path to a .skystyle file."""
    styles = all_styles()
    if style_id in styles:
        return styles[style_id]
    return load_file(style_id)


__all__ = ["STYLES", "FIELDS", "DEFAULT_FIELDS", "MAP_VIEWS", "POSITIONS", "OverlayOptions", "Style", "SpecStyle",
           "StyleDef", "all_styles", "get_style"]
