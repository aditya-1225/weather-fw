#!/usr/bin/env python3
"""Drives host/weather_ui.py against a fake board on a pseudo-terminal.

Checks that the UI asks for status on connect, that `d` sends `diag`, that
the diag replies show up on screen, and that HEALTH lines go to the health
CSV and not the log panel. No hardware needed. Removes the log files the run
creates. Exit code 0 = pass.

    python3 debug/tests/test_ui_fake_board.py
"""

import fcntl
import os
import pty
import re
import select
import struct
import sys
import termios
import time
import tty

HERE = os.path.dirname(os.path.abspath(__file__))
HOST = os.path.normpath(os.path.join(HERE, "..", "..", "host"))
LOGS = os.path.join(HOST, "logs")


def main():
    before = set(os.listdir(LOGS)) if os.path.isdir(LOGS) else set()
    bm, bs = pty.openpty()
    tty.setraw(bm)
    tty.setraw(bs)
    pid, ui = pty.fork()
    if pid == 0:
        os.environ["TERM"] = "xterm"
        os.execvp(sys.executable, [sys.executable, os.path.join(HOST, "weather_ui.py"), "--port", os.ttyname(bs)])
    fcntl.ioctl(ui, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 160, 0, 0))
    got, screen = b"", b""

    def pump(t):
        nonlocal got, screen
        end = time.time() + t
        while time.time() < end:
            r, _, _ = select.select([bm, ui], [], [], 0.1)
            if bm in r:
                got += os.read(bm, 4096)
            if ui in r:
                try:
                    screen += os.read(ui, 65536)
                except OSError:
                    return

    ok = True
    try:
        pump(2.0)
        os.write(bm, b"STAT,BASE,0,0,5000,3,1,1000,UPLOAD\r\n")
        pump(0.5)
        os.write(ui, b"d")
        pump(1.0)
        checks = [("UI sent status on connect", b"status\n" in got),
                  ("d key sent diag", b"diag\n" in got)]
        os.write(bm, b"LOG,diag i2c SDA(A4)=L SCL(A5)=L  (H=pulled up, F=floating, L=held low)\r\n"
                     b"LOG,diag radio: all zeros = MISO stuck low\r\n")
        pump(1.0)
        flat = re.sub(rb"\x1b\[[0-9;?]*[A-Za-z]|\x1b[()][0-9A-B]|\x1b[=>]", b"", screen).replace(b" ", b"")
        checks += [("help line shows 'd wiring check'", b"dwiringcheck" in flat),
                   ("diag reply on screen", b"SDA(A4)=L" in flat and b"MISOstucklow" in flat)]
        os.write(bm, b"HEALTH,L,3,30000,4612,3291,34.5,0,0,0,10,0,100,12,5300\r\n"
                     b"HEALTH,R,7,70000,4598,3288,36.0,2,1,0,10,14,0,85,5350\r\n")
        pump(1.0)
        new = set(os.listdir(LOGS)) - before
        health = [n for n in new if n.startswith("health_")]
        rows = open(os.path.join(LOGS, health[0])).read().splitlines() if health else []
        checks += [("health CSV written with header + 2 rows", len(rows) == 3 and rows[0].startswith("pc_time,node,seq")),
                   ("remote health row has power-loss count 2", len(rows) == 3 and rows[2].split(",")[1:3] == ["remote", "7"]
                    and rows[2].split(",")[7] == "2"),
                   ("health lines kept out of the log panel", b"HEALTH,L," not in screen and b"HEALTH,R," not in screen)]
        flat = re.sub(rb"\x1b\[[0-9;?]*[A-Za-z]|\x1b[()][0-9A-B]|\x1b[=>]", b"", screen).replace(b" ", b"")
        checks += [("health panel shows both boards' supply", b"5V4.61V" in flat and b"5V4.60V" in flat),
                   ("health panel shows remote power losses", b"radiopowerlosses2" in flat)]
        for name, passed in checks:
            print(("PASS " if passed else "FAIL ") + name)
            ok &= passed
        os.write(ui, b"q")
        pump(0.5)
    finally:
        try:
            os.kill(pid, 9)
        except ProcessLookupError:
            pass
        os.waitpid(pid, 0)
        for name in set(os.listdir(LOGS)) - before if os.path.isdir(LOGS) else []:
            os.remove(os.path.join(LOGS, name))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
