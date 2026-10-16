#!/usr/bin/env python3
"""Desktop window for the BMP280 / nRF24 weather link.

The same serial reader and logs as weather_ui.py (the terminal UI), in a window:
both boards' readings with a history chart, their health reports, the radio
link, and buttons for every command, including the sample and health-report
intervals.

    python3 weather_gui.py                 # auto-detects the Nano's port
    python3 weather_gui.py --port /dev/ttyACM0 --theme light

Needs: pip install pyserial PySide6
"""

import argparse
import csv
import datetime as dt
import glob
import os
import sys
import tempfile
import time
from bisect import bisect_left

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QFontDatabase, QGuiApplication, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (QApplication, QButtonGroup, QComboBox, QFrame, QGridLayout, QHBoxLayout,
                               QLabel, QLineEdit, QMainWindow, QMessageBox, QPlainTextEdit, QPushButton,
                               QScrollArea, QSizePolicy, QSpinBox, QVBoxLayout, QWidget)

from weather_settings import (DEFAULT_LINK, DEFAULT_RETRY, DEFAULT_SENSOR, FILTER_COEF, OVERSAMPLING, PA_DBM,
                              STANDBY_MS, Link, Retry, Sensor)
from weather_ui import PA_NAMES, State, SerialLink, num, uptime

HISTORY_S = 24 * 3600           # readings kept for the charts
ANSWER_WAIT_S = 2.0             # longest wait for the base to answer a command line before sending the next
LINE_GAP_S = 0.2                # and the pause after its answer
SPANS = [("5 min", 300), ("15 min", 900), ("1 hour", 3600), ("2 hours", 7200), ("6 hours", 21600),
         ("24 hours", 86400)]

THEMES = {
    "dark": dict(bg="#0f0f10", surface="#18181a", raised="#222225", line="#2f2f33", hover="#2a2a2e",
                 text="#ececee", text2="#a6a6ad", muted="#77777f",
                 base="#3987e5", remote="#199e70", accent="#3987e5", on_accent="#ffffff",
                 good="#0ca30c", warn="#fab219", crit="#e05252"),
    "light": dict(bg="#ececee", surface="#f9f9fa", raised="#ffffff", line="#dadade", hover="#f0f0f2",
                  text="#18181b", text2="#4f4f57", muted="#717179",
                  base="#2a78d6", remote="#1baf7a", accent="#2a78d6", on_accent="#ffffff",
                  good="#0a870a", warn="#b77b00", crit="#d03b3b"),
}


def rgba(color, alpha):
    """'#rrggbb' at `alpha` (0-1), for tinted backgrounds."""
    r, g, b = (int(color[i:i + 2], 16) for i in (1, 3, 5))
    return f"rgba({r}, {g}, {b}, {alpha})"


ICON_DIR = os.path.join(tempfile.gettempdir(), f"weather_gui_icons_{os.getpid()}")
CHEVRONS = {"down": "M3 6l5 5 5-5", "up": "M3 10l5-5 5 5"}


def icon(name, color):
    """Path of a small chevron in `color`, as an SVG file: Qt style sheets take arrow images only as files."""
    os.makedirs(ICON_DIR, exist_ok=True)
    path = os.path.join(ICON_DIR, f"{name}_{color.lstrip('#')}.svg")
    if not os.path.exists(path):
        with open(path, "w") as f:
            f.write(f'<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 16 16">'
                    f'<path d="{CHEVRONS[name]}" fill="none" stroke="{color}" stroke-width="1.8" '
                    f'stroke-linecap="round" stroke-linejoin="round"/></svg>')
    return path.replace(os.sep, "/")


# Heights the style sheet relies on. Qt draws a corner square when its radius is more than half the
# widget's height, so every rounded thing has a fixed height and a radius that fits it.
FIELD_H = 32        # boxes, buttons
PILL_H = 24


def qss(t):
    down, up = icon("down", t['text2']), icon("up", t['text2'])
    down_off, up_off = icon("down", t['muted']), icon("up", t['muted'])
    pills = "".join(f"""
    QLabel#pill[tone="{k}"] {{ color: {t[k]}; background: {rgba(t[k], 0.13)}; border-color: {rgba(t[k], 0.45)}; }}"""
                    for k in ("good", "warn", "crit"))
    return f"""
    QWidget {{ color: {t['text']}; font-size: 13px; }}
    QMainWindow, QScrollArea, #page {{ background: {t['bg']}; }}
    QFrame#card {{ background: {t['surface']}; border: 1px solid {t['line']}; border-radius: 14px; }}
    QFrame#strip {{ background: {t['surface']}; border: 1px solid {t['line']}; border-radius: 12px; }}
    QFrame#tile {{ background: {t['raised']}; border-radius: 10px; }}
    QFrame#info {{ background: {t['raised']}; border-radius: 10px; }}
    QFrame#rule {{ background: {t['line']}; max-height: 1px; min-height: 1px; border: none; }}
    QFrame#vrule {{ background: {t['line']}; max-width: 1px; min-width: 1px; border: none; }}
    QLabel {{ background: transparent; }}
    QLabel[role="app"] {{ font-size: 22px; font-weight: 600; }}
    QLabel[role="hero"] {{ font-size: 68px; font-weight: 300; }}
    QLabel[role="herounit"] {{ font-size: 24px; color: {t['text2']}; }}
    QLabel[role="pressure"] {{ font-size: 21px; }}
    QLabel[role="title"] {{ font-size: 17px; font-weight: 600; }}
    QLabel[role="section"] {{ font-size: 14px; font-weight: 600; }}
    QLabel[role="sub"] {{ color: {t['text2']}; }}
    QLabel[role="muted"] {{ color: {t['muted']}; font-size: 12px; }}
    QLabel[role="caps"] {{ color: {t['muted']}; font-size: 11px; font-weight: 600; letter-spacing: 1px; }}
    QLabel[role="tilelabel"] {{ color: {t['text2']}; font-size: 12px; }}
    QLabel[role="tilevalue"] {{ font-size: 16px; font-weight: 600; }}
    QLabel[role="metric"] {{ color: {t['text2']}; }}
    QLabel[tone="good"] {{ color: {t['good']}; }}
    QLabel[tone="warn"] {{ color: {t['warn']}; }}
    QLabel[tone="crit"] {{ color: {t['crit']}; }}
    QLabel[tone="dim"] {{ color: {t['muted']}; }}
    QLabel[role="hero"][tone="dim"] {{ color: {t['muted']}; }}
    QLabel#pill {{ border-radius: {PILL_H // 2}px; padding: 0 11px; font-weight: 600; font-size: 12px;
                   border: 1px solid {t['line']}; background: {t['raised']}; color: {t['text2']}; }}{pills}

    QPushButton {{ background: {t['raised']}; border: 1px solid {t['line']}; border-radius: 8px; padding: 0 14px;
                   min-height: {FIELD_H - 2}px; max-height: {FIELD_H - 2}px; }}
    QPushButton:hover {{ background: {t['hover']}; }}
    QPushButton:pressed {{ background: {t['line']}; }}
    QPushButton:focus {{ border-color: {t['accent']}; }}
    QPushButton:disabled {{ color: {t['muted']}; background: transparent; }}
    QPushButton[kind="primary"] {{ background: {t['accent']}; color: {t['on_accent']}; border-color: {t['accent']};
                                   font-weight: 600; }}
    QPushButton[kind="primary"]:hover {{ border-color: {t['text']}; }}
    QPushButton[kind="primary"]:disabled {{ background: {t['line']}; border-color: {t['line']}; color: {t['muted']}; }}
    QPushButton[kind="danger"] {{ color: {t['crit']}; }}
    QPushButton[kind="danger"]:hover {{ border-color: {t['crit']}; background: {rgba(t['crit'], 0.10)}; }}
    QPushButton[kind="ghost"] {{ background: transparent; border-color: transparent; color: {t['text2']}; }}
    QPushButton[kind="ghost"]:hover {{ background: {t['hover']}; color: {t['text']}; }}

    QFrame#segtrack {{ background: {t['bg']}; border: 1px solid {t['line']}; border-radius: 10px; }}
    QPushButton[kind="seg"] {{ background: transparent; border: 1px solid transparent; border-radius: 7px;
                               padding: 0 14px; min-height: 26px; max-height: 26px; color: {t['text2']}; }}
    QPushButton[kind="seg"]:hover {{ color: {t['text']}; }}
    QPushButton[kind="seg"]:checked {{ background: {t['raised']}; border-color: {t['line']}; color: {t['text']};
                                       font-weight: 600; }}

    QComboBox, QSpinBox, QLineEdit {{ background: {t['raised']}; border: 1px solid {t['line']}; border-radius: 8px;
                   min-height: {FIELD_H - 2}px; max-height: {FIELD_H - 2}px;
                   selection-background-color: {t['accent']}; selection-color: {t['on_accent']}; }}
    QLineEdit {{ padding: 0 10px; }}
    QComboBox {{ padding: 0 30px 0 10px; combobox-popup: 0; }}
    QComboBox:hover, QSpinBox:hover, QLineEdit:hover {{ border-color: {t['muted']}; }}
    QComboBox:focus, QSpinBox:focus, QLineEdit:focus, QComboBox:on {{ border-color: {t['accent']}; }}
    QComboBox::drop-down {{ subcontrol-origin: padding; subcontrol-position: center right; width: 28px;
                           border: none; background: transparent; }}
    QComboBox::down-arrow {{ image: url({down}); width: 12px; height: 12px; }}
    QComboBox::down-arrow:on {{ image: url({up}); }}
    QComboBox::down-arrow:disabled {{ image: url({down_off}); }}
    QComboBox QAbstractItemView {{ background: {t['raised']}; border: 1px solid {t['line']}; border-radius: 10px;
                   padding: 4px; outline: 0; color: {t['text']}; }}
    QComboBox QAbstractItemView::item {{ min-height: 28px; padding: 0 10px; border-radius: 6px; border: none; }}
    QComboBox QAbstractItemView::item:hover {{ background: {t['hover']}; }}
    QComboBox QAbstractItemView::item:selected {{ background: {t['accent']}; color: {t['on_accent']}; }}
    QSpinBox {{ padding: 0 26px 0 10px; }}
    QSpinBox::up-button, QSpinBox::down-button {{ subcontrol-origin: border; width: 24px; border: none;
                   background: transparent; }}
    QSpinBox::up-button {{ subcontrol-position: top right; border-top-right-radius: 8px; margin: 1px 1px 0 0; }}
    QSpinBox::down-button {{ subcontrol-position: bottom right; border-bottom-right-radius: 8px; margin: 0 1px 1px 0; }}
    QSpinBox::up-button:hover, QSpinBox::down-button:hover {{ background: {t['hover']}; }}
    QSpinBox::up-arrow {{ image: url({up}); width: 10px; height: 10px; }}
    QSpinBox::down-arrow {{ image: url({down}); width: 10px; height: 10px; }}
    QSpinBox::up-arrow:disabled, QSpinBox::up-arrow:off {{ image: url({up_off}); }}
    QSpinBox::down-arrow:disabled, QSpinBox::down-arrow:off {{ image: url({down_off}); }}
    QComboBox:disabled, QSpinBox:disabled, QLineEdit:disabled {{ color: {t['muted']}; background: transparent; }}

    QPlainTextEdit {{ background: {t['raised']}; border: 1px solid {t['line']}; border-radius: 10px;
                      padding: 8px; font-family: "Noto Sans Mono", "DejaVu Sans Mono", monospace; font-size: 12px; }}
    QScrollBar:vertical {{ background: transparent; width: 10px; margin: 4px 2px; }}
    QScrollBar::handle:vertical {{ background: {t['line']}; border-radius: 3px; min-height: 30px; }}
    QScrollBar::handle:vertical:hover {{ background: {t['muted']}; }}
    QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px 4px; }}
    QScrollBar::handle:horizontal {{ background: {t['line']}; border-radius: 3px; min-width: 30px; }}
    QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
    QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}
    QToolTip {{ background: {t['raised']}; color: {t['text']}; border: 1px solid {t['line']}; padding: 4px 6px; }}
    QMessageBox {{ background: {t['surface']}; }}
    """


def make_combo(items=(), tip=None):
    """A drop-down whose list opens below it, with rounded corners (its window has no frame of its own)."""
    box = QComboBox()
    box.addItems(list(items))
    if tip:
        box.setToolTip(tip)
    popup = box.view().window()
    popup.setWindowFlags(popup.windowFlags() | Qt.FramelessWindowHint | Qt.NoDropShadowWindowHint)
    popup.setAttribute(Qt.WA_TranslucentBackground)
    box.view().viewport().setAutoFillBackground(False)
    return box


class StatusLabel(QLabel):
    """A wrapping status line that takes no room while it has nothing to say."""

    def __init__(self, role="muted"):
        super().__init__()
        self.setProperty("role", role)
        self.setWordWrap(True)
        self.hide()

    def setText(self, text):
        super().setText(text)
        self.setVisible(bool(text))


def pill(text="", tone=None):
    w = label(text, tone=tone)
    w.setObjectName("pill")
    w.setFixedHeight(PILL_H)
    w.setAlignment(Qt.AlignCenter)
    return w


class Segmented(QFrame):
    """Buttons in a rounded track, one checked at a time. `changed(index)` on a click."""

    def __init__(self, names, changed):
        super().__init__()
        self.setObjectName("segtrack")
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(3, 3, 3, 3)
        lay.setSpacing(2)
        self.group = QButtonGroup(self)
        for i, name in enumerate(names):
            b = QPushButton(name)
            b.setProperty("kind", "seg")
            b.setCheckable(True)
            b.setFocusPolicy(Qt.NoFocus)
            b.setCursor(Qt.PointingHandCursor)
            self.group.addButton(b, i)
            lay.addWidget(b)
        self.group.button(0).setChecked(True)
        self.group.idClicked.connect(changed)

    def select(self, i):
        self.group.button(i).setChecked(True)


class Tabs(QWidget):
    """Pages under a segmented control. Only the current page is shown and laid out, so a short page
    doesn't keep the height of the longest one (QStackedWidget does, once pages have wrapping text)."""

    def __init__(self):
        super().__init__()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(16)
        self.names, self.pages, self.current = [], [], 0
        self.bar_row = QHBoxLayout()
        lay.addLayout(self.bar_row)
        self.body = QVBoxLayout()
        self.body.setContentsMargins(0, 0, 0, 0)
        lay.addLayout(self.body)
        self.bar = None

    def addTab(self, page, name):
        self.body.addWidget(page)
        self.pages.append(page)
        self.names.append(name)

    def finish(self, extra=None):
        """Build the control once all pages are in; `extra` goes at the right of it."""
        self.bar = Segmented(self.names, self.setCurrentIndex)
        self.bar_row.addWidget(self.bar)
        self.bar_row.addStretch()
        if extra:
            self.bar_row.addWidget(extra)
        self.setCurrentIndex(0)

    def currentIndex(self):
        return self.current

    def setCurrentIndex(self, i):
        self.current = i
        if self.bar:
            self.bar.select(i)
        for j, page in enumerate(self.pages):
            page.setVisible(j == i)


def label(text="", role=None, tone=None):
    w = QLabel(text)
    if role:
        w.setProperty("role", role)
    if tone:
        w.setProperty("tone", tone)
    return w


def set_tone(widget, tone):
    if widget.property("tone") != tone:
        widget.setProperty("tone", tone)
        widget.style().unpolish(widget)
        widget.style().polish(widget)


def tabular(font):
    try:
        font.setFeature(QFont.Tag("tnum"), 1)
    except (AttributeError, TypeError):
        pass
    return font


def ago(t):
    if t is None:
        return "never"
    s = time.time() - t
    return f"{s:.0f} s ago" if s < 120 else f"{s / 60:.0f} min ago"


def load_history(logdir, skip, since):
    """Readings from earlier weather_*.csv logs (this window's or weather_ui.py's) newer than `since`.

    Returns {node: (times, temps_c, pressures_hpa)} with node as written in the log ("base", "remote").
    """
    rows = {}
    for path in sorted(glob.glob(os.path.join(logdir, "weather_*.csv"))):
        if os.path.abspath(path) == os.path.abspath(skip) or os.path.getmtime(path) < since:
            continue
        with open(path, newline="") as f:
            for row in csv.reader(f):
                try:
                    ts = dt.datetime.fromisoformat(row[0]).timestamp()
                    if ts < since:
                        continue
                    ok = row[7] == "1"
                    p = float(row[5])
                    rows.setdefault(row[1], []).append((ts, float(row[4]) if ok else None,
                                                        p / 100 if ok and p else None))
                except (ValueError, IndexError):       # the header, or a line cut short by a crash
                    continue
    out = {}
    for node, r in rows.items():
        r.sort(key=lambda q: q[0])
        out[node] = tuple(list(col) for col in zip(*r))
    return out


# ------------------------------------------------------------------ chart --

class HistoryChart(QWidget):
    """One series over time, with a crosshair and readout under the pointer."""

    def __init__(self, theme, color_key):
        super().__init__()
        self.theme, self.color_key = theme, color_key
        self.times, self.values = [], []     # parallel lists, times ascending; value None = no valid reading
        self.unit, self.decimals, self.min_span = "°C", 2, 0.5
        self.span_s = 900
        self.hover_x = None
        self.setMouseTracking(True)
        self.setMinimumHeight(150)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

    def set_series(self, times, values, unit, decimals, min_span):
        self.times, self.values = times, values
        self.unit, self.decimals, self.min_span = unit, decimals, min_span
        self.update()

    def nearest(self, ts, i0):
        """Index of the valid reading closest to ts, at or after index i0."""
        times, vals, n = self.times, self.values, len(self.times)
        k = bisect_left(times, ts, i0, n)
        a, b = k - 1, k
        while a >= i0 and vals[a] is None:
            a -= 1
        while b < n and vals[b] is None:
            b += 1
        if a < i0:
            return b if b < n else None
        return a if b >= n or ts - times[a] <= times[b] - ts else b

    def mouseMoveEvent(self, e):
        self.hover_x = e.position().x()
        self.update()

    def leaveEvent(self, e):
        self.hover_x = None
        self.update()

    def paintEvent(self, e):
        t = self.theme
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        small = QFont(self.font())
        small.setPixelSize(11)
        p.setFont(tabular(small))
        left, right, top, bottom = 58, 12, 10, 22
        plot = QRectF(left, top, self.width() - left - right, self.height() - top - bottom)
        now = time.time()
        t0 = now - self.span_s
        times, vals = self.times, self.values
        n = len(times)
        i0 = bisect_left(times, t0)
        cols = max(1, int(plot.width()))
        if n - i0 > 2 * cols:
            # More readings than pixels (e.g. 24 h at 1 s): one (min, max) per pixel column, None where empty.
            bands, j = [], i0
            for c in range(cols):
                k = bisect_left(times, t0 + (c + 1) * self.span_s / cols, j, n)
                seg = [v for v in vals[j:k] if v is not None]
                bands.append((min(seg), max(seg)) if seg else None)
                j = k
            pts = None
            found = [b for b in bands if b]
        else:
            bands = None
            pts = [(ts, v) for ts, v in zip(times[i0:], vals[i0:]) if v is not None]
            found = pts

        p.setPen(QColor(t["muted"]))
        p.drawText(QRectF(left, plot.bottom() + 4, 120, 16), Qt.AlignLeft,
                   f"{self.span_s // 60} min ago" if self.span_s < 3600 else f"{self.span_s // 3600} h ago")
        p.drawText(QRectF(plot.right() - 60, plot.bottom() + 4, 60, 16), Qt.AlignRight, "now")
        if not found:
            p.drawText(plot, Qt.AlignCenter, "Waiting for readings")
            return

        if bands:
            lo, hi = min(b[0] for b in found), max(b[1] for b in found)
        else:
            lo, hi = min(v for _, v in pts), max(v for _, v in pts)
        if hi - lo < self.min_span:
            mid = (hi + lo) / 2
            lo, hi = mid - self.min_span / 2, mid + self.min_span / 2
        pad = (hi - lo) * 0.12
        lo, hi = lo - pad, hi + pad
        X = lambda ts: plot.left() + (ts - t0) / self.span_s * plot.width()
        Y = lambda v: plot.bottom() - (v - lo) / (hi - lo) * plot.height()

        grid = QPen(QColor(t["line"]), 1)
        for i in range(3):
            v = lo + (hi - lo) * (0.15 + 0.35 * i)
            y = Y(v)
            p.setPen(grid)
            p.drawLine(QPointF(plot.left(), y), QPointF(plot.right(), y))
            p.setPen(QColor(t["muted"]))
            p.drawText(QRectF(0, y - 8, left - 8, 16), Qt.AlignRight | Qt.AlignVCenter, f"{v:.{self.decimals}f}")

        color = QColor(t[self.color_key])
        path = QPainterPath()
        if bands:
            prev_y = None
            for c, band in enumerate(bands):
                if band is None:                          # no readings in this column breaks the line
                    prev_y = None
                    continue
                x, ya, yb = plot.left() + c + 0.5, Y(band[0]), Y(band[1])
                if prev_y is None:
                    path.moveTo(QPointF(x, ya))
                else:
                    if abs(yb - prev_y) < abs(ya - prev_y):
                        ya, yb = yb, ya                   # join from the end nearest the previous column
                    path.lineTo(QPointF(x, ya))
                path.lineTo(QPointF(x, yb))
                prev_y = yb
        else:
            prev_ts = None
            for ts, v in pts:
                pt = QPointF(X(ts), Y(v))
                if prev_ts is None or ts - prev_ts > 15:  # a gap in the readings breaks the line
                    path.moveTo(pt)
                else:
                    path.lineTo(pt)
                prev_ts = ts
        pen = QPen(color, 2)
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        p.setPen(pen)
        p.drawPath(path)

        def dot(pt):
            p.setPen(QPen(QColor(t["surface"]), 2))
            p.setBrush(color)
            p.drawEllipse(pt, 4.5, 4.5)

        last = self.nearest(now, i0)
        dot(QPointF(X(times[last]), Y(vals[last])))

        if self.hover_x is not None and plot.left() <= self.hover_x <= plot.right():
            i = self.nearest(t0 + (self.hover_x - plot.left()) / plot.width() * self.span_s, i0)
            ts, v = times[i], vals[i]
            x, y = X(ts), Y(v)
            p.setPen(QPen(QColor(t["muted"]), 1, Qt.DashLine))
            p.drawLine(QPointF(x, plot.top()), QPointF(x, plot.bottom()))
            dot(QPointF(x, y))
            big = QFont(small)
            big.setPixelSize(13)
            big.setWeight(QFont.DemiBold)
            day = "" if time.localtime(ts)[:3] == time.localtime(now)[:3] else "%a "    # yesterday's: weekday too
            text1, text2 = f"{v:.{self.decimals}f} {self.unit}", time.strftime(day + "%H:%M:%S", time.localtime(ts))
            w = max(p.fontMetrics().horizontalAdvance(text2), 70) + 40
            box = QRectF(x + 10 if x + 10 + w < plot.right() else x - 10 - w, plot.top() + 4, w, 40)
            p.setPen(QPen(QColor(t["line"]), 1))
            p.setBrush(QColor(t["raised"]))
            p.drawRoundedRect(box, 6, 6)
            p.setFont(big)
            p.setPen(QColor(t["text"]))
            p.drawText(box.adjusted(10, 4, -6, -20), Qt.AlignLeft | Qt.AlignVCenter, text1)
            p.setFont(small)
            p.setPen(QColor(t["text2"]))
            p.drawText(box.adjusted(10, 21, -6, -3), Qt.AlignLeft | Qt.AlignVCenter, text2)


# ------------------------------------------------------------- board card --

class Tile(QFrame):
    def __init__(self, name):
        super().__init__()
        self.setObjectName("tile")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 8, 8, 8)
        lay.setSpacing(1)
        self.value = label("--", "tilevalue")
        self.value.setFont(tabular(self.value.font()))
        lay.addWidget(self.value)
        name_label = label(name, "tilelabel")
        name_label.setWordWrap(True)
        lay.addWidget(name_label)

    def set(self, text, tone=None):
        if tone in ("warn", "crit"):
            text = "⚠ " + text           # never colour alone
        self.value.setText(text)
        set_tone(self.value, tone)


class BoardCard(QFrame):
    TILES = [("vdd", "5 V supply"), ("rail", "3.3 V rail"), ("mcu", "MCU temperature"), ("ram", "Free RAM"),
             ("loop", "Longest loop"), ("losses", "Radio power losses"), ("unreach", "Radio unreachable"),
             ("sfails", "Sensor failures"), ("sends", "Radio sends"), ("strong", "Strong replies")]

    def __init__(self, theme, color_key, title):
        super().__init__()
        self.setObjectName("card")
        self.theme, self.color_key = theme, color_key
        self.showing = "temp"
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 16, 20, 18)
        lay.setSpacing(10)

        head = QHBoxLayout()
        self.swatch = QLabel()
        self.swatch.setFixedSize(12, 12)
        head.addWidget(self.swatch)
        self.title = label(title, "title")
        head.addWidget(self.title)
        self.subtitle = label("", "sub")
        head.addWidget(self.subtitle)
        head.addStretch()
        self.sensor = pill()
        self.sensor.hide()
        head.addWidget(self.sensor)
        lay.addLayout(head)

        reading = QHBoxLayout()
        reading.setSpacing(6)
        self.temp = label("--", "hero")
        self.temp.setFont(tabular(self.temp.font()))
        reading.addWidget(self.temp, 0, Qt.AlignBottom)
        unit = label("°C", "herounit")
        unit.setContentsMargins(0, 0, 0, 12)
        reading.addWidget(unit, 0, Qt.AlignBottom)
        reading.addSpacing(28)
        side = QVBoxLayout()
        side.setSpacing(2)
        side.addStretch()
        self.pressure = label("-- hPa", "pressure")
        self.pressure.setFont(tabular(self.pressure.font()))
        side.addWidget(self.pressure)
        self.altitude = label("altitude --", "sub")
        side.addWidget(self.altitude)
        side.addSpacing(12)
        reading.addLayout(side)
        reading.addStretch()
        lay.addLayout(reading)
        self.freshness = label("No reading yet", "muted")
        lay.addWidget(self.freshness)
        self.extra = label("", "muted")
        self.extra.hide()
        lay.addWidget(self.extra)

        tools = QHBoxLayout()
        self.seg = Segmented(["Temperature", "Pressure"], lambda i: self.set_metric(("temp", "press")[i]))
        tools.addWidget(self.seg)
        tools.addStretch()
        self.span = make_combo()
        for text, s in SPANS:
            self.span.addItem(f"Last {text}", s)
        self.span.setCurrentIndex(1)
        self.span.currentIndexChanged.connect(lambda i: self.set_span(self.span.itemData(i)))
        tools.addWidget(self.span)
        lay.addLayout(tools)
        self.chart = HistoryChart(theme, color_key)
        lay.addWidget(self.chart, 1)

        rule = QFrame()
        rule.setObjectName("rule")
        lay.addWidget(rule)
        hh = QHBoxLayout()
        hh.addWidget(label("Health", "section"))
        hh.addStretch()
        self.health_note = label("", "muted")
        hh.addWidget(self.health_note)
        lay.addLayout(hh)
        grid = QGridLayout()
        grid.setSpacing(8)
        self.tiles = {}
        for i, (key, name) in enumerate(self.TILES):
            self.tiles[key] = Tile(name)
            grid.addWidget(self.tiles[key], i // 5, i % 5)
        lay.addLayout(grid)
        self.placeholder = label("", "sub")
        self.placeholder.setWordWrap(True)
        self.placeholder.hide()
        lay.addWidget(self.placeholder)
        self.times, self.temps, self.press = [], [], []     # history, oldest first; None = no valid reading
        self.retheme(theme)

    def retheme(self, theme):
        self.theme = self.chart.theme = theme
        self.swatch.setStyleSheet(f"background: {theme[self.color_key]}; border-radius: 6px;")
        self.chart.update()

    def set_metric(self, key):
        self.showing = key
        self.refresh_chart()

    def set_span(self, s):
        self.chart.span_s = s
        self.chart.update()

    def add_reading(self, r):
        if not self.times or r["time"] > self.times[-1]:
            p = r["pressure_pa"]
            self.times.append(r["time"])
            self.temps.append(r["temp_c"] if r["sensor_ok"] else None)
            self.press.append(p / 100 if p and r["sensor_ok"] else None)
            old = bisect_left(self.times, time.time() - HISTORY_S)
            if old > 600:                                   # trim in batches; the chart skips the rest
                del self.times[:old], self.temps[:old], self.press[:old]
            self.refresh_chart()

    def add_past(self, past):
        """Put readings from earlier logs in front of this session's."""
        times, temps, press = past
        keep = bisect_left(times, self.times[0]) if self.times else len(times)
        self.times[:0], self.temps[:0], self.press[:0] = times[:keep], temps[:keep], press[:keep]
        self.refresh_chart()

    def refresh_chart(self):
        if self.showing == "temp":
            self.chart.set_series(self.times, self.temps, "°C", 2, 0.5)
        else:
            self.chart.set_series(self.times, self.press, "hPa", 2, 0.5)

    def show_placeholder(self, text):
        """Hide everything but the title and say why there's nothing to show."""
        for i in range(1, self.layout().count()):
            item = self.layout().itemAt(i)
            self._visible(item, text is None)
        self.placeholder.setText(text or "")
        self.placeholder.setVisible(text is not None)
        if text is None:
            self.extra.setVisible(bool(self.extra.text()))
            self.sensor.setVisible(bool(self.sensor.text()))

    def _visible(self, item, on):
        if item.widget() and item.widget() is not self.placeholder:
            item.widget().setVisible(on)
        elif item.layout():
            for j in range(item.layout().count()):
                self._visible(item.layout().itemAt(j), on)

    def update_reading(self, r, interval_ms):
        if r is None:
            self.temp.setText("--")
            self.pressure.setText("-- hPa")
            self.altitude.setText("altitude --")
            self.freshness.setText("No reading yet")
            self.sensor.setText("")
            self.sensor.hide()
            return
        self.add_reading(r)
        ok = r["sensor_ok"]
        self.sensor.setText("Sensor OK" if ok else "⚠ Sensor not responding")
        set_tone(self.sensor, "good" if ok else "crit")
        self.sensor.show()
        t, p, a = r["temp_c"], r["pressure_pa"], r["altitude_m"]
        self.temp.setText(f"{t:.2f}" if t is not None and ok else "--")
        self.pressure.setText(f"{p / 100:.2f} hPa" if p and ok else "-- hPa")
        self.altitude.setText(f"altitude {a:.1f} m at standard sea-level pressure" if a is not None and ok
                              else "altitude --")
        age = time.time() - r["time"]
        limit = max(5.0, 3 * (num(str(interval_ms), int) or 1000) / 1000)
        stale = age > limit
        self.freshness.setText(f"⚠ No new reading for {age:.0f} s (sample #{r['seq']})" if stale
                               else f"Sample #{r['seq']}, {ago(r['time'])}")
        set_tone(self.freshness, "warn" if stale else None)
        set_tone(self.temp, "dim" if stale else None)

    def update_health(self, h, period_ms=None):
        if h is None:
            self.health_note.setText("No report yet")
            for tile in self.tiles.values():
                tile.set("--")
            return
        period = period_ms or h.get("period_ms")
        every = f"every {period / 1000:.0f} s" if period else "interval not known yet"
        stale = time.time() - h["time"] > max(25, 2.5 * (period or 10000) / 1000)
        self.health_note.setText(f"{'⚠ ' if stale else ''}Reported {ago(h['time'])}, {every}")
        set_tone(self.health_note, "warn" if stale else None)
        vdd, rail = num(h["vdd_mv"], int), num(h["rail_3v3_mv"], int)
        self.tiles["vdd"].set(f"{vdd / 1000:.2f} V" if vdd else "--", "warn" if vdd and vdd < 4300 else None)
        self.tiles["rail"].set(f"{rail / 1000:.2f} V" if rail else "--",
                               "warn" if rail and not 3000 <= rail <= 3600 else None)
        self.tiles["mcu"].set(f"{h['mcu_temp_c']} °C")
        self.tiles["ram"].set(f"{h['free_ram']} B")
        self.tiles["loop"].set(f"{h['max_loop_ms']} ms")
        for key, col in (("losses", "radio_power_losses"), ("unreach", "radio_unreachable"),
                         ("sfails", "sensor_fails")):
            n = num(h[col], int) or 0
            self.tiles[key].set(str(n), "crit" if n else "good")
        span = f"{period / 1000:.0f} s" if period else "report"
        self.tiles["sends"].set(f"{h['sends']} per {span}")
        strong = h["strong_pct"]
        self.tiles["strong"].set("none heard" if strong == "nan" else f"{strong} %")   # nan = no replies heard


# ---------------------------------------------------------------- window --

class Window(QMainWindow):
    RATE_ORDER = [2, 0, 1]           # the rate box lists 250 kbps, 1 Mbps, 2 Mbps; these are their RF24 codes

    def __init__(self, state, link, theme_name):
        super().__init__()
        self.state, self.link = state, link
        self.theme_name = theme_name
        self.theme = THEMES[theme_name]
        self.log_seen = 0
        self.synced = set()          # settings boxes already filled from the boards
        self.pending = {}            # control name -> (time sent, [(where, cmd name)], label for the answer)
        self.outbox = []             # this board's commands waiting for the line before to be answered
        self.remote_queue = []       # remote commands waiting for the one before to finish
        self.awaiting = None         # (L or R, cmd name, time sent) of the line the base hasn't answered yet
        self.answered = 0.0          # when it last answered
        self.remote_busy = None      # (cmd name, time sent) of the remote command the base is working on
        # the last 24 h from earlier runs' logs; goes into the charts once we know which board is plugged in
        self.past = load_history(os.path.dirname(state.csv_path), state.csv_path, time.time() - HISTORY_S)
        self.setWindowTitle("Weather link")
        self.resize(1400, 1000)

        page = QWidget()
        page.setObjectName("page")
        root = QVBoxLayout(page)
        root.setContentsMargins(20, 16, 20, 20)
        root.setSpacing(14)

        # header
        head = QHBoxLayout()
        head.setSpacing(10)
        head.addWidget(label("Weather link", "app"))
        head.addSpacing(6)
        self.conn = pill("● Disconnected", "crit")
        head.addWidget(self.conn)
        self.port = label("", "sub")
        head.addWidget(self.port)
        head.addStretch()
        self.board_info = label("", "sub")
        head.addWidget(self.board_info)
        self.theme_button = self.button("", lambda: self.set_theme("light" if self.theme_name == "dark" else "dark"),
                                        "ghost", "Switch between the dark and light look")
        head.addWidget(self.theme_button)
        root.addLayout(head)

        cards = QHBoxLayout()
        cards.setSpacing(16)
        self.local_card = BoardCard(self.theme, "base", "This board")
        self.remote_card = BoardCard(self.theme, "remote", "Remote board")
        cards.addWidget(self.local_card, 1)
        cards.addWidget(self.remote_card, 1)
        root.addLayout(cards, 1)

        strip = QFrame()
        strip.setObjectName("strip")
        sl = QHBoxLayout(strip)
        sl.setContentsMargins(18, 10, 18, 10)
        sl.setSpacing(20)
        sl.addWidget(label("Radio link", "section"))
        self.link_pill = pill("--")
        sl.addWidget(self.link_pill)
        self.link_metrics = []
        for _ in range(5):
            m = label("", "metric")
            m.setFont(tabular(m.font()))
            self.link_metrics.append(m)
            sl.addWidget(m)
        sl.addStretch()
        root.addWidget(strip)

        root.addWidget(self.build_controls())
        root.addWidget(self.build_log())

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setWidget(page)
        self.setCentralWidget(scroll)
        self.apply_theme()

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(250)
        self.refresh()

    # ----- building
    def card(self):
        c = QFrame()
        c.setObjectName("card")
        lay = QVBoxLayout(c)
        lay.setContentsMargins(22, 18, 22, 20)
        lay.setSpacing(12)
        return c, lay

    def button(self, text, handler, kind=None, tip=None):
        b = QPushButton(text)
        if kind:
            b.setProperty("kind", kind)
        if tip:
            b.setToolTip(tip)
        b.setCursor(Qt.PointingHandCursor)
        b.clicked.connect(handler)
        return b

    @staticmethod
    def column():
        lay = QVBoxLayout()
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(10)
        return lay

    def tab(self, *columns):
        """A page with its columns side by side, a thin rule between them, each one top-aligned."""
        w = QWidget()
        row = QHBoxLayout(w)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(28)
        for i, col in enumerate(columns):
            if i:
                rule = QFrame()
                rule.setObjectName("vrule")
                row.addWidget(rule)
            col.addStretch()
            row.addLayout(col, 1)
        return w

    @staticmethod
    def combo(items, tip=None):
        return make_combo(items, tip)

    @staticmethod
    def spin(lo, hi, tip=None):
        s = QSpinBox()
        s.setRange(lo, hi)
        s.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        if tip:
            s.setToolTip(tip)
        return s

    @staticmethod
    def note(text="", role="muted"):
        w = label(text, role)
        w.setWordWrap(True)
        return w

    @staticmethod
    def rule():
        r = QFrame()
        r.setObjectName("rule")
        return r

    def info(self, title):
        """A tinted box that explains what the settings above it do. Returns (box, its text label)."""
        box = QFrame()
        box.setObjectName("info")
        lay = QVBoxLayout(box)
        lay.setContentsMargins(14, 12, 14, 12)
        lay.setSpacing(6)
        lay.addWidget(label(title.upper(), "caps"))
        text = self.note(role="sub")
        lay.addWidget(text)
        return box, text

    @staticmethod
    def actions(*widgets, left=None):
        """A row of buttons on the right, with `left` (a widget or layout) at the left."""
        row = QHBoxLayout()
        row.setSpacing(8)
        if isinstance(left, QWidget):
            row.addWidget(left)
        elif left is not None:
            row.addLayout(left)
        row.addStretch()
        for w in widgets:
            row.addWidget(w)
        return row

    FIELD_W = 210

    def form(self, rows):
        """Label on the left, a box of the same width on the right for every row."""
        grid = QGridLayout()
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(8)
        for i, (text, widget, tip) in enumerate(rows):
            name = label(text)
            name.setToolTip(tip)
            widget.setToolTip(tip)
            widget.setFixedWidth(self.FIELD_W)
            grid.addWidget(name, i, 0)
            grid.addWidget(widget, i, 1)
        grid.setColumnStretch(0, 1)
        return grid

    def section(self, col, title, text=None):
        col.addWidget(label(title, "section"))
        if text:
            col.addWidget(self.note(text))

    def build_controls(self):
        c, lay = self.card()
        self.remote_buttons = []           # enabled only when the base is plugged in
        self.tabs = Tabs()
        self.tabs.addTab(self.build_sensor_tab(), "Sensor")
        self.tabs.addTab(self.build_radio_tab(), "Radio")
        self.tabs.addTab(self.build_timing_tab(), "Timing")
        self.tabs.addTab(self.build_boards_tab(), "Boards")
        self.tabs.finish(label("Settings are kept by each board after a restart", "muted"))
        lay.addWidget(self.tabs)
        c.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Minimum)   # never squeezed: the page scrolls instead
        self.controls_built = True
        self.explain_sensor()
        self.explain_radio()
        return c

    def build_sensor_tab(self):
        left, right = self.column(), self.column()
        self.section(left, "BMP280 sensor", "How each board's pressure and temperature sensor measures.")
        self.osrs_t = self.combo([f"×{n}" for n in OVERSAMPLING[1:]])
        self.osrs_p = self.combo([f"×{n}" for n in OVERSAMPLING[1:]])
        self.filter_box = self.combo(["Off"] + [f"×{n}" for n in FILTER_COEF[1:]])
        self.mode_box = self.combo(["Continuous", "One per reading"])
        self.standby_box = self.combo([f"{ms:g} ms" for ms in STANDBY_MS])
        self.sensor_target = self.combo(["Both boards", "This board", "Remote board"])
        left.addLayout(self.form([
            ("Temperature oversampling", self.osrs_t,
             "Measurements averaged into each temperature value. The pressure is corrected with it."),
            ("Pressure oversampling", self.osrs_p, "Measurements averaged into each pressure value: finer, slower."),
            ("Smoothing filter", self.filter_box,
             "Blends each new measurement into the previous output: higher is smoother and slower to follow."),
            ("Measuring", self.mode_box,
             "Continuous: the chip measures on its own clock. One per reading: it sleeps until the board asks."),
            ("Rest between measurements", self.standby_box, "Continuous only: the pause after each measurement."),
            ("Apply to", self.sensor_target, "Which board gets these settings"),
        ]))
        for box in (self.osrs_t, self.osrs_p, self.filter_box, self.mode_box, self.standby_box):
            box.currentIndexChanged.connect(self.explain_sensor)
        left.addSpacing(4)
        left.addLayout(self.actions(
            self.button("Defaults", lambda: self.set_sensor_controls(DEFAULT_SENSOR),
                        tip="Put the default settings in the boxes (not applied yet)"),
            self.button("Apply", self.apply_sensor, "primary")))
        self.sensor_result = StatusLabel()
        left.addWidget(self.sensor_result)

        box, self.sensor_explain = self.info("What these settings do")
        right.addWidget(box)
        right.addWidget(label("Now on the boards", "section"))
        self.sensor_now = StatusLabel("sub")
        right.addWidget(self.sensor_now)
        self.set_sensor_controls(DEFAULT_SENSOR)
        return self.tab(left, right)

    def build_radio_tab(self):
        left, right = self.column(), self.column()
        self.section(left, "Link", "Both boards must use the same channel, rate and CRC, so these change on both: "
                                   "the remote confirms and switches, then the base follows. If they can't hear "
                                   "each other afterwards, both go back to the earlier settings on their own.")
        self.channel_spin = self.spin(0, 125)
        self.rate_box = self.combo(["250 kbps", "1 Mbps", "2 Mbps"])      # RF24 codes 2, 0, 1
        self.crc_box = self.combo(["16-bit", "8-bit"])                    # RF24 codes 2, 1
        left.addLayout(self.form([
            ("Channel", self.channel_spin, "Frequency = 2400 MHz + channel"),
            ("Data rate", self.rate_box, "Slower rates reach further"),
            ("Error check (CRC)", self.crc_box, "Checksum on every packet; damaged packets are dropped"),
        ]))
        for wdg in (self.channel_spin, self.rate_box, self.crc_box):
            sig = wdg.valueChanged if isinstance(wdg, QSpinBox) else wdg.currentIndexChanged
            sig.connect(self.explain_radio)
        box, self.link_explain = self.info("On these settings")
        left.addWidget(box)
        self.link_apply = self.button("Move both boards…", self.apply_link, "primary")
        left.addLayout(self.actions(
            self.button("Defaults", lambda: self.set_link_controls(DEFAULT_LINK),
                        tip="Put the default settings in the boxes (not applied yet)"),
            self.link_apply))
        self.link_result = StatusLabel()
        left.addWidget(self.link_result)
        self.link_alert = StatusLabel()
        set_tone(self.link_alert, "warn")
        left.addWidget(self.link_alert)
        self.link_now = StatusLabel("sub")
        left.addWidget(self.link_now)

        self.section(right, "Transmit power",
                     "Per board, as the nRF24 chip's output; the module's amplifier adds its gain on top. "
                     "HIGH and MAX can deafen a receiver that is only a few metres away.")
        grid = QGridLayout()
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(8)
        pa_items = [f"{n}  ·  {PA_DBM[i]}" for i, n in enumerate(PA_NAMES)]
        for i, (where, text) in enumerate((("L", "This board"), ("R", "Remote board"))):
            box = self.combo(pa_items, "The nRF24 chip's own output; the module's amplifier adds its gain on top")
            box.setFixedWidth(self.FIELD_W)
            set_pa = self.button("Set", lambda _=False, wh=where, b=box: self.apply_pa(wh, b.currentIndex()))
            grid.addWidget(label(text), i, 0)
            grid.addWidget(box, i, 1)
            grid.addWidget(set_pa, i, 2)
            if where == "L":
                self.local_pa = box
            else:
                self.remote_pa = box
                self.remote_buttons += [box, set_pa]
        grid.setColumnStretch(0, 1)
        right.addLayout(grid)
        self.pa_result = StatusLabel()
        right.addWidget(self.pa_result)

        right.addSpacing(6)
        right.addWidget(self.rule())
        right.addSpacing(6)
        self.section(right, "Remote's retries",
                     "Only the remote transmits (the base just answers), so these are the remote's.")
        self.ard_box = self.combo([f"{250 * (i + 1)} µs" for i in range(16)])
        self.arc_spin = self.spin(0, 15)
        self.attempts_spin = self.spin(1, 9)
        right.addLayout(self.form([
            ("Wait between tries", self.ard_box, "How long the radio waits for the base's reply before trying again"),
            ("Retries per send", self.arc_spin, "Automatic re-tries by the radio chip itself"),
            ("Sends per reading", self.attempts_spin,
             "The firmware starts the whole send again this many times, 3-14 ms apart"),
        ]))
        for wdg in (self.ard_box, self.arc_spin, self.attempts_spin):
            sig = wdg.valueChanged if isinstance(wdg, QSpinBox) else wdg.currentIndexChanged
            sig.connect(self.explain_radio)
        self.retry_explain = self.note(role="sub")
        right.addWidget(self.retry_explain)
        retry_apply = self.button("Apply", self.apply_retry, "primary")
        right.addLayout(self.actions(
            self.button("Defaults", lambda: self.set_retry_controls(DEFAULT_RETRY),
                        tip="Put the default settings in the boxes (not applied yet)"),
            retry_apply))
        self.retry_result = StatusLabel()
        right.addWidget(self.retry_result)
        self.retry_now = StatusLabel("sub")
        right.addWidget(self.retry_now)
        self.remote_buttons += [self.link_apply, retry_apply]
        self.set_link_controls(DEFAULT_LINK)
        self.set_retry_controls(DEFAULT_RETRY)
        return self.tab(left, right)

    def build_timing_tab(self):
        left, right = self.column(), self.column()
        self.section(left, "Readings", "How often both boards measure, show and send a reading.")
        self.interval_spin = self.spin(200, 60000)
        self.interval_spin.setSingleStep(100)
        self.interval_spin.setSuffix(" ms")
        self.interval_spin.setValue(1000)
        self.interval_spin.valueChanged.connect(self.explain_sensor)
        self.interval_spin.setFixedWidth(self.FIELD_W)
        left.addLayout(self.actions(self.interval_spin,
                                    self.button("Apply", lambda: self.apply_timing("interval"), "primary"),
                                    left=label("Take a reading every")))
        self.interval_now = label("", "muted")
        left.addWidget(self.interval_now)

        self.section(right, "Health reports", "Supply voltages, radio and sensor counters, for the logs.")
        self.health_spin = self.spin(2, 60)
        self.health_spin.setSuffix(" s")
        self.health_spin.setValue(10)
        self.health_spin.setFixedWidth(self.FIELD_W)
        self.health_apply = self.button("Apply", lambda: self.apply_timing("health"), "primary")
        right.addLayout(self.actions(self.health_spin, self.health_apply, left=label("Send a health report every")))
        self.health_now = label("", "muted")
        right.addWidget(self.health_now)
        self.timing_result = self.note("Applies to both boards, and each keeps it after a restart.")
        left.addWidget(self.timing_result)
        return self.tab(left, right)

    def build_boards_tab(self):
        cols = []
        for where, title in (("R", "Remote board"), ("L", "This board")):
            col = self.column()
            self.section(col, title, "Through the base, by radio." if where == "R" else "On USB.")
            pre = "remote " if where == "R" else ""
            btns = []
            if where == "R":
                btns.append(self.button("Ping", lambda: self.send_remote("remote ping")))
            btns += [self.button("Restart radio", lambda _=False, p=pre: self.command(p + "radio"),
                                 tip="Re-initialise the nRF24"),
                     self.button("Find sensor", lambda _=False, p=pre: self.command(p + "sensor"),
                                 tip="Re-detect the BMP280")]
            if where == "L":
                btns.append(self.button("Wiring check", lambda: self.send("diag"),
                                        tip="Measures the I2C and radio lines; results appear in the log"))
            row = QHBoxLayout()
            row.setSpacing(8)
            for b in btns:
                row.addWidget(b)
            row.addStretch()
            col.addLayout(row)
            reset = self.button("Restart board…", lambda _=False, wh=where: self.reset(wh), "danger")
            col.addLayout(self.actions(reset))
            if where == "R":
                self.remote_buttons += btns + [reset]
            cols.append(col)
        return self.tab(*cols)

    def build_log(self):
        c, lay = self.card()
        hl = QHBoxLayout()
        hl.setSpacing(12)
        hl.addWidget(label("Log", "section"))
        hl.addStretch()
        self.logpath = label(f"Readings go to {self.state.csv_path}; everything the board prints to serial_*.log",
                             "muted")
        self.logpath.setTextInteractionFlags(Qt.TextSelectableByMouse)
        hl.addWidget(self.logpath)
        self.clear_log = self.button("Clear", self.log_clear, "ghost",
                                     "Empty this view. The log files on disk keep everything.")
        hl.addWidget(self.clear_log)
        lay.addLayout(hl)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(2000)
        self.log.setMinimumHeight(220)
        lay.addWidget(self.log, 1)
        self.cmd = QLineEdit()
        self.cmd.setPlaceholderText("Type a firmware command, e.g. status, help, remote ping")
        self.cmd.returnPressed.connect(self.send_typed)
        row = QHBoxLayout()
        row.setSpacing(8)
        row.addWidget(self.cmd, 1)
        row.addWidget(self.button("Send", self.send_typed))
        lay.addLayout(row)
        return c

    def apply_theme(self):
        self.setStyleSheet(qss(self.theme))
        self.theme_button.setText("☀ Light" if self.theme_name == "dark" else "☾ Dark")
        for card in (self.local_card, self.remote_card):
            card.retheme(self.theme)

    def set_theme(self, name):
        self.theme_name, self.theme = name, THEMES[name]
        self.apply_theme()

    # ----- actions
    def send(self, text):
        self.link.send(text)

    def command(self, text):
        """A board command; the remote's go through their own queue."""
        if text.startswith("remote "):
            self.send_remote(text)
        else:
            self.send_local(text)

    def send_local(self, text):
        self.outbox.append(text)
        self.pump_now()

    def send_remote(self, text):
        """The base holds one remote command at a time and a newer one replaces it, so send them one by one."""
        self.remote_queue.append(text)
        self.pump_now()

    def pump_now(self):
        with self.state.lock:
            commands = dict(self.state.commands)
        self.pump(commands)

    def remote_timeout(self):
        """How long the base keeps trying a remote command (its own limit), plus a little."""
        with self.state.lock:
            every = num(self.state.link.get("remote_interval", ""), int) or 1000
        return max(30, 20 * every / 1000 + 5) + 5

    def pump(self, commands):
        """Send the next queued command, one line at a time: the next goes only once the base has answered the
        one before (CMD,L, or CMD,R,…,QUEUED) or ANSWER_WAIT_S has passed. Sent back to back, a line could
        arrive garbled: after `bmp` moved the base from one per reading to continuous, a `remote bmp` straight
        after came out as "not a command"."""
        now = time.time()
        if self.awaiting:
            where, name, sent = self.awaiting
            r = commands.get((where, name))
            if r and r[1] >= sent:
                self.awaiting, self.answered = None, r[1]
            elif now - sent < ANSWER_WAIT_S:
                return
            else:
                self.awaiting, self.answered = None, now
        if now - self.answered < LINE_GAP_S:
            return
        if self.outbox:
            text = self.outbox.pop(0)
            self.send(text)
            self.awaiting = ("L", text.split()[0].lower(), now)
            return
        if self.remote_busy:
            name, sent = self.remote_busy
            r = commands.get(("R", name))
            if not (r and r[1] >= sent and r[0] != "QUEUED") and now - sent < self.remote_timeout():
                return
            self.remote_busy = None
        if self.remote_queue:
            text = self.remote_queue.pop(0)
            self.send(text)
            name = text.split()[1].lower()
            self.remote_busy = (name, now)
            self.awaiting = ("R", name, now)

    def log_clear(self):
        self.log.clear()

    def send_typed(self):
        text = self.cmd.text().strip()
        if text:
            self.send(text)
            self.cmd.clear()

    def reset(self, where):
        which = "the remote board" if where == "R" else "this board"
        if QMessageBox.question(self, "Restart board", f"Restart {which}? Its link counters start again from zero."
                                ) == QMessageBox.Yes:
            self.send("remote reset" if where == "R" else "reset")

    def apply_setting(self, key, name, arg, out, local=True, remote=True):
        """Send `name arg` to this board and/or the remote, and report their answers in `out`."""
        targets = []
        if local:
            self.send_local(f"{name} {arg}")
            targets.append(("L", name))
        if remote and self.state.role == "BASE":
            self.send_remote(f"remote {name} {arg}")
            targets.append(("R", name))
        if not targets:
            out.setText("⚠ Remote settings go through the base board: plug the base in.")
            set_tone(out, "warn")
            return
        self.pending[key] = (time.time(), targets, out)
        out.setText("Sent. Waiting for the board to confirm…" if len(targets) == 1
                    else "Sent. Waiting for the boards to confirm…")
        set_tone(out, None)

    def apply_timing(self, what):
        if what == "interval":
            self.apply_setting(what, "interval", self.interval_spin.value(), self.timing_result)
        else:
            self.apply_setting(what, "health", self.health_spin.value() * 1000, self.timing_result)

    def apply_sensor(self):
        target = self.sensor_target.currentIndex()        # both, this board, remote
        self.apply_setting("sensor", "bmp", self.sensor_from_controls().code, self.sensor_result,
                           local=target in (0, 1), remote=target in (0, 2))

    def apply_pa(self, where, level):
        self.apply_setting("pa", "pa", level, self.pa_result, local=where == "L", remote=where == "R")

    def apply_retry(self):
        self.apply_setting("retry", "retry", self.retry_from_controls().code, self.retry_result, local=False)

    def apply_link(self):
        new = self.link_from_controls()
        with self.state.lock:
            now = Link.from_code(self.state.status.get("link"))
            every = num(self.state.link.get("remote_interval", ""), int) or 1000
        if now == new:
            self.link_result.setText("Both boards are already on these settings.")
            set_tone(self.link_result, None)
            return
        back = max(15, 3 * every / 1000 + 5) + 10
        if QMessageBox.question(
                self, "Move both boards",
                f"Move both boards to {new.summary()}?\n\nIf they can't hear each other there, both go back to "
                f"the earlier settings on their own, about {back:.0f} s later.") != QMessageBox.Yes:
            return
        self.apply_setting("link", "link", new.code, self.link_result, local=False)

    # ----- the settings boxes
    def set_sensor_controls(self, s):
        for box, i in ((self.osrs_t, s.osrs_t - 1), (self.osrs_p, s.osrs_p - 1), (self.filter_box, s.filter),
                       (self.mode_box, s.forced), (self.standby_box, s.standby)):
            box.setCurrentIndex(i)
        self.explain_sensor()

    def sensor_from_controls(self):
        return Sensor(self.osrs_t.currentIndex() + 1, self.osrs_p.currentIndex() + 1, self.filter_box.currentIndex(),
                      self.standby_box.currentIndex(), self.mode_box.currentIndex())

    def set_link_controls(self, ln):
        self.channel_spin.setValue(ln.channel)
        self.rate_box.setCurrentIndex(self.RATE_ORDER.index(ln.rate))
        self.crc_box.setCurrentIndex(0 if ln.crc == 2 else 1)
        self.explain_radio()

    def link_from_controls(self):
        return Link(self.channel_spin.value(), self.RATE_ORDER[self.rate_box.currentIndex()],
                    2 if self.crc_box.currentIndex() == 0 else 1)

    def set_retry_controls(self, r):
        self.ard_box.setCurrentIndex(r.delay)
        self.arc_spin.setValue(r.count)
        self.attempts_spin.setValue(r.attempts)
        self.explain_radio()

    def retry_from_controls(self):
        return Retry(self.ard_box.currentIndex(), self.arc_spin.value(), self.attempts_spin.value())

    def show_lines(self, widget, lines):
        widget.setText("\n".join(lines))
        set_tone(widget, "warn" if any(x.startswith("⚠") for x in lines) else None)

    def explain_sensor(self):
        if not getattr(self, "controls_built", False):
            return
        s = self.sensor_from_controls()
        self.standby_box.setEnabled(not s.forced)
        self.show_lines(self.sensor_explain, s.explain(self.interval_spin.value()))

    def explain_radio(self):
        if not getattr(self, "controls_built", False):
            return
        ln = self.link_from_controls()
        self.show_lines(self.link_explain, ln.explain())
        with self.state.lock:
            every = num(self.state.link.get("remote_interval", ""), int) or 1000
        # the retries run on whichever link the boards are on: the one in the boxes once it's applied
        self.show_lines(self.retry_explain, self.retry_from_controls().explain(ln, every))

    # ----- refresh
    def refresh(self):
        s = self.state
        with s.lock:
            conn, port, role = s.connected, s.port, s.role
            st, link, tx = dict(s.status), dict(s.link), dict(s.tx)
            local, remote = s.readings["L"], s.readings["R"]
            hl, hr = s.health["L"], s.health["R"]
            commands = dict(s.commands)
            total, lines = s.log_total, list(s.log)

        self.conn.setText("● Connected" if conn else "● Disconnected")
        set_tone(self.conn, "good" if conn else "crit")
        self.port.setText(port or "looking for a board")
        self.board_info.setText(f"{role.title()} board, up {uptime(st.get('uptime_ms'))}" if role and st
                                else "Board not identified yet")

        if self.past is not None and role:
            # the log names boards by role: here "base" is the board on USB and "remote" the one by radio,
            # unless the remote itself is on USB
            remote_node = "remote" if role == "BASE" else None
            for card, node in ((self.local_card, role.lower()), (self.remote_card, remote_node)):
                if node in self.past:
                    card.add_past(self.past[node])
            self.past = None
        self.local_card.subtitle.setText(f"{role.lower()}, on USB" if role else "on USB")
        self.local_card.update_reading(local, st.get("interval"))
        self.local_card.update_health(hl, num(st.get("health", ""), int))
        if st:
            self.local_card.extra.setText(
                f"{'Radio OK' if st.get('radio_ok') == '1' else '⚠ Radio down'}, power {self.pa(st.get('pa'))}, "
                f"reading every {st.get('interval', '--')} ms, radio restarts {st.get('inits', '--')}, "
                f"last restart {st.get('reset', '--').strip().lower() or '--'}")
        self.local_card.extra.setVisible(bool(st))
        if role == "BASE":
            self.remote_card.show_placeholder(None)
            self.remote_card.subtitle.setText("by radio")
            self.remote_card.update_reading(remote, link.get("remote_interval"))
            self.remote_card.update_health(hr)
            self.remote_card.extra.setText(
                f"Radio power {self.pa(link.get('remote_pa'))}, reading every {link.get('remote_interval', '--')} ms, "
                f"radio restarts {link.get('remote_inits', '--')}, "
                f"last restart {link.get('remote_reset', '--').strip().lower() or '--'}")
            self.remote_card.extra.setVisible(bool(link))
        else:
            self.remote_card.show_placeholder(
                "This is the remote board. Plug in the base board to see both." if role == "REMOTE"
                else "The remote's readings arrive through the base board once it's connected.")
        for w in self.remote_buttons:
            w.setEnabled(role == "BASE")
            w.setToolTip("" if role == "BASE" else "Remote commands go through the base board")

        self.update_link(role, st, link, tx, remote)
        self.update_timing(role, st, link, hl, hr)
        self.sync_controls(st, link)
        self.update_settings(role, st, link)
        self.update_pending(commands)
        self.pump(commands)

        new = total - self.log_seen
        if new > 0:
            bar = self.log.verticalScrollBar()
            at_end = bar.value() >= bar.maximum() - 4
            for line in lines[-min(new, len(lines)):]:
                self.log.appendPlainText(line)
            if at_end:
                bar.setValue(bar.maximum())
            self.log_seen = total

    @staticmethod
    def pa(v):
        try:
            return PA_NAMES[int(v)].lower()
        except (TypeError, ValueError, IndexError):
            return "--"

    def update_link(self, role, st, link, tx, remote):
        m = self.link_metrics
        searching = st.get("searching") == "1"
        if role == "BASE" and link:
            up = link.get("up") == "1"
            one_way = up and link.get("remote_searching") == "1"
            self.link_pill.setText("⚠ One way" if one_way else "Up" if up else "⚠ Searching" if searching
                                   else "⚠ Down")
            set_tone(self.link_pill, "warn" if one_way else "good" if up else "crit")
            rx, lost = num(link.get("rx", ""), int) or 0, num(link.get("lost", ""), int) or 0
            loss = 100.0 * lost / (rx + lost) if rx + lost else 0.0
            sig = {"1": "strong", "0": "weak"}.get(link.get("strong"), "--")
            fails, rseq = num(link.get("remote_tx_fails", ""), int), remote["seq"] if remote else None
            missed = "--" if fails is None else f"{fails}"
            if fails is not None and rseq and rseq >= fails:
                missed += f" ({100.0 * fails / rseq:.1f} %)"
            retry = Retry.from_code(link.get("remote_retry")) or DEFAULT_RETRY
            texts = [f"{rx} received", f"{lost} lost ({loss:.1f} %)", f"signal {sig}",
                     f"{link.get('arc', '--')} of {retry.count} retries", f"replies missed by remote {missed}"]
        elif role == "REMOTE" and tx:
            ok = tx.get("ok") == "1"
            self.link_pill.setText("Acknowledged" if ok else "⚠ Searching" if searching else "⚠ No acknowledgement")
            set_tone(self.link_pill, "good" if ok else "crit")
            retry = Retry.from_code(st.get("retry")) or DEFAULT_RETRY
            texts = [f"{tx.get('tx_ok', '--')} sent", f"{tx.get('tx_fails', '--')} failed",
                     f"{tx.get('arc', '--')} of {retry.count} retries", f"last send {ago(tx.get('time'))}", ""]
        else:
            self.link_pill.setText("No report yet")
            set_tone(self.link_pill, "dim")
            texts = [""] * 5
        for w, text in zip(m, texts):
            w.setText(text)

    def update_timing(self, role, st, link, hl, hr):
        here = st.get("interval")
        there = link.get("remote_interval") if role == "BASE" else None
        txt = f"This board: {here or '--'} ms"
        if role == "BASE":
            txt += f".  Remote: {there or '--'} ms"
        self.interval_now.setText(txt)

        hms = num(st.get("health", ""), int)
        here_h = f"{hms / 1000:.0f} s" if hms else (
            f"about {hl['period_ms'] / 1000:.0f} s" if hl and hl.get("period_ms") else "--")
        txt = f"This board: {here_h}"
        if role == "BASE":
            rp = hr.get("period_ms") if hr else None
            txt += f".  Remote: about {rp / 1000:.0f} s" if rp else ".  Remote: known after its next two reports"
        self.health_now.setText(txt)
        if hms is None and st:
            self.health_now.setText(txt + "  ⚠ firmware too old to change this")

    def sync_controls(self, st, link):
        """Fill the boxes from the boards once, when their settings first arrive."""
        todo = [("timing", st.get("interval"), self.sync_timing),
                ("sensor", Sensor.from_code(st.get("bmp")), self.set_sensor_controls),
                ("link", Link.from_code(st.get("link")), self.set_link_controls),
                ("local_pa", num(st.get("pa", ""), int), self.local_pa.setCurrentIndex),
                ("remote_pa", num(link.get("remote_pa", ""), int), self.remote_pa.setCurrentIndex),
                ("retry", Retry.from_code(link.get("remote_retry")) or Retry.from_code(st.get("retry"))
                 if st.get("role") == "REMOTE" or link.get("remote_retry") else None, self.set_retry_controls)]
        for key, value, fill in todo:
            if key not in self.synced and value is not None:
                fill(value)
                self.synced.add(key)

    def sync_timing(self, interval):
        with self.state.lock:
            hms = num(self.state.status.get("health", ""), int)
        self.interval_spin.setValue(num(interval, int) or 1000)
        if hms:
            self.health_spin.setValue(hms // 1000)

    def update_settings(self, role, st, link):
        def now(code, cls, where, *extra):
            v = cls.from_code(code)
            if v:
                return f"{where} now: {v.summary(*extra)}"
            if code == "-" or (st and code is None):
                return f"{where}: older firmware, can't change this yet"
            return f"{where} now: --"

        cur = Link.from_code(st.get("link"))
        lines = [now(st.get("bmp"), Sensor, "This board")] if st else []
        if role == "BASE":
            lines.append(now(link.get("remote_bmp"), Sensor, "Remote"))
        self.sensor_now.setText("\n".join(lines))

        old_remote = role == "BASE" and link.get("remote_bmp") == "-"
        lines = [f"Now: {cur.summary()}" if cur else "Now: --"]
        prev = Link.from_code(st.get("prev_link"))
        if prev and cur and prev != cur:
            lines.append(f"Before that: {prev.summary()}")
        self.link_now.setText("\n".join(lines))
        alerts = []
        if st.get("searching") == "1":
            alerts.append("⚠ This board hears nothing and is trying the link settings it knows "
                          "(now, before that, defaults).")
        elif role == "BASE" and link.get("up") == "1" and link.get("remote_searching") == "1":
            alerts.append("⚠ The remote's readings arrive, but it can't hear this board's replies, so it is trying "
                          "the link settings it knows. If that goes on, this board searches too.")
        if old_remote:
            alerts.append("⚠ The remote has the older firmware: upload the new one before moving the link.")
        self.link_alert.setText("\n".join(alerts))
        self.link_alert.setVisible(bool(alerts))
        self.link_apply.setEnabled(role == "BASE" and not old_remote)

        if role == "BASE":
            self.retry_now.setText(now(link.get("remote_retry"), Retry, "Remote", cur))
        elif role == "REMOTE":
            self.retry_now.setText(now(st.get("retry"), Retry, "This board", cur))
        else:
            self.retry_now.setText("")

    def update_pending(self, commands):
        names = {"L": "This board", "R": "Remote"}
        why = {"UNKNOWN": "doesn't know this command: upload the new firmware to it",
               "BAD_ARG": "rejected the value", "TIMEOUT": "didn't confirm in time",
               "REPLACED": "got a newer command before this one ran"}
        failed = {"sensor": "set the sensor but it didn't keep the settings: see the log",
                  "link": "couldn't reach the base on the new settings, so both are back on the earlier ones"}
        for key, (sent, targets, out) in list(self.pending.items()):
            results = {w: commands.get((w, n)) for w, n in targets}
            done = {w: r[0] for w, r in results.items() if r and r[1] >= sent and r[0] != "QUEUED"}
            if len(done) < len(targets):
                remote = any(w == "R" for w, _ in targets)
                limit = max(45, self.remote_timeout() * (1 + len(self.remote_queue)) + 10) if remote else 45
                if time.time() - sent > limit:
                    self.finish(key, "warn", "No confirmation from the boards. Check the log.")
                continue
            if all(v == "OK" for v in done.values()):
                ok = ("✓ The remote confirmed and switched; the base follows. If they can't hear each other "
                      "there, “Now” goes back to the earlier settings." if key == "link"
                      else "✓ Applied on both boards." if len(done) == 2 else "✓ Applied.")
                self.finish(key, "good", ok)
            else:
                msg = "  ".join(f"⚠ {names[w]} {failed.get(key, 'reported a failure: see the log') if r == 'FAILED' else why.get(r, r.lower())}."
                                for w, r in done.items() if r != "OK")
                self.finish(key, "crit", msg)

    def finish(self, key, tone, text):
        out = self.pending.pop(key)[2]
        out.setText(text)
        set_tone(out, tone)


def system_theme():
    try:
        return "dark" if QGuiApplication.styleHints().colorScheme() == Qt.ColorScheme.Dark else "light"
    except AttributeError:
        return "dark"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", help="serial port, e.g. /dev/ttyACM0 or COM5 (default: auto-detect)")
    ap.add_argument("--logdir", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs"))
    ap.add_argument("--theme", choices=["auto", "dark", "light"], default="auto")
    args = ap.parse_args()

    app = QApplication(sys.argv[:1])
    app.setApplicationName("Weather link")
    app.setStyle("Fusion")
    if "Noto Sans" in QFontDatabase.families():
        f = QFont("Noto Sans")
        f.setPixelSize(13)
        app.setFont(f)

    os.makedirs(args.logdir, exist_ok=True)
    start = time.strftime("%Y%m%d_%H%M%S")
    state = State(os.path.join(args.logdir, f"weather_{start}.csv"),
                  os.path.join(args.logdir, f"serial_{start}.log"),
                  os.path.join(args.logdir, f"health_{start}.csv"))
    link = SerialLink(args.port, state)
    link.start()
    win = Window(state, link, system_theme() if args.theme == "auto" else args.theme)
    if args.theme == "auto":
        try:
            app.styleHints().colorSchemeChanged.connect(lambda _: win.set_theme(system_theme()))
        except AttributeError:
            pass
    win.show()
    code = app.exec()
    link.stop.set()
    print(f"readings saved to {state.csv_path}")
    sys.exit(code)


if __name__ == "__main__":
    main()
