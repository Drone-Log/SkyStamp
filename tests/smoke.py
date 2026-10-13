"""End-to-end smoke test used in CI: synthesize a tiny video + SRT, render every
style with the bundled ffmpeg, and make sure the GUI module imports."""

import sys
import tempfile
from pathlib import Path

from skystamp.styles import STYLES, OverlayOptions
from skystamp.telemetry import parse_srt
from skystamp.video import _run, probe, render

SRT_BLOCK = """{i}
00:00:{s:02d},{ms:03d} --> 00:00:{s2:02d},{ms2:03d}
<font size="28">FrameCnt: {i}, DiffTime: 100ms
2026-10-10 16:43:{s:02d}.{ms:03d}
[iso: 100] [shutter: 1/1000.0] [fnum: 2.2] [ev: 0] [latitude: {lat:.6f}] [longitude: 26.154778] [rel_alt: {alt:.3f} abs_alt: 100.000]</font>
"""


def main() -> int:
    tmp = Path(tempfile.mkdtemp())
    video, srt = tmp / "clip.mp4", tmp / "clip.SRT"
    res = _run(["-y", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=640x360:rate=10:duration=2",
                "-pix_fmt", "yuv420p", str(video)])
    assert res.returncode == 0, res.stderr.decode()
    blocks = []
    for i in range(20):
        t, t2 = i * 100, (i + 1) * 100
        blocks.append(SRT_BLOCK.format(i=i + 1, s=t // 1000, ms=t % 1000, s2=t2 // 1000, ms2=t2 % 1000,
                                       lat=44.378130 + i * 0.00001, alt=10 + i * 0.5))
    srt.write_text("\n".join(blocks))

    tel = parse_srt(srt)
    assert len(tel.samples) == 20 and tel.max_speed > 0
    info = probe(video)
    assert (info.width, info.height) == (640, 360)

    for sid in STYLES:
        out = tmp / f"out_{sid}.mp4"
        render(video, tel, out, sid, OverlayOptions(map_tiles="none"), hardware=False)
        assert out.stat().st_size > 1000, sid
        print(f"rendered {sid}: {out.stat().st_size} bytes")

    import skystamp.gui  # noqa: F401  (catches broken imports / Qt packaging issues)

    print("smoke test OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
