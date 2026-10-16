#!/usr/bin/env python3
"""Live radio watch: shows the moment the nRF24 turns on or off while you
press on the module, to find a loose joint.

    python3 debug/radio_watch.py                 # auto-detects the Nano's port
    python3 debug/radio_watch.py --port /dev/ttyACM0
    python3 debug/radio_watch.py --quiet         # no beep

The board samples the radio 100 times a second (firmware "watch" command) and
stops on its own about 10 s after this script exits. Close weather_ui.py
first: only one program can have the port open. Ctrl+C to stop.
"""

import argparse
import datetime as dt
import os
import sys
import time
from collections import deque

import serial

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "host"))
from weather_ui import BAUD, find_port  # noqa: E402
from serial_probe import holder  # noqa: E402

KEEPALIVE_S = 3.0           # firmware watch mode lasts 10 s per "watch" command
TIMELINE = 60               # timeline cells, 100 ms each = last 6 s

GREEN, YELLOW, RED, DIM, BOLD, RESET = "\033[32m", "\033[33m", "\033[31m", "\033[2m", "\033[1m", "\033[0m"
STATES = {
    "ON":      (GREEN,  "█"),
    "FLICKER": (YELLOW, "▒"),
    "OFF":     (RED,    "·"),
}


def classify(valid, samples):
    if samples == 0 or valid == 0:
        return "OFF"
    return "ON" if valid == samples else "FLICKER"


def now_str():
    return dt.datetime.now().strftime("%H:%M:%S.%f")[:-3]


class Watch:
    def __init__(self, beep, color):
        self.beep = beep
        self.color = color
        self.state = None
        self.since = time.monotonic()
        self.timeline = deque(maxlen=TIMELINE)
        self.history = deque(maxlen=100)        # last 10 s of (valid, samples)
        self.on_count = 0
        self.longest_on = 0.0

    def c(self, code, text):
        return f"{code}{text}{RESET}" if self.color else text

    def event(self, text):
        if self.color:
            sys.stdout.write("\r\033[K")        # clear the live line, print the event above it
        print(text, flush=True)

    def update(self, valid, samples, status):
        state = classify(valid, samples)
        t = time.monotonic()
        if state != self.state:
            held = t - self.since
            if self.state == "ON":
                self.longest_on = max(self.longest_on, held)
            if self.state is not None:
                prev = f"(was {self.state} for {held:.2f} s)"
            else:
                prev = "(start)"
            code = STATES[state][0]
            self.event(f"{now_str()}  {self.c(BOLD + code, f'{state:<7}')}  STATUS=0x{status}  {self.c(DIM, prev)}")
            if state == "ON":
                self.on_count += 1
                if self.beep:
                    sys.stdout.write("\a")
            self.state, self.since = state, t
        self.timeline.append(state)
        self.history.append((valid, samples))
        self.draw(status)

    def draw(self, status):
        cells = "".join(self.c(STATES[s][0], STATES[s][1]) for s in self.timeline)
        v = sum(h[0] for h in self.history)
        n = sum(h[1] for h in self.history) or 1
        code = STATES[self.state][0]
        line = (f"{self.c(BOLD + code, f'{self.state:<7}')} STATUS=0x{status}  "
                f"[{cells}{' ' * (TIMELINE - len(self.timeline))}]  "
                f"alive {100 * v / n:3.0f}% (10 s)  ON events {self.on_count}  longest ON {self.longest_on:.1f} s")
        if self.color:
            sys.stdout.write("\r\033[K" + line)
            sys.stdout.flush()


def run(port, beep, color):
    print(f"Radio watch on {port}. Press on the radio module; Ctrl+C to stop.")
    print(f"{'ON':<7} = radio answers on every sample  {'FLICKER':<7} = some samples  {'OFF':<7} = silent")
    print("Timeline: newest on the right, 100 ms per cell.\n")
    watch = Watch(beep, color)
    with serial.Serial(port, BAUD, timeout=0.2) as ser:
        ser.reset_input_buffer()
        last_keepalive = 0.0
        last_data = time.monotonic()
        while True:
            t = time.monotonic()
            if t - last_keepalive >= KEEPALIVE_S:
                ser.write(b"watch\n")
                last_keepalive = t
            raw = ser.readline()
            if not raw:
                if time.monotonic() - last_data > 3:
                    watch.event(f"{now_str()}  no watch data from the board - is it running the latest firmware?")
                    last_data = time.monotonic()
                continue
            line = raw.decode(errors="replace").strip()
            if line.startswith("W,"):
                parts = line.split(",")
                if len(parts) == 4 and parts[1].isdigit() and parts[2].isdigit():
                    last_data = time.monotonic()
                    watch.update(int(parts[1]), int(parts[2]), parts[3])
            elif line.startswith("LOG,boot"):
                watch.event(f"{now_str()}  board rebooted: {line[4:]}")
                time.sleep(0.3)                 # let setup() finish, then re-arm watch mode
                ser.write(b"watch\n")
                last_keepalive = time.monotonic()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", help="serial port, e.g. /dev/ttyACM0 or COM5 (default: auto-detect)")
    ap.add_argument("--quiet", action="store_true", help="no beep when the radio turns on")
    args = ap.parse_args()
    port = args.port or find_port()
    if not port:
        sys.exit("No Arduino found. Plug it in or pass --port.")
    other = holder(port)
    if other:
        sys.exit(f"{port} is open in another program ({other}). Close it first.")
    color = sys.stdout.isatty()
    while True:
        try:
            run(port, not args.quiet, color)
        except KeyboardInterrupt:
            print("\nstopped (the board leaves watch mode by itself within 10 s)")
            return
        except serial.SerialException as e:
            print(f"\n{now_str()}  lost {port}: {e}; retrying...")
            time.sleep(1)
            port = args.port or find_port() or port


if __name__ == "__main__":
    main()
