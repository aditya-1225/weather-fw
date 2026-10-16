#!/usr/bin/env python3
"""Drives the Sensor and Radio tabs of host/weather_gui.py against a fake base board.

Runs the window offscreen on a pseudo-terminal. Checks that the boxes fill from
the boards' STAT / LINK lines, that Apply sends the right codes to the right
boards, that remote commands go one at a time (the base holds only one), that a
link change asks first and goes only through the remote, and that failures and
an older remote firmware are explained. Exit code 0 = pass.

    python3 debug/tests/test_gui_settings_fake_board.py [--shots DIR]   # DIR: save screenshots
"""

import os
import pty
import select
import sys
import tempfile
import time
import tty

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.normpath(os.path.join(HERE, "..", "..", "host")))

from PySide6.QtWidgets import QApplication, QMessageBox   # noqa: E402

import weather_gui                                          # noqa: E402
from weather_ui import SerialLink, State                    # noqa: E402

STAT = "STAT,BASE,1,1,5000,1,3,1000,UPLOAD ,10000,{bmp},{retry},{link},{prev},{search}"
LINK = "LINK,{up},120,2,1,3,4,1,3,1000,POWER ,0,{rbmp},{rretry}"


def main():
    shots = sys.argv[sys.argv.index("--shots") + 1] if "--shots" in sys.argv else None
    app = QApplication([])
    app.setStyle("Fusion")
    bm, bs = pty.openpty()
    tty.setraw(bm)
    tty.setraw(bs)
    logs = tempfile.mkdtemp()
    state = State(os.path.join(logs, "w.csv"), os.path.join(logs, "s.log"), os.path.join(logs, "h.csv"))
    link = SerialLink(os.ttyname(bs), state)
    link.start()
    win = weather_gui.Window(state, link, "dark")
    win.show()
    got = b""

    def pump(t):
        nonlocal got
        end = time.time() + t
        while time.time() < end:
            app.processEvents()
            r, _, _ = select.select([bm], [], [], 0.02)
            if bm in r:
                got += os.read(bm, 4096)

    def board(*lines):
        os.write(bm, b"".join(line.encode() + b"\r\n" for line in lines))
        pump(0.6)

    def sent():
        nonlocal got
        out, got = got.decode(), b""
        return [x for x in out.split("\n") if x and x != "status"]

    checks = []
    pump(1.5)
    board(STAT.format(bmp=15210, retry=10154, link=10822, prev=10822, search=0),
          LINK.format(up=1, rbmp=35301, rretry=8103),
          "DATA,L,10,10000,22.41,100830.0,1", "DATA,R,9,9000,21.87,100812.5,1")
    s = win.sensor_from_controls()
    checks += [
        ("sensor boxes fill from this board's 15210", s.code == 15210 and win.filter_box.currentText() == "×4"),
        ("link boxes fill from 10822", win.link_from_controls().code == 10822 and win.rate_box.currentText() == "250 kbps"),
        ("retry boxes fill from the remote's 8103", win.retry_from_controls().code == 8103),
        ("shows both boards' sensor settings",
         "This board now: temp ×1, pressure ×16, filter ×4" in win.sensor_now.text()
         and "Remote now: temp ×4, pressure ×16, filter ×8, one per reading" in win.sensor_now.text()),
        ("shows the remote's retries, raised to 1500 µs at 250 kbps",
         win.retry_now.text() == "Remote now: 2250 µs apart, 10 retries, 3 sends per reading"),
        ("link strip counts the remote's real retry count", any(m.text() == "3 of 10 retries" for m in win.link_metrics)),
        ("explains the sensor settings (x1/x16: 35.5 + 62.5 ms)", "every 98 ms" in win.sensor_explain.text()),
    ]

    win.interval_spin.setValue(200)
    win.filter_box.setCurrentIndex(0)
    checks += [("explanation follows the boxes",
                "holds about 2.0 new measurements" in win.sensor_explain.text()
                and "No smoothing" in win.sensor_explain.text())]
    win.mode_box.setCurrentIndex(1)
    checks += [("rest box is off in one-per-reading mode", not win.standby_box.isEnabled())]
    win.mode_box.setCurrentIndex(0)
    win.standby_box.setCurrentIndex(5)
    checks += [("warns when the chip is slower than the readings",
                "⚠" in win.sensor_explain.text() and win.sensor_explain.property("tone") == "warn")]

    # Sensor to both boards
    win.set_sensor_controls(weather_gui.Sensor(1, 5, 0, 1, 0))
    sent()
    win.apply_sensor()
    pump(0.4)
    checks.append(("Apply sends bmp 15010 to this board first", sent() == ["bmp 15010"]))
    board("CMD,L,0,bmp,OK")
    checks.append(("and the remote's once this board has answered", sent() == ["remote bmp 15010"]))
    board("CMD,R,6,bmp,QUEUED")
    checks.append(("waits for the remote", "Waiting" in win.sensor_result.text()))
    board("CMD,R,6,bmp,OK")
    checks.append(("confirms both", win.sensor_result.text() == "✓ Applied on both boards."))

    # Only this board
    win.sensor_target.setCurrentIndex(1)
    win.apply_sensor()
    pump(0.4)
    checks.append(("'This board' sends only the local command", sent() == ["bmp 15010"]))
    board("CMD,L,0,bmp,FAILED")
    checks.append(("explains a sensor that didn't keep the settings",
                   "didn't keep the settings" in win.sensor_result.text()
                   and win.sensor_result.property("tone") == "crit"))

    # A base that never answers (older firmware prints its help instead) doesn't hold up the queue for good
    win.sensor_target.setCurrentIndex(0)
    win.apply_sensor()
    pump(1.0)
    first = sent()
    pump(1.6)
    checks += [("no answer: the remote's command waits", first == ["bmp 15010"]),
               ("and goes after the answer wait", sent() == ["remote bmp 15010"])]
    board("CMD,R,7,bmp,QUEUED", "CMD,R,7,bmp,OK")
    win.pending.pop("sensor", None)

    # Remote commands one at a time
    win.set_retry_controls(weather_gui.Retry(12, 15, 5))
    win.apply_retry()
    win.remote_pa.setCurrentIndex(2)
    win.apply_pa("R", 2)
    pump(0.4)
    first = sent()
    board("CMD,R,7,retry,QUEUED")
    still = sent()
    board("CMD,R,7,retry,OK")
    pump(0.3)
    then = sent()
    checks += [("retries go only to the remote, first in line", first == ["remote retry 12155"]),
               ("power waits while the base holds the retry command", still == []),
               ("power goes once the retry command is done", then == ["remote pa 2"]),
               ("retry result reported", win.retry_result.text() == "✓ Applied.")]
    board("CMD,R,8,pa,OK")

    # Link change: asks first, goes only through the remote
    win.set_link_controls(weather_gui.Link(90, 0, 2))
    asked = []
    QMessageBox.question = staticmethod(lambda *a, **k: asked.append(a[2]) or QMessageBox.No)
    win.apply_link()
    pump(0.3)
    checks.append(("link change asks first; No sends nothing", asked and "channel 90" in asked[0] and sent() == []))
    QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.Yes)
    win.apply_link()
    pump(0.4)
    checks.append(("Yes sends remote link 9002 only", sent() == ["remote link 9002"]))
    board("CMD,R,9,link,OK", STAT.format(bmp=15010, retry=10154, link=9002, prev=10822, search=0))
    checks += [("link confirmation explained", "confirmed and switched" in win.link_result.text()),
               ("shows the new and the earlier link",
                "Now: channel 90 (2490 MHz), 1 Mbps, 16-bit CRC" in win.link_now.text()
                and "Before that: channel 108" in win.link_now.text())]

    win.set_link_controls(weather_gui.Link(80, 1, 1))
    win.apply_link()
    pump(0.4)
    sent()
    board("CMD,R,10,link,FAILED")
    checks.append(("explains a link change that didn't work",
                   "couldn't reach the base on the new settings" in win.link_result.text()))

    board(STAT.format(bmp=15010, retry=10154, link=9002, prev=10822, search=0), LINK.format(up=1, rbmp=35301, rretry="8103,1"))
    checks += [("a remote that can't hear the base shows as one way", win.link_pill.text() == "⚠ One way"),
               ("and is explained", "can't hear this board's replies" in win.link_alert.text())]
    board(STAT.format(bmp=15010, retry=10154, link=9002, prev=10822, search=1), LINK.format(up=0, rbmp=35301, rretry="8103,0"))
    checks += [("searching shows in the link strip", win.link_pill.text() == "⚠ Searching"),
               ("and under the link settings", "trying the link settings it knows" in win.link_alert.text())]

    if shots:
        os.makedirs(shots, exist_ok=True)
        win.show_view(1)
        for i, name in enumerate(("sensor", "radio")):
            win.tabs.setCurrentIndex(i)
            pump(0.3)
            win.tabs.parentWidget().grab().save(os.path.join(shots, f"tab_{name}.png"))

    board(LINK.format(up=1, rbmp="-", rretry="-"))
    checks += [("older remote firmware: link button off", not win.link_apply.isEnabled()),
               ("and explained", "older firmware" in win.link_alert.text()
                and "Remote: older firmware" in win.sensor_now.text())]

    link.stop.set()
    ok = True
    for name, passed in checks:
        print(("PASS " if passed else "FAIL ") + name)
        ok &= passed
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
