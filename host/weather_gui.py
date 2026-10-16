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
import time
from bisect import bisect_left

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QFontDatabase, QGuiApplication, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (QApplication, QButtonGroup, QComboBox, QFrame, QGridLayout, QHBoxLayout,
                               QLabel, QLineEdit, QMainWindow, QMessageBox, QPlainTextEdit, QPushButton,
                               QScrollArea, QSizePolicy, QSpinBox, QTabWidget, QVBoxLayout, QWidget)

from weather_settings import (DEFAULT_LINK, DEFAULT_RETRY, DEFAULT_SENSOR, FILTER_COEF, OVERSAMPLING, PA_DBM,
                              STANDBY_MS, Link, Retry, Sensor)
from weather_ui import PA_NAMES, State, SerialLink, num, uptime

HISTORY_S = 24 * 3600           # readings kept for the charts
ANSWER_WAIT_S = 2.0             # longest wait for the base to answer a command line before sending the next
LINE_GAP_S = 0.2                # and the pause after its answer
SPANS = [("5 min", 300), ("15 min", 900), ("1 hour", 3600), ("2 hours", 7200), ("6 hours", 21600),
         ("24 hours", 86400)]

THEMES = {
    "dark": dict(bg="#131c28", surface="#1a2533", raised="#223044", line="#2c3b50", hover="#2a3a52",
                 text="#e6edf5", text2="#a3b1c2", muted="#74859a",
                 base="#3987e5", remote="#199e70", accent="#3987e5", on_accent="#ffffff",
                 good="#0ca30c", warn="#fab219", crit="#e05252"),
    "light": dict(bg="#e8edf3", surface="#f8fafc", raised="#ffffff", line="#d2dbe5", hover="#eef2f7",
                  text="#142030", text2="#4a5b6e", muted="#6e7f93",
                  base="#2a78d6", remote="#1baf7a", accent="#2a78d6", on_accent="#ffffff",
                  good="#0a870a", warn="#b77b00", crit="#d03b3b"),
}


def qss(t):
    return f"""
    QWidget {{ color: {t['text']}; font-size: 13px; }}
    QMainWindow, QScrollArea, #page {{ background: {t['bg']}; }}
    QFrame#card {{ background: {t['surface']}; border: 1px solid {t['line']}; border-radius: 14px; }}
    QFrame#strip {{ background: {t['surface']}; border: 1px solid {t['line']}; border-radius: 10px; }}
    QFrame#tile {{ background: {t['raised']}; border-radius: 8px; }}
    QFrame#rule {{ background: {t['line']}; max-height: 1px; min-height: 1px; }}
    QLabel {{ background: transparent; }}
    QLabel[role="app"] {{ font-size: 22px; font-weight: 600; }}
    QLabel[role="hero"] {{ font-size: 68px; font-weight: 300; }}
    QLabel[role="herounit"] {{ font-size: 24px; color: {t['text2']}; }}
    QLabel[role="pressure"] {{ font-size: 21px; }}
    QLabel[role="title"] {{ font-size: 17px; font-weight: 600; }}
    QLabel[role="section"] {{ font-size: 14px; font-weight: 600; }}
    QLabel[role="sub"] {{ color: {t['text2']}; }}
    QLabel[role="muted"] {{ color: {t['muted']}; font-size: 12px; }}
    QLabel[role="tilelabel"] {{ color: {t['text2']}; font-size: 12px; }}
    QLabel[role="tilevalue"] {{ font-size: 16px; font-weight: 600; }}
    QLabel[role="metric"] {{ color: {t['text2']}; }}
    QLabel[tone="good"] {{ color: {t['good']}; }}
    QLabel[tone="warn"] {{ color: {t['warn']}; }}
    QLabel[tone="crit"] {{ color: {t['crit']}; }}
    QLabel[tone="dim"] {{ color: {t['muted']}; }}
    QLabel[role="hero"][tone="dim"] {{ color: {t['muted']}; }}
    QLabel#pill {{ border-radius: 11px; padding: 2px 10px; font-weight: 600; font-size: 12px;
                   border: 1px solid {t['line']}; }}
    QLabel#pill[tone="good"] {{ border-color: {t['good']}; }}
    QLabel#pill[tone="warn"] {{ border-color: {t['warn']}; }}
    QLabel#pill[tone="crit"] {{ border-color: {t['crit']}; }}
    QPushButton {{ background: {t['raised']}; border: 1px solid {t['line']}; border-radius: 8px;
                   padding: 6px 12px; }}
    QPushButton:hover {{ background: {t['hover']}; }}
    QPushButton:pressed {{ background: {t['line']}; }}
    QPushButton:focus {{ border: 1px solid {t['accent']}; }}
    QPushButton:disabled {{ color: {t['muted']}; background: transparent; }}
    QPushButton[kind="primary"] {{ background: {t['accent']}; color: {t['on_accent']}; border-color: {t['accent']};
                                   font-weight: 600; }}
    QPushButton[kind="primary"]:hover {{ background: {t['accent']}; border-color: {t['text']}; }}
    QPushButton[kind="danger"] {{ color: {t['crit']}; }}
    QPushButton[kind="danger"]:hover {{ border-color: {t['crit']}; }}
    QPushButton[kind="seg"] {{ padding: 3px 10px; border-radius: 6px; font-size: 12px; background: transparent; }}
    QPushButton[kind="seg"]:checked {{ background: {t['raised']}; border-color: {t['line']}; font-weight: 600; }}
    QSpinBox, QComboBox, QLineEdit {{ background: {t['raised']}; border: 1px solid {t['line']};
                   border-radius: 8px; padding: 5px 8px; selection-background-color: {t['accent']}; }}
    QSpinBox:focus, QComboBox:focus, QLineEdit:focus {{ border-color: {t['accent']}; }}
    QComboBox QAbstractItemView {{ background: {t['raised']}; border: 1px solid {t['line']};
                   selection-background-color: {t['accent']}; }}
    QPlainTextEdit {{ background: {t['raised']}; border: 1px solid {t['line']}; border-radius: 10px;
                      padding: 6px; font-family: "Noto Sans Mono", monospace; font-size: 12px; }}
    QScrollBar:vertical {{ background: transparent; width: 10px; }}
    QScrollBar::handle:vertical {{ background: {t['line']}; border-radius: 5px; min-height: 30px; }}
    QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; }}
    QToolTip {{ background: {t['raised']}; color: {t['text']}; border: 1px solid {t['line']}; }}
    QTabWidget::pane {{ border: none; }}
    QTabBar::tab {{ background: transparent; color: {t['text2']}; border: 1px solid transparent; border-radius: 7px;
                    padding: 5px 14px; margin-right: 4px; }}
    QTabBar::tab:hover {{ color: {t['text']}; }}
    QTabBar::tab:selected {{ background: {t['raised']}; color: {t['text']}; border-color: {t['line']};
                             font-weight: 600; }}
    QComboBox:disabled, QSpinBox:disabled {{ color: {t['muted']}; }}
    """


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
        self.sensor = label("", tone="dim")
        self.sensor.setObjectName("pill")
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
        tools.setSpacing(4)
        self.seg = QButtonGroup(self)
        for key, text in (("temp", "Temperature"), ("press", "Pressure")):
            b = QPushButton(text)
            b.setProperty("kind", "seg")
            b.setCheckable(True)
            b.setChecked(key == "temp")
            b.clicked.connect(lambda _=False, k=key: self.set_metric(k))
            self.seg.addButton(b)
            tools.addWidget(b)
        tools.addStretch()
        self.span = QComboBox()
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
            return
        self.add_reading(r)
        ok = r["sensor_ok"]
        self.sensor.setText("Sensor OK" if ok else "⚠ Sensor not responding")
        set_tone(self.sensor, "good" if ok else "crit")
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
        head.addWidget(label("Weather link", "app"))
        head.addSpacing(12)
        self.conn = label("Disconnected", tone="crit")
        self.conn.setObjectName("pill")
        head.addWidget(self.conn)
        self.port = label("", "sub")
        head.addWidget(self.port)
        head.addStretch()
        self.board_info = label("", "sub")
        head.addWidget(self.board_info)
        root.addLayout(head)
        self.logpath = label(f"Logging to {state.csv_path}", "muted")
        self.logpath.setTextInteractionFlags(Qt.TextSelectableByMouse)
        root.addWidget(self.logpath)

        cards = QHBoxLayout()
        cards.setSpacing(14)
        self.local_card = BoardCard(self.theme, "base", "This board")
        self.remote_card = BoardCard(self.theme, "remote", "Remote board")
        cards.addWidget(self.local_card, 1)
        cards.addWidget(self.remote_card, 1)
        root.addLayout(cards, 1)

        strip = QFrame()
        strip.setObjectName("strip")
        sl = QHBoxLayout(strip)
        sl.setContentsMargins(16, 10, 16, 10)
        sl.setSpacing(18)
        sl.addWidget(label("Radio link", "section"))
        self.link_pill = label("--", tone="dim")
        self.link_pill.setObjectName("pill")
        sl.addWidget(self.link_pill)
        self.link_metrics = []
        for _ in range(5):
            m = label("", "metric")
            m.setFont(tabular(m.font()))
            self.link_metrics.append(m)
            sl.addWidget(m)
        sl.addStretch()
        root.addWidget(strip)

        bottom = QHBoxLayout()
        bottom.setSpacing(14)
        bottom.addWidget(self.build_controls(), 0)
        bottom.addWidget(self.build_log(), 1)
        root.addLayout(bottom)

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
        lay.setContentsMargins(18, 14, 18, 16)
        lay.setSpacing(10)
        return c, lay

    def button(self, text, handler, kind=None, tip=None):
        b = QPushButton(text)
        if kind:
            b.setProperty("kind", kind)
        if tip:
            b.setToolTip(tip)
        b.clicked.connect(handler)
        return b

    def tab(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(2, 12, 2, 2)
        lay.setSpacing(8)
        return w, lay

    @staticmethod
    def combo(items, tip=None):
        box = QComboBox()
        box.addItems(items)
        if tip:
            box.setToolTip(tip)
        return box

    @staticmethod
    def spin(lo, hi, tip=None):
        s = QSpinBox()
        s.setRange(lo, hi)
        s.setButtonSymbols(QSpinBox.NoButtons)
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

    @staticmethod
    def form(rows):
        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(6)
        for i, (text, widget, tip) in enumerate(rows):
            name = label(text)
            name.setToolTip(tip)
            widget.setToolTip(tip)
            grid.addWidget(name, i, 0)
            grid.addWidget(widget, i, 1)
        grid.setColumnStretch(0, 1)
        return grid

    def build_controls(self):
        c, lay = self.card()
        c.setFixedWidth(520)
        self.remote_buttons = []           # enabled only when the base is plugged in
        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self.tabs.addTab(self.build_sensor_tab(), "Sensor")
        self.tabs.addTab(self.build_radio_tab(), "Radio")
        self.tabs.addTab(self.build_timing_tab(), "Timing")
        self.tabs.addTab(self.build_boards_tab(), "Boards")
        lay.addWidget(self.tabs)
        lay.addStretch()
        self.controls_built = True
        self.explain_sensor()
        self.explain_radio()
        return c

    def build_sensor_tab(self):
        w, lay = self.tab()
        self.osrs_t = self.combo([f"×{n}" for n in OVERSAMPLING[1:]])
        self.osrs_p = self.combo([f"×{n}" for n in OVERSAMPLING[1:]])
        self.filter_box = self.combo(["Off"] + [f"×{n}" for n in FILTER_COEF[1:]])
        self.mode_box = self.combo(["Continuous", "One per reading"])
        self.standby_box = self.combo([f"{ms:g} ms" for ms in STANDBY_MS])
        lay.addLayout(self.form([
            ("Temperature oversampling", self.osrs_t,
             "Measurements averaged into each temperature value. The pressure is corrected with it."),
            ("Pressure oversampling", self.osrs_p, "Measurements averaged into each pressure value: finer, slower."),
            ("Smoothing filter", self.filter_box,
             "Blends each new measurement into the previous output: higher is smoother and slower to follow."),
            ("Measuring", self.mode_box,
             "Continuous: the chip measures on its own clock. One per reading: it sleeps until the board asks."),
            ("Rest between measurements", self.standby_box, "Continuous only: the pause after each measurement."),
        ]))
        for box in (self.osrs_t, self.osrs_p, self.filter_box, self.mode_box, self.standby_box):
            box.currentIndexChanged.connect(self.explain_sensor)
        self.sensor_explain = self.note(role="sub")
        lay.addWidget(self.sensor_explain)
        row = QHBoxLayout()
        row.addWidget(label("Apply to"))
        self.sensor_target = self.combo(["Both boards", "This board", "Remote board"])
        row.addWidget(self.sensor_target)
        row.addStretch()
        row.addWidget(self.button("Defaults", lambda: self.set_sensor_controls(DEFAULT_SENSOR),
                                  tip="Put the default settings in the boxes (not applied yet)"))
        row.addWidget(self.button("Apply", self.apply_sensor, "primary"))
        lay.addLayout(row)
        self.sensor_now = self.note()
        lay.addWidget(self.sensor_now)
        self.sensor_result = self.note()
        lay.addWidget(self.sensor_result)
        lay.addStretch()
        self.set_sensor_controls(DEFAULT_SENSOR)
        return w

    def build_radio_tab(self):
        w, lay = self.tab()
        lay.addWidget(label("Link", "section"))
        lay.addWidget(self.note("Both boards must use the same channel, rate and CRC, so these change on both: "
                                "the remote confirms and switches, then the base follows. If they can't hear "
                                "each other afterwards, both go back to the earlier settings on their own."))
        self.channel_spin = self.spin(0, 125)
        self.rate_box = self.combo(["250 kbps", "1 Mbps", "2 Mbps"])      # RF24 codes 2, 0, 1
        self.crc_box = self.combo(["16-bit", "8-bit"])                    # RF24 codes 2, 1
        lay.addLayout(self.form([
            ("Channel", self.channel_spin, "Frequency = 2400 MHz + channel"),
            ("Data rate", self.rate_box, "Slower rates reach further"),
            ("Error check (CRC)", self.crc_box, "Checksum on every packet; damaged packets are dropped"),
        ]))
        for wdg in (self.channel_spin, self.rate_box, self.crc_box):
            sig = wdg.valueChanged if isinstance(wdg, QSpinBox) else wdg.currentIndexChanged
            sig.connect(self.explain_radio)
        self.link_explain = self.note(role="sub")
        lay.addWidget(self.link_explain)
        row = QHBoxLayout()
        row.addStretch()
        row.addWidget(self.button("Defaults", lambda: self.set_link_controls(DEFAULT_LINK),
                                  tip="Put the default settings in the boxes (not applied yet)"))
        self.link_apply = self.button("Move both boards…", self.apply_link, "primary")
        row.addWidget(self.link_apply)
        lay.addLayout(row)
        self.link_now = self.note()
        lay.addWidget(self.link_now)
        self.link_alert = self.note()
        set_tone(self.link_alert, "warn")
        lay.addWidget(self.link_alert)
        self.link_result = self.note()
        lay.addWidget(self.link_result)

        lay.addWidget(self.rule())
        lay.addWidget(label("Transmit power", "section"))
        lay.addWidget(self.note("Per board. HIGH and MAX can deafen a receiver that is only a few metres away."))
        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        pa_items = [f"{n} ({PA_DBM[i]} + module)" for i, n in enumerate(PA_NAMES)]
        for i, (where, text) in enumerate((("L", "This board"), ("R", "Remote board"))):
            box = self.combo(pa_items, "The nRF24's own output; the module's amplifier adds its gain on top")
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
        lay.addLayout(grid)
        self.pa_result = self.note()
        lay.addWidget(self.pa_result)

        lay.addWidget(self.rule())
        lay.addWidget(label("Remote's retries", "section"))
        lay.addWidget(self.note("Only the remote transmits (the base just answers), so these are the remote's."))
        self.ard_box = self.combo([f"{250 * (i + 1)} µs" for i in range(16)])
        self.arc_spin = self.spin(0, 15)
        self.attempts_spin = self.spin(1, 9)
        lay.addLayout(self.form([
            ("Wait between tries", self.ard_box, "How long the radio waits for the base's reply before trying again"),
            ("Retries per send", self.arc_spin, "Automatic re-tries by the radio chip itself"),
            ("Sends per reading", self.attempts_spin,
             "The firmware starts the whole send again this many times, 3-14 ms apart"),
        ]))
        for wdg in (self.ard_box, self.arc_spin, self.attempts_spin):
            sig = wdg.valueChanged if isinstance(wdg, QSpinBox) else wdg.currentIndexChanged
            sig.connect(self.explain_radio)
        self.retry_explain = self.note(role="sub")
        lay.addWidget(self.retry_explain)
        row = QHBoxLayout()
        row.addStretch()
        row.addWidget(self.button("Defaults", lambda: self.set_retry_controls(DEFAULT_RETRY),
                                  tip="Put the default settings in the boxes (not applied yet)"))
        retry_apply = self.button("Apply", self.apply_retry, "primary")
        row.addWidget(retry_apply)
        lay.addLayout(row)
        self.retry_now = self.note()
        lay.addWidget(self.retry_now)
        self.retry_result = self.note()
        lay.addWidget(self.retry_result)
        self.remote_buttons += [self.link_apply, retry_apply]
        self.set_link_controls(DEFAULT_LINK)
        self.set_retry_controls(DEFAULT_RETRY)
        return w

    def build_timing_tab(self):
        w, lay = self.tab()
        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(6)
        grid.addWidget(label("Take a reading every"), 0, 0)
        self.interval_spin = self.spin(200, 60000)
        self.interval_spin.setSingleStep(100)
        self.interval_spin.setSuffix(" ms")
        self.interval_spin.setValue(1000)
        self.interval_spin.valueChanged.connect(self.explain_sensor)
        grid.addWidget(self.interval_spin, 0, 1)
        grid.addWidget(self.button("Apply", lambda: self.apply_timing("interval"), "primary"), 0, 2)
        self.interval_now = label("", "muted")
        grid.addWidget(self.interval_now, 1, 0, 1, 3)
        grid.addWidget(label("Send a health report every"), 2, 0)
        self.health_spin = self.spin(2, 60)
        self.health_spin.setSuffix(" s")
        self.health_spin.setValue(10)
        grid.addWidget(self.health_spin, 2, 1)
        self.health_apply = self.button("Apply", lambda: self.apply_timing("health"), "primary")
        grid.addWidget(self.health_apply, 2, 2)
        self.health_now = label("", "muted")
        grid.addWidget(self.health_now, 3, 0, 1, 3)
        grid.setColumnStretch(0, 1)
        lay.addLayout(grid)
        self.timing_result = self.note("Applies to both boards, and each keeps it after a restart.")
        lay.addWidget(self.timing_result)
        lay.addStretch()
        return w

    def build_boards_tab(self):
        w, lay = self.tab()
        for where, title in (("R", "Remote board"), ("L", "This board")):
            lay.addWidget(label(title, "section"))
            row1, row2 = QHBoxLayout(), QHBoxLayout()
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
            for b in btns:
                row1.addWidget(b)
            row1.addStretch()
            row2.addStretch()
            reset = self.button("Restart board…", lambda _=False, wh=where: self.reset(wh), "danger")
            row2.addWidget(reset)
            lay.addLayout(row1)
            lay.addLayout(row2)
            if where == "R":
                self.remote_buttons += btns + [reset]
        lay.addStretch()
        return w

    def build_log(self):
        c, lay = self.card()
        hl = QHBoxLayout()
        hl.addWidget(label("Log", "section"))
        hl.addStretch()
        hl.addWidget(label("Everything the board prints is also saved to serial_*.log", "muted"))
        lay.addLayout(hl)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(2000)
        self.log.setMinimumHeight(240)
        lay.addWidget(self.log, 1)
        row = QHBoxLayout()
        self.cmd = QLineEdit()
        self.cmd.setPlaceholderText("Type a firmware command, e.g. status, help, remote ping")
        self.cmd.returnPressed.connect(self.send_typed)
        row.addWidget(self.cmd, 1)
        row.addWidget(self.button("Send", self.send_typed))
        lay.addLayout(row)
        return c

    def apply_theme(self):
        self.setStyleSheet(qss(self.theme))
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
            self.link_pill.setText("Up" if up else "⚠ Searching" if searching else "⚠ Down")
            set_tone(self.link_pill, "good" if up else "crit")
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
