"""SkyStamp desktop app: pick a video + SRT, choose a style, render."""

from __future__ import annotations

import os
import subprocess
import sys
import threading
from pathlib import Path

from PIL.ImageQt import ImageQt
from PySide6.QtCore import QObject, QPointF, QRunnable, QSettings, Qt, QThread, QThreadPool, QTimer, QUrl, Signal
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
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSlider,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from . import __version__
from .maptiles import PROVIDERS
from .editor import StyleEditor, move_element
from .styles import DEFAULT_FIELDS, FIELDS, MAP_VIEWS, POSITIONS, OverlayOptions, SpecStyle
from .styles import library as style_lib
from . import flightlog
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
    """Keeps the preview image scaled to the available space and reports mouse
    positions in image pixels (for the style editor)."""
    pressed = Signal(QPointF)
    moved = Signal(QPointF)
    released = Signal()
    wheeled = Signal(int)  # +1 / -1

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

    def _to_image(self, pos) -> QPointF | None:
        if self._pix is None or self._pix.width() == 0:
            return None
        k = min(self.width() / self._pix.width(), self.height() / self._pix.height())
        ox = (self.width() - self._pix.width() * k) / 2
        oy = (self.height() - self._pix.height() * k) / 2
        return QPointF((pos.x() - ox) / k, (pos.y() - oy) / k)

    def mousePressEvent(self, e):
        p = self._to_image(e.position())
        if p is not None:
            self.pressed.emit(p)

    def mouseMoveEvent(self, e):
        p = self._to_image(e.position())
        if p is not None and e.buttons() & Qt.LeftButton:
            self.moved.emit(p)

    def mouseReleaseEvent(self, e):
        self.released.emit()

    def wheelEvent(self, e):
        step = 1 if e.angleDelta().y() > 0 else -1 if e.angleDelta().y() < 0 else 0
        if step:
            self.wheeled.emit(step)


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
        self.editing = False
        self.edit_base = None
        self.edit_boxes: dict = {}
        self.edit_guides: list = []
        self.drag = None

        self.preview_timer = QTimer(self, singleShot=True, interval=120)
        self.preview_timer.timeout.connect(self.refresh_preview)

        self._build()
        self._reload_styles(select=self.settings.value("style", "hud"))
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
        self.preview.pressed.connect(self._preview_pressed)
        self.preview.moved.connect(self._preview_moved)
        self.preview.released.connect(self._preview_released)
        self.preview.wheeled.connect(self._preview_wheel)
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
        # One row per file: path + its button, so the panel stays short
        def file_row(edit, *buttons):
            row = QHBoxLayout()
            row.addWidget(edit, 1)
            for btn in buttons:
                row.addWidget(btn)
            fl.addRow(row)

        self.video_edit = QLineEdit(readOnly=True, placeholderText="No video selected")
        b = QPushButton("Video…")
        b.clicked.connect(self.choose_video)
        file_row(self.video_edit, b)
        self.srt_edit = QLineEdit(readOnly=True, placeholderText="Found automatically next to the video")
        b2 = QPushButton("SRT…")
        b2.clicked.connect(self.choose_srt)
        file_row(self.srt_edit, b2)
        self.log_edit = QLineEdit(readOnly=True, placeholderText="Optional: DJI Fly flight log")
        self.log_edit.setToolTip("FlightRecord_….txt from the DJI Fly app. Adds GPS satellite count and battery.")
        b3 = QPushButton("Log…")
        b3.clicked.connect(self.choose_log)
        self.key_btn = QPushButton("API key…")
        self.key_btn.setToolTip("Needed to read encrypted DJI Fly logs. Free from developer.dji.com.")
        self.key_btn.clicked.connect(self.edit_api_key)
        file_row(self.log_edit, b3, self.key_btn)
        self.log_status = QLabel(wordWrap=True)
        self.log_status.setStyleSheet("color:#888;")
        self.log_status.setMinimumWidth(1)
        self.log_status.hide()  # shown only when there's something to say
        fl.addRow(self.log_status)
        side.addWidget(files)

        style = QGroupBox("2 · Style")
        style_outer = QVBoxLayout(style)
        self.style_stack = QStackedWidget()
        style_outer.addWidget(self.style_stack)
        normal = QWidget()
        sl = QFormLayout(normal)
        sl.setContentsMargins(0, 0, 0, 0)
        self.style_stack.addWidget(normal)
        self.editor = StyleEditor()
        self.editor.spec_changed.connect(self._render_edit_preview)
        self.editor.selection_changed.connect(lambda _: self._render_edit_preview())
        self.editor.save_requested.connect(self.save_edited_style)
        self.editor.cancel_requested.connect(self.exit_edit)
        self.style_stack.addWidget(self.editor)

        self.style_combo = QComboBox()
        self.style_combo.currentIndexChanged.connect(self._style_changed)
        self.style_desc = QLabel(wordWrap=True)
        self.style_desc.setMinimumWidth(1)
        self.style_desc.setStyleSheet("color:#888;")
        sl.addRow("Style", self.style_combo)
        srow = QHBoxLayout()
        self.customize_btn = QPushButton("Customize…")
        self.customize_btn.setToolTip("Move, resize and choose what each part of the overlay shows")
        self.customize_btn.clicked.connect(self.enter_edit)
        more = QPushButton("More ▾")
        more_menu = QMenu(more)
        more_menu.addAction("New empty style", lambda: self.enter_edit(blank=True))
        more_menu.addAction("Import style…", self.import_style)
        self.export_action = more_menu.addAction("Export style…", self.export_style)
        self.delete_action = more_menu.addAction("Delete style", self.delete_style)
        more.setMenu(more_menu)
        srow.addWidget(self.customize_btn, 1)
        srow.addWidget(more)
        sl.addRow(srow)
        sl.addRow(self.style_desc)
        self.styles = {}
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
        self.color_btn = QPushButton()
        self.color_btn.setToolTip("Accent color")
        self.color_btn.setFixedWidth(100)
        self.color_btn.clicked.connect(self.choose_color)
        size_row = QHBoxLayout()
        size_row.addWidget(self.size_slider, 1)
        size_row.addWidget(self.color_btn)
        sl.addRow("Size · color", size_row)
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
        sl.addRow("Map background", self.map_combo)
        self.view_combo = QComboBox()
        for k, label in MAP_VIEWS.items():
            self.view_combo.addItem(label, k)
        self.view_combo.setToolTip("3D views slowly circle the flight and take longer to render.\n"
                                   "Terrain heights are downloaded once per video (internet needed) and cached.")
        self.view_combo.currentIndexChanged.connect(self._schedule_preview)
        sl.addRow("Map view", self.view_combo)
        self.field_checks["map"].toggled.connect(lambda on: (self.map_combo.setEnabled(on),
                                                             self.view_combo.setEnabled(on)))
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
        orow = QHBoxLayout()
        orow.addWidget(self.output_edit, 1)
        orow.addWidget(ob)
        ol.addRow(orow)
        side.addStretch(1)

        # Output and the Create button stay in view below the scrolling settings
        bottom = QWidget()
        actions = QVBoxLayout(bottom)
        actions.setContentsMargins(0, 6, 8, 0)
        actions.addWidget(out)

        self.render_btn = QPushButton("Create video")
        self.render_btn.setMinimumHeight(44)
        self.render_btn.setStyleSheet("font-size:16px; font-weight:600;")
        self.render_btn.clicked.connect(self.start_render)
        actions.addWidget(self.render_btn)
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.clicked.connect(self.cancel_render)
        self.cancel_btn.hide()
        actions.addWidget(self.cancel_btn)
        self.progress = QProgressBar(textVisible=True)
        self.progress.hide()
        actions.addWidget(self.progress)
        self.status = QLabel(wordWrap=True)
        actions.addWidget(self.status)
        self.open_btn = QPushButton("Show in folder")
        self.open_btn.clicked.connect(self.reveal_output)
        self.open_btn.hide()
        actions.addWidget(self.open_btn)

        # Long combo entries / file paths must shrink, never widen the panel
        for w in (panel, bottom):
            for combo in w.findChildren(QComboBox):
                combo.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
                combo.setMinimumContentsLength(8)
                combo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            for edit in w.findChildren(QLineEdit):
                edit.setMinimumWidth(0)
            for form in w.findChildren(QFormLayout):
                # macOS keeps fields at their size hint by default; let them fill the panel
                form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        scroll = QScrollArea(widgetResizable=True)
        scroll.setWidget(panel)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setFrameShape(QScrollArea.NoFrame)
        right = QWidget()
        right.setFixedWidth(430)
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.setSpacing(0)
        rl.addWidget(scroll, 1)
        rl.addWidget(bottom)
        outer.addWidget(right)

    # -- settings ----------------------------------------------------------
    def _load_settings(self):
        s = self.settings
        self._set_combo(self.style_combo, s.value("style", "hud"))
        self._set_combo(self.units_combo, s.value("units", "metric"))
        self._set_combo(self.map_combo, s.value("map_tiles", "osm"))
        self._set_combo(self.view_combo, s.value("map_view", "2d"))
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
        s.setValue("map_view", self.view_combo.currentData())
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
            map_view=self.view_combo.currentData(),
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
        self._auto_find_log()

    # -- DJI Fly flight log ------------------------------------------------
    def _log_folders(self) -> list[Path]:
        folders = []
        if self.video:
            folders += [self.video.parent, self.video.parent.parent]
        last = self.settings.value("log_dir", "")
        if last:
            folders.append(Path(last))
        folders.append(Path.home() / "Desktop" / "FlightRecords")
        return folders

    def _auto_find_log(self):
        self.flight_log = None
        self.log_edit.clear()
        self._log_note("")
        start = next((s.timestamp for s in self.tel.samples if s.timestamp), None) if self.tel else None
        if start is None:
            return
        found = flightlog.find_log(start, self._log_folders())
        if found:
            self.load_log(found)

    def choose_log(self):
        start = self.settings.value("log_dir", str(self.video.parent if self.video else Path.home()))
        path, _ = QFileDialog.getOpenFileName(self, "Choose DJI Fly flight log", start,
                                              "DJI flight logs (*.txt);;All files (*)")
        if path:
            self.settings.setValue("log_dir", str(Path(path).parent))
            self.load_log(Path(path))

    def api_key(self) -> str | None:
        return (self.settings.value("dji_api_key", "") or "").strip() or flightlog.api_key_from_env()

    def edit_api_key(self):
        key, ok = QInputDialog.getText(
            self, "DJI API key",
            "DJI Fly logs are encrypted. To read them, create a free app at developer.dji.com\n"
            "(App Type: Open API) and paste its App Key here. It stays on this computer.",
            QLineEdit.Password, self.settings.value("dji_api_key", ""))
        if ok:
            self.settings.setValue("dji_api_key", key.strip())
            if self.log_edit.text():
                self.load_log(Path(self.log_edit.text()))

    def load_log(self, path: Path):
        if not self.tel:
            return
        self.log_edit.setText(str(path))
        self._log_note("Reading flight log…")
        self.log_req = getattr(self, "log_req", 0) + 1
        req, key = self.log_req, self.api_key()
        job = _Job(req, lambda: flightlog.load(path, key))
        job.signals.done.connect(self._log_loaded)
        self.pool.start(job)

    def _log_loaded(self, req, result):
        if req != self.log_req or not self.tel:
            return
        if isinstance(result, Exception):
            self.flight_log = None
            msg = str(result) if isinstance(result, flightlog.FlightLogError) else f"Couldn't read log: {result}"
            self._log_note(f"⚠ {msg}")
            return
        if not flightlog.attach(self.tel, result):
            self._log_note("⚠ This flight log doesn't match the video (different flight or time).")
            return
        self.flight_log = result
        sats = [s.satellites for s in self.tel.samples if s.satellites is not None]
        bats = [s.battery for s in self.tel.samples if s.battery is not None]
        parts = [f"✓ Flight log matched ({result.start.astimezone():%H:%M}–{result.end.astimezone():%H:%M})"]
        if sats:
            parts.append(f"satellites {min(sats)}–{max(sats)}")
        if bats:
            parts.append(f"battery {bats[0]:.0f}% → {bats[-1]:.0f}%")
        self._log_note(" · ".join(parts))
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

    def _log_note(self, text: str):
        self.log_status.setText(text)
        self.log_status.setVisible(bool(text))

    def _paint_color(self):
        self.color_btn.setText(self.accent.upper())
        self.color_btn.setStyleSheet(f"background:{self.accent}; color:#000; font-weight:600;")

    # -- styles --------------------------------------------------------------
    def _reload_styles(self, select: str | None = None):
        current = select or self.style_combo.currentData()
        self.styles = style_lib.all_styles()
        self.style_combo.blockSignals(True)
        self.style_combo.clear()
        user_started = False
        for sid, st in self.styles.items():
            if not st.builtin and not user_started:
                self.style_combo.insertSeparator(self.style_combo.count())
                user_started = True
            self.style_combo.addItem(st.name if st.builtin else f"★ {st.name}", sid)
        i = self.style_combo.findData(current)
        self.style_combo.setCurrentIndex(max(0, i))
        self.style_combo.blockSignals(False)
        self._style_changed()

    def current_style(self):
        return self.styles.get(self.style_combo.currentData()) or self.styles["hud"]

    def _style_changed(self):
        st = self.current_style()
        self.style_desc.setText(st.description or ("Your own style." if not st.builtin else ""))
        uses_position = any(el.get("position_option") for el in st.spec["elements"])
        self.position_combo.setEnabled(uses_position)
        self.customize_btn.setText("Edit…" if not st.builtin else "Customize…")
        self.export_action.setEnabled(True)
        self.delete_action.setEnabled(not st.builtin)
        self._schedule_preview()

    def enter_edit(self, blank: bool = False):
        if not self.tel or not self.info:
            QMessageBox.information(self, "SkyStamp", "Load a video and its SRT first, so you can see the style "
                                                      "on a real frame while you edit it.")
            return
        base = self.current_style()
        if blank:
            spec, self.edit_base = {"name": "My style", "elements": []}, None
        elif base.builtin:
            spec = dict(base.spec, name=f"{base.name} (my version)", description="")
            self.edit_base = None
        else:
            spec, self.edit_base = base.spec, base
        self.editing = True
        self.editor.load(spec)
        self.style_stack.setCurrentWidget(self.editor)
        self.style_combo.setEnabled(False)
        self._update_enabled()
        self.status.setText("Editing style: drag elements on the preview.")
        self._render_edit_preview()

    def exit_edit(self):
        self.editing = False
        self.drag = None
        self.edit_guides = []
        self.style_stack.setCurrentIndex(0)
        self.style_combo.setEnabled(True)
        self.status.setText("")
        self._update_enabled()
        self._schedule_preview()

    def save_edited_style(self):
        spec = self.editor.current_spec()
        if not spec["elements"]:
            QMessageBox.warning(self, "SkyStamp", "Add at least one element before saving.")
            return
        try:
            saved = style_lib.save_user_style(spec, previous=self.edit_base)
        except OSError as e:
            QMessageBox.critical(self, "Couldn't save style", str(e))
            return
        self.exit_edit()
        self._reload_styles(select=saved.id)
        self.status.setText(f"✓ Style “{saved.name}” saved.")

    def import_style(self):
        path, _ = QFileDialog.getOpenFileName(self, "Import style", str(Path.home()),
                                              f"SkyStamp style (*{style_lib.EXT});;All files (*)")
        if not path:
            return
        try:
            st = style_lib.import_style(path)
        except (ValueError, OSError) as e:
            QMessageBox.warning(self, "Can't import style", str(e))
            return
        self._reload_styles(select=st.id)
        self.status.setText(f"✓ Imported “{st.name}”.")

    def export_style(self):
        st = self.current_style()
        name = style_lib._slug(st.name) + style_lib.EXT
        path, _ = QFileDialog.getSaveFileName(self, "Export style", str(Path.home() / name),
                                              f"SkyStamp style (*{style_lib.EXT})")
        if path:
            if not path.endswith(style_lib.EXT):
                path += style_lib.EXT
            style_lib.export_style(st, path)
            self.status.setText(f"✓ Exported to {path}")

    def delete_style(self):
        st = self.current_style()
        if st.builtin:
            return
        if QMessageBox.question(self, "Delete style", f"Delete “{st.name}”?") != QMessageBox.Yes:
            return
        style_lib.delete_user_style(st)
        self._reload_styles(select="hud")

    # -- editing on the preview ------------------------------------------------
    def _edit_frame(self):
        t = self.slider.value() / 10
        return t, self.frame_cache.get(t)

    def _render_edit_preview(self):
        if not self.editing or not self.tel or not self.video:
            return
        from PIL import Image, ImageDraw

        t, frame = self._edit_frame()
        if frame is None:
            self.refresh_preview()  # fetches the frame, then comes back here
            return
        st = SpecStyle(frame.width, frame.height, self.tel, self.options(), self.editor.current_spec())
        img = Image.alpha_composite(frame, st.render(t))
        self.edit_boxes = st.boxes
        self.edit_margin = st.margin
        d = ImageDraw.Draw(img)
        for kind, v in self.edit_guides:
            if kind == "v":
                d.line([(v, 0), (v, img.height)], fill=(255, 64, 160, 230), width=2)
            else:
                d.line([(0, v), (img.width, v)], fill=(255, 64, 160, 230), width=2)
        for i, (x0, y0, x1, y1) in self.edit_boxes.items():
            sel = i == self.editor.selected
            pad = 4
            d.rectangle((x0 - pad, y0 - pad, x1 + pad, y1 + pad),
                        outline=(56, 189, 248, 255) if sel else (255, 255, 255, 90), width=3 if sel else 1)
        self.preview.set_image(QPixmap.fromImage(ImageQt(img.convert("RGB"))))

    def _preview_pressed(self, p):
        if not self.editing:
            return
        hit = None
        for i, (x0, y0, x1, y1) in self.edit_boxes.items():
            if x0 - 6 <= p.x() <= x1 + 6 and y0 - 6 <= p.y() <= y1 + 6:
                hit = i  # later elements are drawn on top
        self.editor.select(hit)
        self.drag = (hit, p, self.edit_boxes.get(hit)) if hit is not None else None

    def _preview_moved(self, p):
        if not self.editing or not self.drag:
            return
        i, start, box = self.drag
        dx, dy = p.x() - start.x(), p.y() - start.y()
        _, frame = self._edit_frame()
        if frame is None or box is None:
            return
        new_box = (box[0] + dx, box[1] + dy, box[2] + dx, box[3] + dy)
        el, self.edit_guides = move_element(self.editor.spec["elements"][i], new_box, frame.width, frame.height,
                                            getattr(self, "edit_margin", 0))
        self.editor.update_element(i, el)

    def _preview_wheel(self, step: int):
        """Mouse wheel over the preview resizes the selected element."""
        if self.editing and self.editor.selected is not None:
            s = self.editor.size
            s.setValue(s.value() + step * max(1, s.value() // 12))

    def _preview_released(self):
        if self.drag:
            self.drag = None
            self.edit_guides = []
            self._render_edit_preview()

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
        self.render_btn.setEnabled(ready and not busy and not self.editing)

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
        opts, style = self.options(), self.current_style()
        editing = self.editing
        cache = self.frame_cache

        def work():
            from PIL import Image

            frame = cache.get(t)
            if frame is None:
                frame = extract_frame(video, t, 1600).convert("RGBA")
                if len(cache) > 30:
                    cache.clear()
                cache[t] = frame
            if tel is None or editing:
                return frame.convert("RGB")
            # Layouts are resolution-independent, so draw straight at preview size
            overlay = style(frame.width, frame.height, tel, opts).render(t)
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
        if self.editing:
            self._render_edit_preview()
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
            style_id=self.current_style(), opts=self.options(), fmt=fmt,
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
