#!/usr/bin/env python3
"""Drives debug/radio_watch.py against a fake board on a pseudo-terminal.

Plays OFF -> ON -> FLICKER -> reboot -> OFF and checks the printed events,
their durations, and that the script re-arms watch mode after a reboot.
No hardware needed. Exit code 0 = pass.

    python3 debug/tests/test_radio_watch_fake_board.py
"""

import os
import pty
import re
import select
import sys
import time
import tty

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.normpath(os.path.join(HERE, "..", "radio_watch.py"))


def main():
    bm, bs = pty.openpty()
    tty.setraw(bm)
    tty.setraw(bs)
    pid, out = pty.fork()
    if pid == 0:
        os.environ["TERM"] = "xterm"
        os.execvp(sys.executable, [sys.executable, SCRIPT, "--port", os.ttyname(bs), "--quiet"])
    # Let go of our copy of the board side, or radio_watch.py sees the port as in use and refuses.
    os.close(bs)
    got, screen = b"", b""

    def pump(t):
        nonlocal got, screen
        end = time.time() + t
        while time.time() < end:
            r, _, _ = select.select([bm, out], [], [], 0.02)
            if bm in r:
                try:
                    got += os.read(bm, 4096)
                except OSError:             # board side not opened yet
                    time.sleep(0.02)
            if out in r:
                try:
                    screen += os.read(out, 65536)
                except OSError:
                    return

    def feed(valid, n, st, count):
        for _ in range(count):
            os.write(bm, f"W,{valid},{n},{st}\r\n".encode())
            pump(0.1)

    checks = []
    try:
        pump(0.8)
        checks.append(("sent watch on start", b"watch\n" in got))
        feed(0, 10, "FF", 10)       # 1 s off
        feed(10, 10, "0E", 15)      # 1.5 s on
        feed(4, 10, "FF", 5)        # flicker
        got = b""
        os.write(bm, b"LOG,boot role=REMOTE cause=POWER\r\n")
        pump(0.6)
        checks.append(("re-armed watch after reboot", b"watch\n" in got))
        feed(0, 10, "FF", 5)
        os.kill(pid, 2)
        pump(0.5)
    finally:
        try:
            os.kill(pid, 9)
        except ProcessLookupError:
            pass
        os.waitpid(pid, 0)

    text = re.sub(rb"\x1b\[[0-9;]*[A-Za-z]", b"", screen).decode(errors="replace").replace("\r", "\n")
    events = [l for l in text.splitlines() if re.match(r"\d\d:\d\d:\d\d\.\d{3}", l)]
    for e in events:
        print("   ", e)
    states = [m.group(1) for e in events for m in [re.search(r"\s(OFF|ON|FLICKER)\s", e)] if m]
    checks.append(("event order OFF, ON, FLICKER, OFF", states == ["OFF", "ON", "FLICKER", "OFF"]))
    on_held = [float(m.group(1)) for e in events for m in [re.search(r"was ON for ([\d.]+) s", e)] if m]
    checks.append(("ON lasted ~1.5 s", len(on_held) == 1 and 1.3 <= on_held[0] <= 1.8))
    checks.append(("reboot reported", any("board rebooted" in e for e in events)))
    ok = True
    for name, passed in checks:
        print(("PASS " if passed else "FAIL ") + name)
        ok &= passed
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
