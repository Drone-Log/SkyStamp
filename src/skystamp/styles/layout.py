"""Editor geometry: anchors and snapping. Pure functions, no Qt, so they test anywhere."""

from __future__ import annotations

SNAP_PX = 14  # snap distance, in preview pixels


def anchor_for(cx: float, cy: float, w: float, h: float) -> str:
    """Which corner/edge an element should grow from, by where its centre is."""
    col = "l" if cx < w / 3 else "r" if cx > 2 * w / 3 else ""
    row = "t" if cy < h / 3 else "b" if cy > 2 * h / 3 else ""
    return {("t", "l"): "tl", ("t", ""): "t", ("t", "r"): "tr", ("", "l"): "l", ("", ""): "c",
            ("", "r"): "r", ("b", "l"): "bl", ("b", ""): "b", ("b", "r"): "br"}[(row, col)]


def move_element(el: dict, box: tuple[float, float, float, float], frame_w: int, frame_h: int,
                 margin: int) -> tuple[dict, list[tuple[str, float]]]:
    """Place ``el`` so its rectangle lands on ``box`` (frame pixels).

    Picks the anchor from the box position, converts to safe-area fractions and
    snaps to the edges and centre lines. Returns the updated element and the
    guide lines to draw: ("v", x) / ("h", y) in frame pixels.
    """
    x0, y0, x1, y1 = box
    a = anchor_for((x0 + x1) / 2, (y0 + y1) / 2, frame_w, frame_h)
    ax = 0.0 if a in ("tl", "l", "bl") else 1.0 if a in ("tr", "r", "br") else 0.5
    ay = 0.0 if a in ("tl", "t", "tr") else 1.0 if a in ("bl", "b", "br") else 0.5
    X, Y = x0 + ax * (x1 - x0), y0 + ay * (y1 - y0)
    sw, sh = max(1, frame_w - 2 * margin), max(1, frame_h - 2 * margin)
    fx, fy = (X - margin) / sw, (Y - margin) / sh
    guides: list[tuple[str, float]] = []
    for target in (0.0, 0.5, 1.0):
        if abs(fx - target) * sw <= SNAP_PX and ax == target:
            fx = target
            guides.append(("v", margin + target * sw))
        if abs(fy - target) * sh <= SNAP_PX and ay == target:
            fy = target
            guides.append(("h", margin + target * sh))
    out = dict(el, anchor=a, x=round(min(1.0, max(0.0, fx)), 4), y=round(min(1.0, max(0.0, fy)), 4))
    out.pop("position_option", None)
    return out, guides


