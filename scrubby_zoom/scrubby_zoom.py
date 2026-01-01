"""
Scrubby Zoom (Horizontal Relative Zoom) — Krita 5.2.x

Fixes "zoom jumping" across multiple documents by correcting Krita's
Canvas.zoomLevel() DPI-scaled return value into KoZoomMode::ZOOM_CONSTANT units.

Key facts (Krita 5.2.x):
- Canvas.zoomLevel() returns zoomManager()->zoom() (libkis Canvas.cpp)
- Canvas.setZoomLevel(v) uses zoomController()->setZoom(ZOOM_CONSTANT, v)
- zoomLevel() is known to include document DPI scaling in many cases
  (often behaving like constant_zoom * (dpi/72)), causing large mismatches.

This script:
- Captures initial zoom in ZOOM_CONSTANT scale (1.0 == 100%)
- Applies exponential zoom based on horizontal drag
- Avoids UI-label-percent misreads by preferring a specific zoom widget match,
  then falling back to corrected canvas.zoomLevel().
"""

from krita import Extension, Krita
from PyQt5.QtWidgets import QApplication, QStatusBar, QLabel, QComboBox, QLineEdit, QToolButton
from PyQt5.QtCore import Qt, QEvent, QObject
from PyQt5.QtGui import QCursor, QPixmap
import os
import re


REFERENCE_DPI = 72.0  # Krita/Calligra zoom infrastructure historically assumes 72dpi as "1:1"


class ZoomEventFilter(QObject):
    def __init__(self, extension):
        super().__init__()
        self.ext = extension

    def eventFilter(self, obj, event):
        if not self.ext.zoom_mode_active:
            return False

        t = event.type()

        # Start drag
        if t == QEvent.MouseButtonPress:
            if event.button() == Qt.LeftButton:
                self.ext.start_drag(self.ext._event_global_pos(event))
                return True

        # Update while dragging
        elif t == QEvent.MouseMove:
            if self.ext.is_dragging:
                self.ext.update_zoom(self.ext._event_global_pos(event))
                return True

        # End drag
        elif t == QEvent.MouseButtonRelease:
            if event.button() == Qt.LeftButton and self.ext.is_dragging:
                self.ext.end_drag()
                return True

        # Deactivate on shortcut release (best-effort: any non-auto-repeat key release)
        elif t == QEvent.KeyRelease:
            if not event.isAutoRepeat():
                self.ext.deactivate_zoom_mode()
                return False

        return False


class ScrubbyZoomExtension(Extension):
    def __init__(self, parent):
        super().__init__(parent)

        self.zoom_mode_active = False
        self.is_dragging = False

        self.drag_start_x = 0
        self.active_view = None

        # Store zoom in ZOOM_CONSTANT scale (1.0 == 100%)
        self.initial_zoom_scale = 1.0
        self.last_set_zoom_scale = None

        # Per-document last known zoom scale (ZOOM_CONSTANT)
        self.document_zoom_cache = {}

        # Sensitivity: pixels per doubling/halving
        self.zoom_sensitivity = 150.0

        self.event_filter = ZoomEventFilter(self)
        self.filter_installed = False

        self.zoom_cursor = None
        self._load_cursor()

    def setup(self):
        pass

    def createActions(self, window):
        action = window.createAction(
            "scrubby_zoom_activate",
            "Horizontal Relative Zoom",
            "tools/scripts"
        )
        action.triggered.connect(self.activate_zoom_mode)

    # ----------------------------
    # Cursor loading (kept from your variant)
    # ----------------------------
    def _get_cursor_icon_path(self):
        plugin_dir = os.path.dirname(os.path.realpath(__file__))
        icons_dir = os.path.join(plugin_dir, "Icons")
        available_sizes = [24, 32, 48, 64, 96]

        try:
            screen = QApplication.primaryScreen()
            dpr = screen.devicePixelRatio() if screen else 1.0
        except Exception:
            dpr = 1.0

        target_physical_size = int(24 * dpr)
        best_size = available_sizes[-1]
        for size in available_sizes:
            if size >= target_physical_size:
                best_size = size
                break

        cursor_path = os.path.join(icons_dir, f"zoomin{best_size}.png")
        if not os.path.exists(cursor_path):
            for size in available_sizes:
                fallback_path = os.path.join(icons_dir, f"zoomin{size}.png")
                if os.path.exists(fallback_path):
                    cursor_path = fallback_path
                    best_size = size
                    break

        return cursor_path, best_size, dpr

    def _load_cursor(self):
        try:
            cursor_path, icon_size, dpr = self._get_cursor_icon_path()
            if os.path.exists(cursor_path):
                pixmap = QPixmap(cursor_path)
                if not pixmap.isNull():
                    pixmap.setDevicePixelRatio(dpr)
                    hotspot = int(icon_size / (4 * dpr))
                    self.zoom_cursor = QCursor(pixmap, hotspot, hotspot)
                    return
        except Exception:
            pass
        self.zoom_cursor = QCursor(Qt.SizeHorCursor)

    # ----------------------------
    # Utilities
    # ----------------------------
    def _has_active_document(self):
        app = Krita.instance()
        try:
            return bool(app and app.activeWindow() and app.activeWindow().activeView() and app.activeDocument())
        except Exception:
            return False

    def _get_current_view(self):
        try:
            app = Krita.instance()
            if app and app.activeWindow():
                return app.activeWindow().activeView()
        except Exception:
            pass
        return None

    def _get_document_id(self):
        """Best-effort stable id for caching."""
        try:
            doc = Krita.instance().activeDocument()
            if not doc:
                return None
            fname = doc.fileName()
            if fname:
                return fname
            return f"untitled_{id(doc)}"
        except Exception:
            return None

    def _event_global_pos(self, event):
        """Support different event types safely."""
        try:
            return event.globalPos()
        except Exception:
            try:
                # Some events expose globalPosF()
                return event.globalPosF().toPoint()
            except Exception:
                return None

    # ----------------------------
    # Zoom reading (core fix)
    # ----------------------------
    def _doc_dpi(self, view):
        try:
            doc = view.document() if view else None
            if doc:
                dpi = float(doc.resolution())
                if dpi > 0:
                    return dpi
        except Exception:
            pass
        return REFERENCE_DPI

    def _canvas_zoom_raw(self, view):
        """Raw canvas.zoomLevel() (may be DPI-scaled)."""
        try:
            canvas = view.canvas() if view else None
            if canvas:
                return float(canvas.zoomLevel())
        except Exception:
            pass
        return None

    def _canvas_zoom_corrected(self, raw_zoom, dpi):
        """
        Convert zoomLevel() into ZOOM_CONSTANT scale when zoomLevel() is DPI-scaled.
        Community-established correction: constant ≈ raw / (dpi/72).
        """
        try:
            factor = dpi / REFERENCE_DPI
            if factor <= 0:
                return raw_zoom
            return raw_zoom / factor
        except Exception:
            return raw_zoom

    def _zoom_scale_from_ui(self):
        """
        Try to find the *actual zoom widget* showing values like '66.7%'.
        This avoids accidentally reading unrelated '%'-labels (opacity, etc.).
        """
        try:
            win = Krita.instance().activeWindow()
            if not win:
                return None
            qwin = win.qwindow()
            if not qwin:
                return None

            # 1) Prefer widgets whose object/accessibility names suggest zoom.
            candidates = []
            for w in qwin.findChildren((QComboBox, QLineEdit, QToolButton, QLabel)):
                try:
                    obj_name = (w.objectName() or "").lower()
                    acc_name = (w.accessibleName() or "").lower()
                    if "zoom" in obj_name or "zoom" in acc_name:
                        candidates.append(w)
                except Exception:
                    continue

            # 2) If none found, scan status bar children only (not the whole window),
            # but STILL require the text be a pure percent token.
            if not candidates:
                status_bar = qwin.findChild(QStatusBar)
                if status_bar:
                    candidates = status_bar.findChildren((QComboBox, QLineEdit, QToolButton, QLabel))

            def read_percent_text(widget):
                try:
                    if isinstance(widget, QComboBox):
                        text = widget.currentText()
                    elif isinstance(widget, QLineEdit):
                        text = widget.text()
                    elif isinstance(widget, QToolButton):
                        text = widget.text()
                    else:  # QLabel
                        text = widget.text()
                except Exception:
                    return None

                if not text:
                    return None
                m = re.match(r'^\s*([\d.]+)\s*%\s*$', text)
                if not m:
                    return None
                return float(m.group(1))

            # Pick the first good pure-percent token from candidates.
            for w in candidates:
                val = read_percent_text(w)
                if val is not None:
                    return val / 100.0

            # 3) As a last resort, parse window title if it contains '@ xx%'.
            title = qwin.windowTitle() or ""
            m = re.search(r'@\s*([\d.]+)\s*%', title)
            if m:
                return float(m.group(1)) / 100.0

        except Exception:
            return None

        return None

    def _get_current_zoom_scale(self, view):
        """
        Return best estimate of current zoom in ZOOM_CONSTANT scale.
        Strategy:
          - Read UI zoom scale if available (ground truth)
          - Read raw canvas.zoomLevel()
          - Compare raw vs corrected vs UI, choose closest to UI when UI exists
          - If UI missing, choose corrected unless raw is clearly already sane
        """
        ui_scale = self._zoom_scale_from_ui()

        raw = self._canvas_zoom_raw(view)
        if raw is None:
            # fallback: cache or default
            doc_id = self._get_document_id()
            if doc_id and doc_id in self.document_zoom_cache:
                return self.document_zoom_cache[doc_id]
            return ui_scale if ui_scale is not None else 1.0

        dpi = self._doc_dpi(view)
        corrected = self._canvas_zoom_corrected(raw, dpi)

        # If we have UI, pick the closer of raw vs corrected.
        if ui_scale is not None and ui_scale > 0:
            dr = abs(raw - ui_scale)
            dc = abs(corrected - ui_scale)
            return corrected if dc <= dr else raw

        # No UI available: heuristic.
        # Many reports show the broken getter returns values "inflated" by dpi/72.
        # If dpi is far from 72, prefer corrected unless raw is clearly within a sane constant range.
        if abs(dpi - REFERENCE_DPI) > 1e-3:
            # If raw is extremely large for typical viewing (but corrected is reasonable), prefer corrected.
            if raw > 10.0 and corrected <= 10.0:
                return corrected
            # If corrected is absurdly tiny but raw is plausible, keep raw.
            if corrected < 0.01 and raw >= 0.01:
                return raw
            return corrected

        return raw

    # ----------------------------
    # Mode lifecycle
    # ----------------------------
    def activate_zoom_mode(self):
        if not self._has_active_document() or self.zoom_mode_active:
            return

        self.zoom_mode_active = True
        self.is_dragging = False
        self.active_view = None
        self.last_set_zoom_scale = None

        QApplication.setOverrideCursor(self.zoom_cursor)

        if not self.filter_installed:
            QApplication.instance().installEventFilter(self.event_filter)
            self.filter_installed = True

    def deactivate_zoom_mode(self):
        if not self.zoom_mode_active:
            return

        self.zoom_mode_active = False
        self.is_dragging = False
        self.active_view = None

        try:
            QApplication.restoreOverrideCursor()
        except Exception:
            pass

    # ----------------------------
    # Drag + zoom
    # ----------------------------
    def start_drag(self, global_pos):
        if not self._has_active_document() or global_pos is None:
            self.deactivate_zoom_mode()
            return

        self.is_dragging = True
        self.drag_start_x = int(global_pos.x())
        self.active_view = self._get_current_view()

        # Capture correct initial zoom scale for THIS view/document
        zoom_scale = self._get_current_zoom_scale(self.active_view)

        # Persist cache per document
        doc_id = self._get_document_id()
        if doc_id:
            self.document_zoom_cache[doc_id] = zoom_scale

        self.initial_zoom_scale = zoom_scale
        self.last_set_zoom_scale = zoom_scale

    def update_zoom(self, global_pos):
        if not self.is_dragging or global_pos is None:
            return

        view = self.active_view
        if not view:
            return

        try:
            canvas = view.canvas()
            if not canvas:
                return

            delta_x = int(global_pos.x()) - int(self.drag_start_x)

            # Exponential zoom: 2^(dx/sensitivity)
            zoom_factor = 2.0 ** (float(delta_x) / float(self.zoom_sensitivity))
            new_scale = float(self.initial_zoom_scale) * zoom_factor

            # Clamp (1% .. 25600%) in scale space
            new_scale = max(0.01, min(256.0, new_scale))

            canvas.setZoomLevel(new_scale)

            self.last_set_zoom_scale = new_scale
            doc_id = self._get_document_id()
            if doc_id:
                self.document_zoom_cache[doc_id] = new_scale

        except Exception:
            pass

    def end_drag(self):
        self.is_dragging = False
        self.active_view = None
