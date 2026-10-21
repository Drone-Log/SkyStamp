"""ffmpeg integration: probing, preview frames and the final render."""

from __future__ import annotations

import io
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Callable

from PIL import Image

from .styles import OverlayOptions, StyleDef, get_style
from .telemetry import Telemetry

# Hide console windows spawned by subprocess on Windows
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def clock(sec: float) -> str:
    """4:05, or 1:04:05 past an hour."""
    h, rest = divmod(int(sec), 3600)
    m, s = divmod(rest, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def time_left(sec: float) -> str:
    """Rounded like a person would say it: about 1 h 5 min / 12 min / 40 s left."""
    if sec >= 3600:
        h, m = divmod(round(sec / 60), 60)
        return f"about {h} h {m} min left" if m else f"about {h} h left"
    if sec >= 90:
        return f"about {round(sec / 60)} min left"
    if sec >= 10:
        return f"about {round(sec / 10) * 10} s left"
    return "almost done"


class RenderCancelled(Exception):
    pass


@lru_cache(maxsize=1)
def ffmpeg_path() -> str:
    """Find ffmpeg: bundled with the app, $SKYSTAMP_FFMPEG, imageio-ffmpeg, then PATH."""
    exe = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
    candidates = []
    if getattr(sys, "frozen", False):
        base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
        candidates += [base / exe, base / "ffmpeg" / exe]
    if os.environ.get("SKYSTAMP_FFMPEG"):
        candidates.append(Path(os.environ["SKYSTAMP_FFMPEG"]))
    for c in candidates:
        if c.is_file():
            return str(c)
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        pass
    found = shutil.which("ffmpeg")
    if found:
        return found
    raise RuntimeError("ffmpeg not found. Install ffmpeg or the imageio-ffmpeg package.")


def _run(args: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run([ffmpeg_path(), *args], capture_output=True, stdin=subprocess.DEVNULL,
                          creationflags=_NO_WINDOW, **kw)


@lru_cache(maxsize=1)
def available_encoders() -> set[str]:
    out = _run(["-hide_banner", "-encoders"]).stdout.decode(errors="replace")
    return {m.group(1) for m in re.finditer(r"^\s*V\S*\s+(\S+)", out, re.M)}


@dataclass
class VideoInfo:
    width: int
    height: int
    fps: float
    duration: float
    bitrate_kbps: int | None
    pix_fmt: str
    has_audio: bool


def probe(path: str | Path) -> VideoInfo:
    """Read video properties from `ffmpeg -i` (ffprobe is not always shipped)."""
    err = _run(["-hide_banner", "-i", str(path)]).stderr.decode(errors="replace")
    dur = re.search(r"Duration:\s*(\d+):(\d+):([\d.]+)", err)
    if not dur:
        raise ValueError(f"Not a readable video: {path}")
    duration = int(dur.group(1)) * 3600 + int(dur.group(2)) * 60 + float(dur.group(3))
    br = re.search(r"Duration:.*bitrate:\s*(\d+)\s*kb/s", err)

    # First real video stream (skip embedded cover thumbnails)
    video = None
    for line in err.splitlines():
        if "Video:" in line and "attached pic" not in line:
            video = line
            break
    if not video:
        raise ValueError(f"No video stream in {path}")
    size = re.search(r",\s*(\d{2,5})x(\d{2,5})", video)
    fps = re.search(r"([\d.]+)\s*fps", video) or re.search(r"([\d.]+)\s*tbr", video)
    pix = re.search(r"Video:\s*[^,]+,\s*([a-z0-9_]+)", video)
    w, h = int(size.group(1)), int(size.group(2))

    # ffmpeg auto-rotates on decode, so overlay must match the displayed size
    rot = re.search(r"rotation of (-?[\d.]+) degrees", err) or re.search(r"rotate\s*:\s*(-?\d+)", err)
    if rot and round(abs(float(rot.group(1)))) % 180 == 90:
        w, h = h, w
    return VideoInfo(
        width=w,
        height=h,
        fps=float(fps.group(1)) if fps else 30.0,
        duration=duration,
        bitrate_kbps=int(br.group(1)) if br else None,
        pix_fmt=pix.group(1) if pix else "yuv420p",
        has_audio=bool(re.search(r"Stream #\d+:\d+.*Audio:", err)),
    )


def extract_frame(path: str | Path, t: float, max_width: int = 1600) -> Image.Image:
    """Grab one frame at time t, scaled down for previewing."""
    res = _run([
        "-hide_banner", "-loglevel", "error", "-ss", f"{max(0.0, t):.3f}", "-i", str(path),
        "-map", "0:v:0", "-frames:v", "1", "-vf", f"scale='min({max_width},iw)':-2",
        "-f", "image2pipe", "-vcodec", "png", "-",
    ])
    if not res.stdout:
        raise ValueError(res.stderr.decode(errors="replace").strip() or "Could not read frame")
    return Image.open(io.BytesIO(res.stdout)).convert("RGB")


def resolve_style(style) -> StyleDef:
    """A style id, a .skystyle path, a StyleDef or a raw spec dict (e.g. one being edited)."""
    if isinstance(style, StyleDef):
        return style
    if isinstance(style, dict):
        return StyleDef("custom", style, builtin=False)
    return get_style(str(style))


def preview(video: str | Path, info: VideoInfo, tel: Telemetry, style_id: str,
            opts: OverlayOptions, t: float, max_width: int = 1600) -> Image.Image:
    frame = extract_frame(video, t, max_width).convert("RGBA")
    overlay = resolve_style(style_id)(info.width, info.height, tel, opts).render(t)
    overlay = overlay.resize(frame.size, Image.LANCZOS)
    return Image.alpha_composite(frame, overlay).convert("RGB")


# --- Output formats ------------------------------------------------------

FORMATS = {
    "h264": "MP4 · H.264 (plays everywhere)",
    "hevc": "MP4 · H.265/HEVC (smaller file)",
    "overlay": "MOV · ProRes 4444 overlay only (transparent, for editors)",
}
QUALITY = {"high": 18, "medium": 22, "low": 26}  # x264/x265 CRF
QUALITY_BITRATE = {"high": 1.0, "medium": 0.6, "low": 0.35}  # × source bitrate for HW encoders


@lru_cache(maxsize=None)
def hw_encoder(fmt: str) -> str | None:
    """First hardware encoder that is compiled in *and* actually works here
    (e.g. NVENC is listed on machines without an NVIDIA GPU)."""
    enc = available_encoders()
    for name in (f"{fmt}_videotoolbox", f"{fmt}_nvenc", f"{fmt}_qsv", f"{fmt}_amf"):
        if name not in enc:
            continue
        test = _run(["-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                     "color=c=black:s=640x360:r=30", "-frames:v", "5", "-pix_fmt", "yuv420p",
                     "-c:v", name, "-f", "null", "-"], timeout=20)
        if test.returncode == 0:
            return name
    return None


def _encode_args(fmt: str, quality: str, hardware: bool, info: VideoInfo) -> list[str]:
    if fmt == "overlay":
        return ["-c:v", "prores_ks", "-profile:v", "4444", "-pix_fmt", "yuva444p10le",
                "-vendor", "apl0"]
    ten_bit = "10" in info.pix_fmt and fmt == "hevc"
    hw = hw_encoder(fmt) if hardware else None
    if hw:
        kbps = int((info.bitrate_kbps or 40000) * QUALITY_BITRATE[quality])
        args = ["-c:v", hw, "-b:v", f"{kbps}k", "-maxrate", f"{int(kbps * 1.5)}k",
                "-bufsize", f"{kbps * 2}k"]
        args += ["-pix_fmt", "p010le" if ten_bit else ("nv12" if "qsv" in hw else "yuv420p")]
    else:
        codec = "libx264" if fmt == "h264" else "libx265"
        args = ["-c:v", codec, "-crf", str(QUALITY[quality]), "-preset", "medium",
                "-pix_fmt", "yuv420p10le" if ten_bit else "yuv420p"]
        if codec == "libx265":
            args += ["-x265-params", "log-level=error"]
    if fmt == "hevc":
        args += ["-tag:v", "hvc1"]  # so QuickTime / iOS play it
    return args


def _render_once(
    video: str | Path,
    tel: Telemetry,
    output: str | Path,
    style_id: str = "hud",
    opts: OverlayOptions | None = None,
    fmt: str = "h264",
    quality: str = "high",
    hardware: bool = True,
    overlay_fps: float = 10.0,
    time_offset: float = 0.0,
    progress: Callable[[float, str], None] | None = None,
    cancel: threading.Event | None = None,
) -> None:
    """Burn the telemetry overlay into ``video`` and write ``output``.

    Overlay frames are drawn in Python and streamed to ffmpeg as raw RGBA on
    stdin; ffmpeg composites them over the decoded video and encodes.
    ``overlay_fps`` is how often the data refreshes (numbers don't need 60 fps).
    """
    opts = opts or OverlayOptions()
    info = probe(video)
    style = resolve_style(style_id)(info.width, info.height, tel, opts)
    rate = min(overlay_fps, info.fps) if fmt != "overlay" else info.fps
    n_frames = max(1, int(info.duration * rate + 0.999))
    progress = progress or (lambda p, msg: None)

    overlay_in = ["-f", "rawvideo", "-pix_fmt", "rgba", "-s", f"{info.width}x{info.height}",
                  "-framerate", f"{rate:g}", "-i", "pipe:0"]
    if fmt == "overlay":
        cmd = ["-y", "-hide_banner", "-nostats", "-loglevel", "error", "-progress", "pipe:1",
               *overlay_in, "-r", f"{info.fps:.3f}"]
    else:
        cmd = ["-y", "-hide_banner", "-nostats", "-loglevel", "error", "-progress", "pipe:1",
               "-i", str(video), *overlay_in,
               "-filter_complex", "[0:v:0][1:v]overlay=0:0:eof_action=repeat:format=auto[v]",
               "-map", "[v]", "-map", "0:a?", "-c:a", "copy",
               "-map_metadata", "0", "-movflags", "+faststart"]
    cmd += _encode_args(fmt, quality, hardware, info)
    cmd += [str(output)]

    proc = subprocess.Popen([ffmpeg_path(), *cmd], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, creationflags=_NO_WINDOW)
    errors: list[str] = []
    encoded = [0.0]

    def read_progress():
        for raw in proc.stdout:
            line = raw.decode(errors="replace").strip()
            if line.startswith("out_time_us=") or line.startswith("out_time_ms="):
                try:
                    encoded[0] = int(line.split("=", 1)[1]) / 1_000_000
                except ValueError:
                    pass

    def read_errors():
        for raw in proc.stderr:
            errors.append(raw.decode(errors="replace").rstrip())

    threads = [threading.Thread(target=read_progress, daemon=True),
               threading.Thread(target=read_errors, daemon=True)]
    for th in threads:
        th.start()

    started = time.monotonic()
    try:
        for i in range(n_frames):
            if cancel is not None and cancel.is_set():
                raise RenderCancelled()
            t = i / rate
            img = style.render(t + time_offset)
            proc.stdin.write(img.tobytes())
            done = encoded[0] / max(info.duration, 0.001)
            msg = f"Rendering {clock(t)} / {clock(info.duration)}"
            elapsed = time.monotonic() - started
            if elapsed >= 3 and i >= n_frames // 50:  # too early, the estimate jumps around
                msg += f" · {time_left(elapsed / (i + 1) * (n_frames - i - 1))}"
            progress(min(done, 0.99), msg)
        proc.stdin.close()
        while proc.poll() is None:
            if cancel is not None and cancel.is_set():
                raise RenderCancelled()
            progress(min(encoded[0] / max(info.duration, 0.001), 0.99), "Finishing encode…")
            try:
                proc.wait(timeout=0.25)
            except subprocess.TimeoutExpired:
                pass
    except (BrokenPipeError, OSError) as e:
        proc.wait()
        if isinstance(e, RenderCancelled):
            raise
        raise RuntimeError("ffmpeg stopped unexpectedly:\n" + "\n".join(errors[-15:])) from e
    except RenderCancelled:
        proc.kill()
        proc.wait()
        Path(output).unlink(missing_ok=True)
        raise
    for th in threads:
        th.join(timeout=2)
    if proc.returncode != 0:
        raise RuntimeError("ffmpeg failed:\n" + "\n".join(errors[-15:]))
    progress(1.0, "Done")


def render(video, tel, output, style_id="hud", opts=None, fmt="h264", quality="high",
           hardware=True, overlay_fps=10.0, time_offset=0.0, progress=None, cancel=None) -> None:
    """Render; if the hardware encoder fails, retry once in software."""
    args = dict(video=video, tel=tel, output=output, style_id=style_id, opts=opts, fmt=fmt,
                quality=quality, overlay_fps=overlay_fps, time_offset=time_offset,
                progress=progress, cancel=cancel)
    use_hw = hardware and fmt != "overlay" and hw_encoder(fmt) is not None
    try:
        _render_once(hardware=use_hw, **args)
    except RuntimeError:
        if not use_hw:
            raise
        if progress:
            progress(0.0, "Hardware encoder failed, retrying in software…")
        _render_once(hardware=False, **args)
