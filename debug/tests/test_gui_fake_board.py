#!/usr/bin/env python3
"""Drives host/weather_gui.py against a fake board on a pseudo-terminal.

Runs the window offscreen. Checks that it asks for status on connect, that the
health-interval control sends `health <ms>` to both boards and reports their
answers, that the remote's interval is worked out from its HEALTH reports, and
that an older firmware (no health field in STAT) is called out. Writes logs to
a temporary folder. Exit code 0 = pass.

    python3 debug/tests/test_gui_fake_board.py [--shots DIR]   # DIR: save screenshots
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

    checks = []
    pump(1.5)
    checks.append(("asks for status on connect", b"status\n" in got))

    board("STAT,BASE,1,1,5000,3,1,1000,UPLOAD ,10000",
          "LINK,1,120,2,1,0,4,1,1,1000,POWER ,0",
          "DATA,L,10,10000,22.41,100830.0,1", "DATA,R,9,9000,21.87,100812.5,1",
          "HEALTH,L,3,30000,4612,3291,34.5,0,0,0,10,0,100,12,5300",
          "HEALTH,R,7,70000,4598,3288,36.0,2,1,0,10,14,0,85,5350")
    checks += [("health spin starts at the board's 10 s", win.health_spin.value() == 10),
               ("big reading shows this board's temperature", win.local_card.temp.text() == "22.41"),
               ("remote card shows the remote's temperature", win.remote_card.temp.text() == "21.87"),
               ("remote power-loss tile is flagged", win.remote_card.tiles["losses"].value.text() == "⚠ 2"),
               ("remote buttons enabled on the base", all(b.isEnabled() for b in win.remote_buttons))]

    win.health_spin.setValue(30)
    got = b""
    win.health_apply.click()
    pump(0.5)
    first, got = got, b""
    board("CMD,L,0,health,OK")
    checks.append(("Apply sends health 30000 to both boards, the remote's after this board answered",
                   first == b"health 30000\n" and got == b"remote health 30000\n"))
    board("CMD,R,5,health,QUEUED")
    checks.append(("waits while the remote's command is queued", "Waiting" in win.timing_result.text()))
    board("CMD,R,5,health,OK", "STAT,BASE,1,1,6000,3,1,1000,UPLOAD ,30000",
          "HEALTH,R,8,100000,4598,3288,36.0,2,1,0,30,42,0,85,5350")
    checks += [("confirms both boards", win.timing_result.text() == "✓ Applied on both boards."),
               ("shows this board's new interval", "This board: 30 s" in win.health_now.text()),
               ("works out the remote's interval from its reports", "Remote: about 30 s" in win.health_now.text()),
               ("sends tile uses the real interval", win.remote_card.tiles["sends"].value.text() == "42 per 30 s")]
    if shots:
        os.makedirs(shots, exist_ok=True)
        now = time.time()      # fill the charts so the screenshot shows them
        import math
        for card, base in ((win.local_card, 22.4), (win.remote_card, 21.9)):
            card.times[:] = [now - 900 + i for i in range(900)]
            card.temps[:] = [base + 0.35 * math.sin(i / 140) + 0.03 * math.sin(i / 7) for i in range(900)]
            card.press[:] = [1008.3 + 0.2 * math.cos(i / 200) for i in range(900)]
            card.refresh_chart()
        win.local_card.chart.hover_x = win.local_card.chart.width() * 0.62
        pump(0.4)
        win.grab().save(os.path.join(shots, "gui_dark.png"))
        win.show_view(1)
        pump(0.4)
        win.grab().save(os.path.join(shots, "gui_dark_control.png"))
        win.show_view(0)
        pump(0.2)
        win.set_theme("light")
        pump(0.4)
        win.grab().save(os.path.join(shots, "gui_light.png"))
        win.set_theme("dark")

    win.apply_timing("health")
    pump(0.3)
    board("CMD,L,0,health,OK")
    board("CMD,R,6,health,UNKNOWN")
    checks.append(("explains an older remote firmware", "upload the new firmware" in win.timing_result.text()))

    board("STAT,BASE,1,1,7000,3,1,1000,UPLOAD ")    # firmware from before the health command
    checks.append(("flags firmware without the health field", "firmware too old" in win.health_now.text()))

    board("STAT,REMOTE,1,1,7000,3,1,1000,POWER ,10000")
    checks.append(("remote buttons disabled when the remote is plugged in",
                   not any(b.isEnabled() for b in win.remote_buttons)))
    QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.No)
    got = b""
    win.reset("L")
    pump(0.3)
    checks.append(("restart asks first and sends nothing on No", b"reset" not in got))

    link.stop.set()
    ok = True
    for name, passed in checks:
        print(("PASS " if passed else "FAIL ") + name)
        ok &= passed
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
