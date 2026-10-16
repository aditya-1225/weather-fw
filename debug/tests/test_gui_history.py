#!/usr/bin/env python3
"""Checks the desktop window's 24-hour history.

Writes fake weather_*.csv logs to a temporary folder and checks that the window
reads the last 24 h of them into the right board's chart. Then it draws a 24 h
chart of one reading per second and checks that a one-second spike and a
one-hour gap still show, and how long a redraw takes. Exit code 0 = pass.

    python3 debug/tests/test_gui_history.py [--shots DIR]   # DIR: save the 24 h chart
"""

import datetime as dt
import math
import os
import pty
import sys
import tempfile
import time
import tty

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.normpath(os.path.join(HERE, "..", "..", "host")))

from PySide6.QtGui import QColor, QImage                    # noqa: E402
from PySide6.QtWidgets import QApplication                  # noqa: E402

import weather_gui                                          # noqa: E402
from weather_ui import SerialLink, State                    # noqa: E402

HEADER = "pc_time,node,seq,node_uptime_ms,temp_c,pressure_pa,altitude_m,sensor_ok\n"


def stamp(ts):
    return dt.datetime.fromtimestamp(ts).isoformat(timespec="milliseconds")


def write_log(path, rows, mtime=None):
    with open(path, "w") as f:
        f.write(HEADER)
        for ts, node, temp, ok in rows:
            f.write(f"{stamp(ts)},{node},1,1000,{temp:.2f},{91000 + temp:.1f},880.0,{ok}\n")
    if mtime:
        os.utime(path, (mtime, mtime))


def series_pixels(img, color, top, bottom, left, right):
    """Pixel columns where the series colour shows, between rows top..bottom."""
    c = QColor(color)
    hits = set()
    for x in range(left, right):
        for y in range(top, bottom):
            p = img.pixelColor(x, y)
            if abs(p.red() - c.red()) + abs(p.green() - c.green()) + abs(p.blue() - c.blue()) < 60:
                hits.add(x)
                break
    return hits


def main():
    shots = sys.argv[sys.argv.index("--shots") + 1] if "--shots" in sys.argv else None
    app = QApplication([])
    app.setStyle("Fusion")
    checks = []
    now = time.time()

    # ---- reading earlier logs
    logs = tempfile.mkdtemp()
    write_log(os.path.join(logs, "weather_20200101_000000.csv"),            # last written 3 days ago: skipped
              [(now - 3 * 86400, "base", 1.0, 1)], mtime=now - 3 * 86400)
    rows = []
    for i in range(0, 30 * 3600, 60):                                       # 30 h, one per minute per board
        ts = now - 30 * 3600 + i
        rows += [(ts, "base", 20 + i / 1e5, 1), (ts + 0.5, "remote", 15 + i / 1e5, 1)]
    rows.append((now - 100, "remote", 99.0, 0))                             # sensor not OK
    write_log(os.path.join(logs, "weather_20200102_000000.csv"), rows)
    with open(os.path.join(logs, "weather_20200102_000000.csv"), "a") as f:
        f.write(f"{stamp(now - 50)},base,1,10")                             # cut short by a crash
    current = os.path.join(logs, "weather_20990101_000000.csv")
    write_log(current, [(now - 10, "base", 50.0, 1)])                       # this session's own file: skipped

    past = weather_gui.load_history(logs, current, now - weather_gui.HISTORY_S)
    bt, btemp, bp = past["base"]
    rt, rtemp, rp = past["remote"]
    checks += [("reads only the last 24 h", now - 86400 <= bt[0] < now - 86400 + 61 and len(bt) in (1439, 1440)),
               ("skips logs last written over 24 h ago", min(btemp) > 19),
               ("skips this session's own log", 50.0 not in btemp),
               ("a failed sensor reading is a gap, not a value",
                [(v, q) for ts, v, q in zip(rt, rtemp, rp) if abs(ts - (now - 100)) < 0.01] == [(None, None)]),
               ("readings sorted by time", bt == sorted(bt) and rt == sorted(rt))]

    # ---- the window puts them in the right cards once it knows the role
    os.rename(current, current + ".x")
    state = State(current, os.path.join(logs, "s.log"), os.path.join(logs, "h.csv"))
    bm, bs = pty.openpty()
    tty.setraw(bm)
    tty.setraw(bs)
    link = SerialLink(os.ttyname(bs), state)
    link.start()
    win = weather_gui.Window(state, link, "dark")
    win.show()

    def pump(t):
        end = time.time() + t
        while time.time() < end:
            app.processEvents()
            time.sleep(0.02)

    pump(1.0)
    checks.append(("waits for the role before filling the charts", not win.local_card.times))
    os.write(bm, b"STAT,BASE,1,1,5000,3,1,1000,UPLOAD ,10000\r\nDATA,L,10,10000,22.41,100830.0,1\r\n"
                 b"DATA,R,9,9000,21.87,100812.5,1\r\n")
    pump(0.8)
    lc, rc = win.local_card, win.remote_card
    checks += [("base log goes to this board's chart", len(lc.times) == len(bt) + 1 and lc.temps[0] < 21
                and lc.temps[-1] == 22.41),
               ("remote log goes to the remote's chart", len(rc.times) == len(rt) + 1 and rc.temps[0] < 16
                and rc.temps[-1] == 21.87),
               ("24 hours is a chart option", "Last 24 hours" in [lc.span.itemText(i) for i in range(lc.span.count())])]
    link.stop.set()

    # ---- drawing 24 h of one reading per second
    chart = weather_gui.HistoryChart(weather_gui.THEMES["dark"], "base")
    chart.resize(640, 220)
    chart.span_s = 86400
    times = [now - 86400 + i for i in range(86400)]
    temps = [21 + 0.3 * math.sin(i / 3000) for i in range(86400)]
    spike_at = 50000
    temps[spike_at] = 23.0                                       # one reading, 2 °C above the rest
    for i in range(20000, 23600):                                # one hour with no readings
        temps[i] = None
    chart.set_series(times, temps, "°C", 2, 0.5)
    img = QImage(chart.size(), QImage.Format_ARGB32)
    t = time.perf_counter()
    chart.render(img)
    ms = (time.perf_counter() - t) * 1000
    left, right = 58, 640 - 12
    plot_w = right - left
    col = lambda i: left + int((times[i] - (now - 86400)) / 86400 * plot_w)
    top_band = series_pixels(img, weather_gui.THEMES["dark"]["base"], 10, 40, left, right)
    drawn = series_pixels(img, weather_gui.THEMES["dark"]["base"], 10, 220 - 22, left, right)
    gap_cols = set(range(col(20000) + 2, col(23600) - 1))
    checks += [("a one-second spike still shows at 24 h", any(abs(x - col(spike_at)) <= 2 for x in top_band)),
               ("nothing else reaches the spike's height", all(abs(x - col(spike_at)) <= 6 for x in top_band)),
               ("a one-hour gap stays empty", not (drawn & gap_cols)),
               ("the line is drawn on both sides of the gap", col(10000) in drawn and col(40000) in drawn),
               (f"redraw takes {ms:.0f} ms (< 150)", ms < 150)]
    chart.hover_x = col(spike_at)
    i = chart.nearest(times[spike_at] + 0.2, 0)
    checks += [("hover finds the nearest reading", i == spike_at),
               ("hover skips missing readings", temps[chart.nearest(times[21000], 0)] is not None)]
    if shots:
        os.makedirs(shots, exist_ok=True)
        chart.render(img)
        img.save(os.path.join(shots, "chart_24h.png"))

    ok = True
    for name, passed in checks:
        print(("PASS " if passed else "FAIL ") + name)
        ok &= passed
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
