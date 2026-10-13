"""SkyStamp desktop app: pick a video + SRT, choose a style, render."""

from __future__ import annotations

import os
import subprocess
import sys
import threading
from pathlib import Path

from PIL.ImageQt import ImageQt
from PySide6.QtCore import QObject, QRunnable, QSettings, Qt, QThread, QThreadPool, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices, QIcon, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QColorDialog,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from . import __version__
from .maptiles import PROVIDERS
from .styles import DEFAULT_FIELDS, FIELDS, POSITIONS, STYLES, OverlayOptions
from .djmd import attach_video_meta
from .telemetry import Telemetry, parse_srt
from .video import FORMATS, RenderCancelled, VideoInfo, extract_frame, hw_encoder, probe, render

VIDEO_FILTER = "Videos (*.mp4 *.MP4 *.mov *.MOV);;All files (*)"
SRT_FILTER = "DJI telemetry (*.srt *.SRT);;All files (*)"


def find_srt(video: Path) -> Path | None:
    for ext in (".SRT", ".srt"):
        p = video.with_suffix(ext)
        if p.exists():
            return p
    return None


def fmt_time(sec: float) -> str:
    m, s = divmod(int(sec), 60)
    return f"{m}:{s:02d}"


# --- background jobs -------------------------------------------------------

class _Signals(QObject):
    done = Signal(object, object)  # (request id, result or Exception)


class _Job(QRunnable):
    def __init__(self, req, fn):
        super().__init__()
        self.req, self.fn, self.signals = req, fn, _Signals()

    def run(self):
        try:
            res = self.fn()
        except Exception as e:  # noqa: BLE001 - surfaced in the UI
            res = e
        self.signals.done.emit(self.req, res)


class RenderThread(QThread):
    progressed = Signal(float, str)
    finished_ok = Signal(str)
    failed = Signal(str)
    cancelled = Signal()

    def __init__(self, kwargs: dict):
        super().__init__()
        self.kwargs = kwargs
        self.cancel_event = threading.Event()

    def run(self):
        try:
            render(**self.kwargs, cancel=self.cancel_event,
                   progress=lambda p, m: self.progressed.emit(p, m))
            self.finished_ok.emit(str(self.kwargs["output"]))
        except RenderCancelled:
            self.cancelled.emit()
        except Exception as e:  # noqa: BLE001
            self.failed.emit(str(e))


# --- widgets ---------------------------------------------------------------

class PreviewLabel(QLabel):
    """Keeps the preview image scaled to the available space."""

    def __init__(self):
        super().__init__("Drop a DJI video here\nor use “Choose video…”")
        self.setAlignment(Qt.AlignCenter)
        self.setMinimumSize(480, 270)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setStyleSheet("background:#111; color:#888; border-radius:8px; font-size:15px;")
        self._pix: QPixmap | None = None

    def set_image(self, pix: QPixmap | None):
        self._pix = pix
        self._update()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._update()

    def _update(self):
        if self._pix is not None:
            self.setPixmap(self._pix.scaled(self.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation))


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"SkyStamp {__version__}")
        self.setAcceptDrops(True)
        self.resize(1360, 820)
        self.settings = QSettings("SkyStamp", "SkyStamp")

        self.video: Path | None = None
        self.info: VideoInfo | None = None
        self.tel: Telemetry | None = None
        self.frame_cache: dict[float, object] = {}
        self.preview_req = 0
        self.render_thread: RenderThread | None = None
        self.pool = QThreadPool.globalInstance()
        self.accent = self.settings.value("accent", "#38BDF8")

        self.preview_timer = QTimer(self, singleShot=True, interval=120)
        self.preview_timer.timeout.connect(self.refresh_preview)

        self._build()
        self._load_settings()
        self._update_enabled()

    # -- layout ------------------------------------------------------------
    def _build(self):
        root = QWidget()
        self.setCentralWidget(root)
        outer = QHBoxLayout(root)

        # Left: preview + timeline
        left = QVBoxLayout()
        self.preview = PreviewLabel()
        left.addWidget(self.preview, 1)
        tl = QHBoxLayout()
        self.slider = QSlider(Qt.Horizontal)
        self.slider.setRange(0, 0)
        self.slider.valueChanged.connect(self._schedule_preview)
        self.time_label = QLabel("0:00 / 0:00")
        tl.addWidget(self.slider, 1)
        tl.addWidget(self.time_label)
        left.addLayout(tl)
        self.info_label = QLabel("")
        self.info_label.setStyleSheet("color:#888;")
        left.addWidget(self.info_label)
        outer.addLayout(left, 1)

        # Right: settings (scrollable for small screens)
        panel = QWidget()
        side = QVBoxLayout(panel)
        side.setContentsMargins(0, 0, 8, 0)

        files = QGroupBox("1 · Files")
        fl = QFormLayout(files)
        self.video_edit = QLineEdit(readOnly=True, placeholderText="No video selected")
        b = QPushButton("Choose video…")
        b.clicked.connect(self.choose_video)
        fl.addRow(self.video_edit)
        fl.addRow(b)
        self.srt_edit = QLineEdit(readOnly=True, placeholderText="Found automatically next to the video")
        b2 = QPushButton("Choose SRT…")
        b2.clicked.connect(self.choose_srt)
        fl.addRow(self.srt_edit)
        fl.addRow(b2)
        side.addWidget(files)

        style = QGroupBox("2 · Style")
        sl = QFormLayout(style)
        self.style_combo = QComboBox()
        for sid, cls in STYLES.items():
            self.style_combo.addItem(cls.name, sid)
        self.style_combo.currentIndexChanged.connect(self._style_changed)
        self.style_desc = QLabel(wordWrap=True)
        self.style_desc.setMinimumWidth(1)
        self.style_desc.setStyleSheet("color:#888;")
        sl.addRow("Style", self.style_combo)
        sl.addRow(self.style_desc)
        self.units_combo = QComboBox()
        self.units_combo.addItem("Metric (m, km/h)", "metric")
        self.units_combo.addItem("Imperial (ft, mph)", "imperial")
        self.units_combo.currentIndexChanged.connect(self._schedule_preview)
        sl.addRow("Units", self.units_combo)
        self.position_combo = QComboBox()
        for p in POSITIONS:
            self.position_combo.addItem(p.replace("-", " ").capitalize(), p)
        self.position_combo.currentIndexChanged.connect(self._schedule_preview)
        sl.addRow("Position", self.position_combo)
        self.size_slider = QSlider(Qt.Horizontal, minimum=60, maximum=180, value=100)
        self.size_slider.valueChanged.connect(self._schedule_preview)
        sl.addRow("Text size", self.size_slider)
        self.color_btn = QPushButton()
        self.color_btn.clicked.connect(self.choose_color)
        sl.addRow("Accent color", self.color_btn)
        grid = QGridLayout()
        self.field_checks: dict[str, QCheckBox] = {}
        for i, (key, label) in enumerate(FIELDS.items()):
            cb = QCheckBox(label.replace("&", "&&"))
            cb.toggled.connect(self._schedule_preview)
            self.field_checks[key] = cb
            grid.addWidget(cb, i // 2, i % 2)
        sl.addRow(grid)
        self.map_combo = QComboBox()
        for k, prov in PROVIDERS.items():
            self.map_combo.addItem(prov.name, k)
        self.map_combo.addItem("None (path only)", "none")
        self.map_combo.setToolTip("Map tiles are downloaded once per video (internet needed) and cached.")
        self.map_combo.currentIndexChanged.connect(self._schedule_preview)
        self.field_checks["map"].toggled.connect(lambda on: self.map_combo.setEnabled(on))
        sl.addRow("Map background", self.map_combo)
        side.addWidget(style)

        out = QGroupBox("3 · Output")
        ol = QFormLayout(out)
        self.format_combo = QComboBox()
        for k, v in FORMATS.items():
            self.format_combo.addItem(v, k)
        self.format_combo.currentIndexChanged.connect(self._format_changed)
        ol.addRow("Format", self.format_combo)
        self.quality_combo = QComboBox()
        self.quality_combo.addItem("High", "high")
        self.quality_combo.addItem("Medium", "medium")
        self.quality_combo.addItem("Small file", "low")
        ol.addRow("Quality", self.quality_combo)
        self.rate_combo = QComboBox()
        for r in (5, 10, 15, 30):
            self.rate_combo.addItem(f"{r} times / second", r)
        ol.addRow("Data refresh", self.rate_combo)
        self.hw_check = QCheckBox("Use hardware encoder (much faster)")
        ol.addRow(self.hw_check)
        self.output_edit = QLineEdit(placeholderText="Where to save the final video")
        ob = QPushButton("Save as…")
        ob.clicked.connect(self.choose_output)
        ol.addRow(self.output_edit)
        ol.addRow(ob)
        side.addWidget(out)

        self.render_btn = QPushButton("Create video")
        self.render_btn.setMinimumHeight(44)
        self.render_btn.setStyleSheet("font-size:16px; font-weight:600;")
        self.render_btn.clicked.connect(self.start_render)
        side.addWidget(self.render_btn)
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.clicked.connect(self.cancel_render)
        self.cancel_btn.hide()
        side.addWidget(self.cancel_btn)
        self.progress = QProgressBar(textVisible=True)
        self.progress.hide()
        side.addWidget(self.progress)
        self.status = QLabel(wordWrap=True)
        side.addWidget(self.status)
        self.open_btn = QPushButton("Show in folder")
        self.open_btn.clicked.connect(self.reveal_output)
        self.open_btn.hide()
        side.addWidget(self.open_btn)
        side.addStretch(1)

        # Long combo entries / file paths must shrink, never widen the panel
        for combo in panel.findChildren(QComboBox):
            combo.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
            combo.setMinimumContentsLength(8)
            combo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        for edit in panel.findChildren(QLineEdit):
            edit.setMinimumWidth(0)
        for form in panel.findChildren(QFormLayout):
            # macOS keeps fields at their size hint by default; let them fill the panel
            form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        scroll = QScrollArea(widgetResizable=True)
        scroll.setWidget(panel)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setFixedWidth(430)
        scroll.setFrameShape(QScrollArea.NoFrame)
        outer.addWidget(scroll)

    # -- settings ----------------------------------------------------------
    def _load_settings(self):
        s = self.settings
        self._set_combo(self.style_combo, s.value("style", "hud"))
        self._set_combo(self.units_combo, s.value("units", "metric"))
        self._set_combo(self.map_combo, s.value("map_tiles", "osm"))
        self._set_combo(self.position_combo, s.value("position", "bottom-left"))
        self._set_combo(self.format_combo, s.value("format", "h264"))
        self._set_combo(self.quality_combo, s.value("quality", "high"))
        self._set_combo(self.rate_combo, int(s.value("rate", 10)))
        self.size_slider.setValue(int(s.value("size", 100)))
        fields = s.value("fields", OverlayOptions().fields)
        if isinstance(fields, str):
            fields = [fields]
        # Fields added in a newer version start with their default state
        known = s.value("known_fields", [k for k in FIELDS if k != "gps_status"])  # 0.1.0 had no list
        known = [known] if isinstance(known, str) else known
        fields = list(fields) + [k for k in DEFAULT_FIELDS if k not in known]
        for k, cb in self.field_checks.items():
            cb.setChecked(k in fields)
        has_hw = hw_encoder("h264") is not None
        self.hw_check.setEnabled(has_hw)
        self.hw_check.setChecked(has_hw and s.value("hardware", "true") in (True, "true"))
        self._paint_color()
        self._style_changed()

    def _save_settings(self):
        s = self.settings
        s.setValue("style", self.style_combo.currentData())
        s.setValue("units", self.units_combo.currentData())
        s.setValue("map_tiles", self.map_combo.currentData())
        s.setValue("position", self.position_combo.currentData())
        s.setValue("format", self.format_combo.currentData())
        s.setValue("quality", self.quality_combo.currentData())
        s.setValue("rate", self.rate_combo.currentData())
        s.setValue("size", self.size_slider.value())
        s.setValue("fields", self.options().fields)
        s.setValue("known_fields", list(FIELDS))
        s.setValue("hardware", self.hw_check.isChecked())
        s.setValue("accent", self.accent)

    @staticmethod
    def _set_combo(combo: QComboBox, data):
        i = combo.findData(data)
        if i >= 0:
            combo.setCurrentIndex(i)

    def options(self) -> OverlayOptions:
        return OverlayOptions(
            units=self.units_combo.currentData(),
            fields=[k for k, cb in self.field_checks.items() if cb.isChecked()],
            scale=self.size_slider.value() / 100,
            accent=self.accent,
            position=self.position_combo.currentData(),
            map_tiles=self.map_combo.currentData(),
        )

    # -- file selection ----------------------------------------------------
    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e):
        for url in e.mimeData().urls():
            p = Path(url.toLocalFile())
            if p.suffix.lower() == ".srt":
                self.load_srt(p)
            elif p.suffix.lower() in (".mp4", ".mov"):
                self.load_video(p)

    def choose_video(self):
        start = self.settings.value("last_dir", str(Path.home()))
        path, _ = QFileDialog.getOpenFileName(self, "Choose drone video", start, VIDEO_FILTER)
        if path:
            self.load_video(Path(path))

    def choose_srt(self):
        start = str(self.video.parent) if self.video else self.settings.value("last_dir", str(Path.home()))
        path, _ = QFileDialog.getOpenFileName(self, "Choose SRT telemetry", start, SRT_FILTER)
        if path:
            self.load_srt(Path(path))

    def load_video(self, path: Path):
        try:
            info = probe(path)
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "Can't open video", str(e))
            return
        self.video, self.info = path, info
        self.frame_cache.clear()
        self.settings.setValue("last_dir", str(path.parent))
        self.video_edit.setText(str(path))
        self.slider.setRange(0, int(info.duration * 10))
        self.slider.setValue(min(int(info.duration * 10) // 3, 300))
        ext = ".mov" if self.format_combo.currentData() == "overlay" else ".mp4"
        out_dir = Path(self.settings.value("out_dir", str(path.parent)))
        if not out_dir.is_dir():
            out_dir = path.parent
        self.output_edit.setText(str(out_dir / f"{path.stem}_skystamp{ext}"))
        srt = find_srt(path)
        if srt:
            self.load_srt(srt)
        else:
            self.tel = None
            self.srt_edit.clear()
            self.status.setText("No SRT found next to the video. Choose it manually.")
        self._update_info()
        self._update_enabled()
        self._schedule_preview()

    def load_srt(self, path: Path):
        try:
            tel = parse_srt(path)
            if not tel.samples:
                raise ValueError("No telemetry found in this file.")
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "Can't read SRT", str(e))
            return
        self.tel = tel
        self.meta_ok = bool(self.video) and attach_video_meta(tel, self.video)
        self.srt_edit.setText(str(path))
        self.status.setText("")
        self._update_info()
        self._update_enabled()
        self._schedule_preview()

    def _update_info(self):
        parts = []
        if self.info:
            i = self.info
            parts.append(f"{i.width}×{i.height} · {i.fps:g} fps · {fmt_time(i.duration)}")
        if self.tel:
            t = self.tel
            gps = "GPS ✓" if t.has_gps else "no GPS fix in this clip"
            if getattr(self, "meta_ok", False):
                gps += " · speed measured by drone"
            parts.append(f"{len(t.samples)} telemetry points · {gps} · max alt {t.max_alt:.0f} m · "
                         f"max speed {t.max_speed * 3.6:.0f} km/h")
            if self.info and abs(t.duration - self.info.duration) > 2:
                parts.append("⚠ SRT and video lengths differ. Is this the right SRT?")
        self.info_label.setText("   ·   ".join(parts))

    def choose_output(self):
        fmt = self.format_combo.currentData()
        filt = "QuickTime (*.mov)" if fmt == "overlay" else "MP4 video (*.mp4)"
        start = self.output_edit.text() or str(Path.home())
        path, _ = QFileDialog.getSaveFileName(self, "Save final video as", start, filt)
        if path:
            p = Path(path)
            want = ".mov" if fmt == "overlay" else ".mp4"
            if p.suffix.lower() != want:
                p = p.with_suffix(want)
            self.output_edit.setText(str(p))
            self.settings.setValue("out_dir", str(p.parent))

    def choose_color(self):
        c = QColorDialog.getColor(QColor(self.accent), self, "Accent color")
        if c.isValid():
            self.accent = c.name()
            self._paint_color()
            self._schedule_preview()

    def _paint_color(self):
        self.color_btn.setText(self.accent.upper())
        self.color_btn.setStyleSheet(f"background:{self.accent}; color:#000; font-weight:600;")

    def _style_changed(self):
        sid = self.style_combo.currentData()
        self.style_desc.setText(STYLES[sid].description)
        self.position_combo.setEnabled(sid == "minimal")
        self._schedule_preview()

    def _format_changed(self):
        fmt = self.format_combo.currentData()
        is_overlay = fmt == "overlay"
        self.quality_combo.setEnabled(not is_overlay)
        self.rate_combo.setEnabled(not is_overlay)
        self.hw_check.setEnabled(not is_overlay and hw_encoder(fmt) is not None)
        text = self.output_edit.text()
        if text:
            self.output_edit.setText(str(Path(text).with_suffix(".mov" if is_overlay else ".mp4")))

    def _update_enabled(self):
        ready = self.video is not None and self.tel is not None
        busy = self.render_thread is not None
        self.render_btn.setEnabled(ready and not busy)

    # -- preview -----------------------------------------------------------
    def _schedule_preview(self, *_):
        if self.info:
            t = self.slider.value() / 10
            self.time_label.setText(f"{fmt_time(t)} / {fmt_time(self.info.duration)}")
        self.preview_timer.start()

    def refresh_preview(self):
        if not self.video or not self.info:
            return
        self.preview_req += 1
        req, video, info, tel = self.preview_req, self.video, self.info, self.tel
        t = self.slider.value() / 10
        opts, sid = self.options(), self.style_combo.currentData()
        cache = self.frame_cache

        def work():
            from PIL import Image

            frame = cache.get(t)
            if frame is None:
                frame = extract_frame(video, t, 1600).convert("RGBA")
                if len(cache) > 30:
                    cache.clear()
                cache[t] = frame
            if tel is None:
                return frame.convert("RGB")
            overlay = STYLES[sid](info.width, info.height, tel, opts).render(t)
            overlay = overlay.resize(frame.size, Image.LANCZOS)
            return Image.alpha_composite(frame, overlay).convert("RGB")

        job = _Job(req, work)
        job.signals.done.connect(self._preview_done)
        self.pool.start(job)

    def _preview_done(self, req, result):
        if req != self.preview_req:
            return  # a newer preview is on its way
        if isinstance(result, Exception):
            self.status.setText(f"Preview failed: {result}")
            return
        self.preview.set_image(QPixmap.fromImage(ImageQt(result)))

    # -- render ------------------------------------------------------------
    def start_render(self):
        out = self.output_edit.text().strip()
        if not out:
            self.choose_output()
            out = self.output_edit.text().strip()
            if not out:
                return
        out_path = Path(out)
        if out_path.resolve() == self.video.resolve():
            QMessageBox.warning(self, "SkyStamp", "Output can't overwrite the original video.")
            return
        if out_path.exists():
            r = QMessageBox.question(self, "File exists", f"{out_path.name} already exists. Replace it?")
            if r != QMessageBox.Yes:
                return
        out_path.parent.mkdir(parents=True, exist_ok=True)
        self._save_settings()

        fmt = self.format_combo.currentData()
        self.render_thread = RenderThread(dict(
            video=self.video, tel=self.tel, output=out_path,
            style_id=self.style_combo.currentData(), opts=self.options(), fmt=fmt,
            quality=self.quality_combo.currentData(), hardware=self.hw_check.isChecked(),
            overlay_fps=self.rate_combo.currentData(),
        ))
        self.render_thread.progressed.connect(self._on_progress)
        self.render_thread.finished_ok.connect(self._on_done)
        self.render_thread.failed.connect(self._on_failed)
        self.render_thread.cancelled.connect(self._on_cancelled)
        self.render_thread.finished.connect(self._render_cleanup)
        self.progress.setValue(0)
        self.progress.show()
        self.cancel_btn.show()
        self.open_btn.hide()
        self.status.setText("Starting…")
        self.render_thread.start()
        self._update_enabled()

    def cancel_render(self):
        if self.render_thread:
            self.render_thread.cancel_event.set()
            self.status.setText("Cancelling…")

    def _on_progress(self, p, msg):
        self.progress.setValue(int(p * 100))
        self.status.setText(msg)

    def _on_done(self, path):
        self.progress.setValue(100)
        self.status.setText(f"✓ Saved: {path}")
        self.last_output = path
        self.open_btn.show()

    def _on_failed(self, msg):
        self.status.setText("Render failed.")
        QMessageBox.critical(self, "Render failed", msg)

    def _on_cancelled(self):
        self.status.setText("Cancelled.")

    def _render_cleanup(self):
        self.render_thread = None
        self.cancel_btn.hide()
        self._update_enabled()

    def reveal_output(self):
        path = getattr(self, "last_output", None)
        if not path:
            return
        if sys.platform == "darwin":
            subprocess.run(["open", "-R", path])
        elif os.name == "nt":
            subprocess.run(["explorer", "/select,", os.path.normpath(path)])
        else:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(path).parent)))

    def closeEvent(self, e):
        if self.render_thread:
            r = QMessageBox.question(self, "SkyStamp", "A video is still rendering. Stop it and quit?")
            if r != QMessageBox.Yes:
                e.ignore()
                return
            self.render_thread.cancel_event.set()
            self.render_thread.wait(5000)
        self._save_settings()
        super().closeEvent(e)


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("SkyStamp")
    from importlib import resources

    icon = resources.files("skystamp") / "assets" / "icon.png"
    if icon.is_file():
        app.setWindowIcon(QIcon(str(icon)))
    win = MainWindow()
    win.show()
    if len(sys.argv) > 1 and Path(sys.argv[1]).is_file():
        win.load_video(Path(sys.argv[1]))
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
