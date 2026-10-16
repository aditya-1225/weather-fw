#!/usr/bin/env python3
"""Send commands to a board and print what it says back, without the UI.

    python3 debug/serial_probe.py --list                       # boards and their USB serial numbers
    python3 debug/serial_probe.py --send diag                  # wiring check on the only board
    python3 debug/serial_probe.py --board 2CF4 --send status --send diag --secs 8
    python3 debug/serial_probe.py --baud 9600 --secs 10        # just listen (e.g. an old 9600-baud sketch)

--board matches the start of the USB serial number, so with both boards
plugged in you can pick one without knowing whether it is ttyACM0 or ttyACM1.
Close weather_ui.py first: only one program can have the port open.
"""

import argparse
import glob
import os
import sys
import time

import serial
from serial.tools import list_ports


def find(board):
    ports = [p for p in list_ports.comports() if p.vid is not None]
    if board:
        ports = [p for p in ports if (p.serial_number or "").upper().startswith(board.upper())]
    if not ports:
        sys.exit("no matching board found (try --list)")
    if len(ports) > 1:
        sys.exit("more than one board found, pick one with --board or --port:\n" + listing(ports))
    return ports[0].device


def holder(port):
    """The command line of another process that has `port` open (Linux), else None."""
    for fd in glob.glob("/proc/[0-9]*/fd/*"):
        try:
            if os.readlink(fd) == port and int(fd.split("/")[2]) != os.getpid():
                with open(f"/proc/{fd.split('/')[2]}/cmdline", "rb") as f:
                    return f.read().replace(b"\0", b" ").decode(errors="replace").strip()
        except (OSError, ValueError):
            pass
    return None


def listing(ports):
    return "\n".join(f"  {p.device:<14} serial {p.serial_number or '?':<34} {p.description}" for p in ports)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", help="serial port, e.g. /dev/ttyACM0")
    ap.add_argument("--board", help="start of the board's USB serial number, e.g. 2CF4 or B800")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--send", action="append", default=[], help="command to send (repeatable)")
    ap.add_argument("--secs", type=float, default=5.0, help="how long to listen (default 5)")
    ap.add_argument("--list", action="store_true", help="list connected boards and exit")
    args = ap.parse_args()

    if args.list:
        print(listing([p for p in list_ports.comports() if p.vid is not None]) or "no boards found")
        return
    port = args.port or find(args.board)
    other = holder(port)
    if other:
        sys.exit(f"{port} is open in another program ({other}). Close it first.")
    print(f"# {port} @ {args.baud}", file=sys.stderr)
    with serial.Serial(port, args.baud, timeout=0.1) as ser:
        time.sleep(0.3)
        for cmd in args.send:
            ser.write(cmd.encode() + b"\n")
            time.sleep(0.2)
        end = time.monotonic() + args.secs
        while time.monotonic() < end:
            data = ser.read(4096)
            if data:
                sys.stdout.write(data.decode(errors="replace").replace("\r", ""))
                sys.stdout.flush()


if __name__ == "__main__":
    main()
