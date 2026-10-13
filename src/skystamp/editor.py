"""Visual style editor: drag elements on the preview, tweak them in a side panel."""

from __future__ import annotations

import copy

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QCheckBox,
    QColorDialog,
    QComboBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QPushButton,
    QSizePolicy,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from .styles.base import FIELDS
from .styles.engine import DATA_FIELDS, ELEMENT_DEFAULTS, INFO_ITEMS, TYPE_NAMES, normalize
from .styles.layout import SNAP_PX, anchor_for, move_element  # noqa: F401  (re-exported for the GUI)

INFO_NAMES = {"gps": "GPS signal", "datetime": "Date & time", "date": "Date", "time": "Time",
              "camera": "Camera settings", "coords": "GPS coordinates"}


def new_element(kind: str) -> dict:
    el = copy.deepcopy(ELEMENT_DEFAULTS[kind])
    el.update(type=kind, x=0.5, y=0.5, anchor="c")
    return el


def describe(el: dict) -> str:
    t = el["type"]
    if t == "data":
        names = [FIELDS.get(f, f) for f in el.get("fields", [])]
        return f"{TYPE_NAMES[t]}: {', '.join(names[:3])}{'…' if len(names) > 3 else ''}"
    if t == "info":
        return f"{TYPE_NAMES[t]}: {', '.join(INFO_NAMES.get(i, i) for i in el.get('items', []))}"
    if t == "speedbox":
        return f"{TYPE_NAMES[t]}: {FIELDS.get(el.get('field', 'speed'), 'speed')}"
    return TYPE_NAMES[t]


# --- widgets ------------------------------------------------------------------------

class CheckList(QListWidget):
    """Checkable list whose rows can be dragged to reorder."""
    changed = Signal(list)

    def __init__(self, options: dict[str, str]):
        super().__init__()
        self.setDragDropMode(QListWidget.InternalMove)
        self.setMaximumHeight(160)
        self._loading = False
        self.itemChanged.connect(self._emit)
        self.model().rowsMoved.connect(self._emit)
        self.options = options

    def set_values(self, selected: list[str]):
        self._loading = True
        self.clear()
        order = [k for k in selected if k in self.options] + [k for k in self.options if k not in selected]
        for key in order:
            it = QListWidgetItem(self.options[key])
            it.setData(Qt.UserRole, key)
            it.setFlags(it.flags() | Qt.ItemIsUserCheckable | Qt.ItemIsDragEnabled)
            it.setCheckState(Qt.Checked if key in selected else Qt.Unchecked)
            self.addItem(it)
        self._loading = False

    def values(self) -> list[str]:
        return [self.item(i).data(Qt.UserRole) for i in range(self.count())
                if self.item(i).checkState() == Qt.Checked]

    def _emit(self, *_):
        if not self._loading:
            self.changed.emit(self.values())


class StyleEditor(QWidget):
    """Side panel for editing a style spec. The preview handles dragging."""
    spec_changed = Signal()
    selection_changed = Signal(object)  # element index or None
    save_requested = Signal()
    cancel_requested = Signal()

    def __init__(self):
        super().__init__()
        self.spec: dict = {"elements": []}
        self.selected: int | None = None
        self._loading = False
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)

        form = QFormLayout()
        self.name_edit = QLineEdit()
        self.name_edit.textChanged.connect(self._name_changed)
        form.addRow("Name", self.name_edit)
        self.corners = QCheckBox("Viewfinder corners")
        self.corners.toggled.connect(self._corners_changed)
        form.addRow(self.corners)
        lay.addLayout(form)

        hint = QLabel("Click an element on the preview to select it, drag to move it (it snaps to the edges and "
                      "the centre), scroll to resize it.")
        hint.setWordWrap(True)
        hint.setMinimumWidth(1)
        hint.setStyleSheet("color:#888;")
        lay.addWidget(hint)

        self.list = QListWidget()
        self.list.setMaximumHeight(130)
        self.list.currentRowChanged.connect(self._row_changed)
        lay.addWidget(self.list)
        row = QHBoxLayout()
        add = QPushButton("Add…")
        menu = QMenu(add)
        for kind, label in TYPE_NAMES.items():
            menu.addAction(label, lambda k=kind: self.add_element(k))
        add.setMenu(menu)
        self.remove_btn = QPushButton("Remove")
        self.remove_btn.clicked.connect(self.remove_selected)
        row.addWidget(add, 1)
        row.addWidget(self.remove_btn, 1)
        lay.addLayout(row)

        # Properties
        self.props = QWidget()
        self.pform = QFormLayout(self.props)
        self.pform.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        self.size = QSlider(Qt.Horizontal, minimum=10, maximum=200)
        self.size.valueChanged.connect(lambda v: self._set("size", float(v)))
        self.pform.addRow("Size", self.size)
        self.color = QPushButton()
        self.color.clicked.connect(self._pick_color)
        self.pform.addRow("Text color", self.color)

        self.fields = CheckList({k: FIELDS[k] for k in DATA_FIELDS})
        self.fields.changed.connect(lambda v: self._set("fields", v))
        self.pform.addRow("Show", self.fields)
        self.items = CheckList(INFO_NAMES)
        self.items.changed.connect(lambda v: self._set("items", v))
        self.pform.addRow("Show", self.items)
        self.direction = self._combo({"row": "In a row", "column": "Stacked"}, "direction")
        self.pform.addRow("Layout", self.direction)
        self.labels = self._combo({"above": "Small label above", "below": "Label below", "prefix": "ALT: 57.2m",
                                   "short": "H 57.2 m (inline)", "none": "No labels"}, "labels")
        self.pform.addRow("Labels", self.labels)
        self.panel = self._combo({"none": "None (text outline)", "dark": "Dark panel", "bar": "Panel + accent bar"},
                                 "panel")
        self.pform.addRow("Background", self.panel)
        self.extra = self._combo({"none": "None", "header": "Line above", "footer": "Line below"}, None)
        self.extra.currentIndexChanged.connect(self._extra_changed)
        self.pform.addRow("Info line", self.extra)
        self.extra_items = CheckList(INFO_NAMES)
        self.extra_items.changed.connect(self._extra_changed)
        self.pform.addRow("Info line shows", self.extra_items)
        self.inner_map = QCheckBox("Mini-map inside the panel")
        self.inner_map.toggled.connect(lambda v: self._set("map", v))
        self.pform.addRow(self.inner_map)
        self.field = self._combo({k: FIELDS[k] for k in DATA_FIELDS}, "field")
        self.pform.addRow("Number", self.field)
        self.caption = QCheckBox("GPS coordinates under the map")
        self.caption.toggled.connect(lambda v: self._set("caption", "coords" if v else None))
        self.pform.addRow(self.caption)
        lay.addWidget(self.props)
        lay.addStretch(1)

        btns = QHBoxLayout()
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.cancel_requested)
        self.save_btn = QPushButton("Save style")
        self.save_btn.setDefault(True)
        self.save_btn.setStyleSheet("font-weight:600;")
        self.save_btn.clicked.connect(self.save_requested)
        btns.addWidget(cancel, 1)
        btns.addWidget(self.save_btn, 1)
        lay.addLayout(btns)

        for w in (self.props,):
            w.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)

    # -- helpers -----------------------------------------------------------------
    def _combo(self, options: dict[str, str], key: str | None) -> QComboBox:
        c = QComboBox()
        c.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        c.setMinimumContentsLength(8)
        for k, v in options.items():
            c.addItem(v, k)
        if key:
            c.currentIndexChanged.connect(lambda _=0, c=c, key=key: self._set(key, c.currentData()))
        return c

    def _row(self, widget, visible: bool):
        self.pform.setRowVisible(widget, visible)

    # -- spec in / out ---------------------------------------------------------------
    def load(self, spec: dict):
        self.spec = normalize(spec)
        self._loading = True
        self.name_edit.setText(self.spec["name"])
        self.corners.setChecked(self.spec["corners"])
        self._loading = False
        self._refresh_list()
        self.select(0 if self.spec["elements"] else None)

    def current_spec(self) -> dict:
        return normalize(self.spec)

    def _refresh_list(self):
        self.list.blockSignals(True)
        self.list.clear()
        for el in self.spec["elements"]:
            self.list.addItem(describe(el))
        if self.selected is not None and self.selected < self.list.count():
            self.list.setCurrentRow(self.selected)
        self.list.blockSignals(False)

    def select(self, index: int | None):
        if index is not None and not (0 <= index < len(self.spec["elements"])):
            index = None
        self.selected = index
        self.list.blockSignals(True)
        self.list.setCurrentRow(index if index is not None else -1)
        self.list.blockSignals(False)
        self._show_props()
        self.selection_changed.emit(index)

    def update_element(self, index: int, el: dict):
        """Called by the preview while dragging."""
        self.spec["elements"][index] = el
        self.spec_changed.emit()

    def _row_changed(self, row: int):
        self.select(row if row >= 0 else None)

    def _show_props(self):
        el = self.spec["elements"][self.selected] if self.selected is not None else None
        self.props.setVisible(el is not None)
        self.remove_btn.setEnabled(el is not None)
        if el is None:
            return
        t = el["type"]
        self._loading = True
        self.size.setValue(int(el["size"]))
        self._paint_color(el.get("color"))
        self.fields.set_values(el.get("fields", []))
        self.items.set_values(el.get("items", []))
        for combo, key in ((self.direction, "direction"), (self.labels, "labels"), (self.panel, "panel"),
                           (self.field, "field")):
            i = combo.findData(el.get(key))
            combo.setCurrentIndex(max(0, i))
        where = "header" if el.get("header") else "footer" if el.get("footer") else "none"
        self.extra.setCurrentIndex(self.extra.findData(where))
        self.extra_items.set_values(el.get("header") or el.get("footer") or [])
        self.inner_map.setChecked(bool(el.get("map")))
        self.caption.setChecked(el.get("caption") == "coords")
        self._loading = False
        self.pform.setRowVisible(self.size, True)
        self._row(self.color, t != "map")
        self._row(self.fields, t == "data")
        self._row(self.items, t == "info")
        self._row(self.direction, t in ("data", "info"))
        self._row(self.labels, t == "data")
        self._row(self.panel, t in ("data", "speedbox"))
        self._row(self.extra, t == "data")
        self._row(self.extra_items, t == "data" and where != "none")
        self._row(self.inner_map, t == "data")
        self._row(self.field, t == "speedbox")
        self._row(self.caption, t == "map")

    def _set(self, key: str, value):
        if self._loading or self.selected is None:
            return
        el = self.spec["elements"][self.selected]
        if key == "panel" and el["type"] == "speedbox" and value == "bar":
            value = "dark"
        el[key] = value
        self.list.item(self.selected).setText(describe(el))
        self.spec_changed.emit()

    def _extra_changed(self, *_):
        if self._loading or self.selected is None:
            return
        el = self.spec["elements"][self.selected]
        where = self.extra.currentData()
        items = self.extra_items.values() or ["datetime", "gps"]
        el["header"], el["footer"] = (items, []) if where == "header" else ([], items) if where == "footer" else ([], [])
        self._row(self.extra_items, where != "none")
        if where != "none" and not self.extra_items.values():
            self._loading = True
            self.extra_items.set_values(items)
            self._loading = False
        self.spec_changed.emit()

    def _pick_color(self):
        if self.selected is None:
            return
        cur = self.spec["elements"][self.selected].get("color") or "#FFFFFF"
        c = QColorDialog.getColor(QColor(cur), self, "Text color")
        if c.isValid():
            self._set("color", c.name())
            self._paint_color(c.name())

    def _paint_color(self, color: str | None):
        c = color or "#FFFFFF"
        self.color.setText((color or "White (default)").upper() if color else "White (default)")
        self.color.setStyleSheet(f"background:{c}; color:#000; font-weight:600;")

    def _name_changed(self, text: str):
        if not self._loading:
            self.spec["name"] = text.strip() or "Untitled"

    def _corners_changed(self, on: bool):
        if not self._loading:
            self.spec["corners"] = on
            self.spec_changed.emit()

    def add_element(self, kind: str):
        self.spec["elements"].append(new_element(kind))
        self._refresh_list()
        self.select(len(self.spec["elements"]) - 1)
        self.spec_changed.emit()

    def remove_selected(self):
        if self.selected is None:
            return
        del self.spec["elements"][self.selected]
        self.selected = None
        self._refresh_list()
        self.select(min(len(self.spec["elements"]) - 1, 0) if self.spec["elements"] else None)
        self.spec_changed.emit()
