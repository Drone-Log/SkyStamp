# dmgbuild settings: the SkyStamp installer window (icon -> arrow -> Applications).
# Used by packaging/make_dmg.py; `defines` provides app_path.
import os

app = defines["app_path"]  # noqa: F821 (injected by dmgbuild)

format = "UDZO"
filesystem = "HFS+"
files = [app]
symlinks = {"Applications": "/Applications"}
background = os.path.join(defines["here"], "dmg-background.png")  # noqa: F821 (@2x is picked up automatically)
show_status_bar = False
show_tab_view = False
show_toolbar = False
show_pathbar = False
show_sidebar = False
window_rect = ((200, 120), (660, 400))
default_view = "icon-view"
icon_size = 112
text_size = 13
icon_locations = {os.path.basename(app): (170, 215), "Applications": (490, 215)}
