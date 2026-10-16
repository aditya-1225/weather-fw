#!/usr/bin/env python3
"""Terminal UI and logger for the BMP280 / nRF24 weather link.

For two Arduino Nano Every boards. Plug in the base board (or the remote, to
watch it on its own) and run:

    python3 weather_ui.py                 # auto-detects the Nano's port
    python3 weather_ui.py --port /dev/ttyACM0

Every reading goes to logs/weather_<start time>.csv, the boards' health reports
(supply voltages, radio power losses, ...) to logs/health_<start time>.csv, and
every serial line to logs/serial_<start time>.log. The serial port is reopened automatically if
the USB cable is pulled or the board resets.

Needs: pip install pyserial   (on Windows also: pip install windows-curses)
"""

import argparse
import csv
import curses
import datetime as dt
import math
import os
import threading
import time
from collections import deque

import serial
from serial.tools import list_ports

BAUD = 115200
PA_NAMES = ["MIN", "LOW", "HIGH", "MAX"]
NANO_USB_IDS = {0x1A86, 0x0403, 0x2341, 0x10C4}   # CH340, FTDI, Arduino, CP210x


def altitude_m(pressure_pa, sea_level_pa=101325.0):
    if pressure_pa is None or pressure_pa <= 0:
        return None
    return 44330.0 * (1.0 - (pressure_pa / sea_level_pa) ** (1 / 5.255))


def num(text, kind=float):
    try:
        value = kind(text)
    except ValueError:
        return None
    if kind is float and math.isnan(value):
        return None
    return value


def find_port():
    ports = list(list_ports.comports())
    for p in ports:
        if p.vid in NANO_USB_IDS:
            return p.device
    for p in ports:
        if "USB" in p.device or "ACM" in p.device:
            return p.device
    return None


HEALTH_COLUMNS = ["seq", "node_uptime_ms", "vdd_mv", "rail_3v3_mv", "mcu_temp_c",
                  "radio_power_losses", "radio_unreachable", "sensor_fails",
                  "readings", "sends", "strong_pct", "max_loop_ms", "free_ram"]


class State:
    """Everything the board has told us, parsed from its serial lines."""

    def __init__(self, csv_path, raw_path, health_path=None):
        self.lock = threading.Lock()
        self.connected = False
        self.port = None
        self.role = None                      # "BASE" / "REMOTE"
        self.status = {}                      # last STAT line of this board
        self.readings = {"L": None, "R": None}
        self.health = {"L": None, "R": None}     # latest HEALTH report per node
        self.link = {}
        self.tx = {}
        self.log = deque(maxlen=200)
        self.log_total = 0                    # lines ever added, so a viewer can tell which are new
        self.commands = {}                    # (L|R, name) -> (result, time) of the latest CMD line
        self.csv_path = csv_path
        new_file = not os.path.exists(csv_path)
        self.csv_file = open(csv_path, "a", newline="")
        self.csv = csv.writer(self.csv_file)
        if new_file:
            self.csv.writerow(["pc_time", "node", "seq", "node_uptime_ms",
                               "temp_c", "pressure_pa", "altitude_m", "sensor_ok"])
        self.raw_file = open(raw_path, "a")
        self.health_path = health_path
        self.health_csv = None                # opened on the first HEALTH line

    def note(self, text):
        with self.lock:
            self._log(text)

    def _log(self, text):
        """Caller holds the lock."""
        self.log.append(f"{time.strftime('%H:%M:%S')}  {text}")
        self.log_total += 1

    def handle_line(self, line):
        now = time.time()
        stamp = dt.datetime.now().isoformat(timespec="milliseconds")
        self.raw_file.write(f"{stamp} {line}\n")
        self.raw_file.flush()
        parts = line.split(",")
        kind = parts[0]
        with self.lock:
            if kind == "DATA" and len(parts) >= 7:
                which = parts[1]
                reading = {
                    "seq": num(parts[2], int),
                    "uptime_ms": num(parts[3], int),
                    "temp_c": num(parts[4]),
                    "pressure_pa": num(parts[5]),
                    "sensor_ok": parts[6] == "1",
                    "time": now,
                }
                reading["altitude_m"] = altitude_m(reading["pressure_pa"])
                self.readings[which] = reading
                node = self.role.lower() if which == "L" and self.role else ("remote" if which == "R" else "local")
                alt = reading["altitude_m"]
                self.csv.writerow([stamp, node, parts[2], parts[3], parts[4], parts[5],
                                   f"{alt:.1f}" if alt is not None else "nan", parts[6]])
                self.csv_file.flush()
            elif kind == "LINK" and len(parts) >= 2:
                keys = ["up", "rx", "lost", "strong", "arc", "remote_tx_fails",
                        "remote_inits", "remote_pa", "remote_interval", "remote_reset", "repeats",
                        "remote_bmp", "remote_retry", "remote_searching"]
                self.link.update(dict(zip(keys, parts[1:])))
                self.link["time"] = now
            elif kind == "TX" and len(parts) >= 6:
                self.tx = dict(zip(["ok", "arc", "tx_ok", "tx_fails", "inits"], parts[1:]))
                self.tx["time"] = now
            elif kind == "STAT" and len(parts) >= 9:
                self.role = parts[1]
                self.status = dict(zip(["role", "radio_ok", "sensor_ok", "uptime_ms", "inits", "pa", "interval",
                                        "reset", "health", "bmp", "retry", "link", "prev_link", "searching"],
                                       parts[1:]))
            elif kind == "HEALTH" and len(parts) >= 2 + len(HEALTH_COLUMNS):
                if parts[1] in self.health:
                    report = dict(zip(HEALTH_COLUMNS, parts[2:]), time=now)
                    # Time between this report and the previous one, by the board's own clock.
                    # The only way to see the remote's health interval: it isn't in any packet.
                    prev = self.health[parts[1]] or {}
                    seq, up = num(report["seq"], int), num(report["node_uptime_ms"], int)
                    pseq, pup = num(prev.get("seq", ""), int), num(prev.get("node_uptime_ms", ""), int)
                    if None not in (seq, up, pseq, pup) and seq > pseq and up > pup:
                        report["period_ms"] = round((up - pup) / (seq - pseq))   # a lost report spans two
                    else:
                        report["period_ms"] = None    # first report, or the board restarted
                    self.health[parts[1]] = report
                if self.health_path:
                    if self.health_csv is None:
                        new_file = not os.path.exists(self.health_path)
                        self.health_file = open(self.health_path, "a", newline="")
                        self.health_csv = csv.writer(self.health_file)
                        if new_file:
                            self.health_csv.writerow(["pc_time", "node"] + HEALTH_COLUMNS)
                    node = self.role.lower() if parts[1] == "L" and self.role else "remote"
                    self.health_csv.writerow([stamp, node] + parts[2:2 + len(HEALTH_COLUMNS)])
                    self.health_file.flush()
            elif kind == "LOG":
                self._log(line[4:])
                if "boot role=" in line:
                    self.role = line.split("role=")[1].split()[0]
            elif kind == "CMD" and len(parts) >= 5:
                where = "this board" if parts[1] == "L" else "remote"
                self.commands[(parts[1], parts[3])] = (parts[4], now)
                self._log(f"command {parts[3]} -> {where}: {parts[4]}")
            elif line:
                self._log(line)


class SerialLink(threading.Thread):
    """Reads lines forever, reconnecting whenever the port goes away."""

    def __init__(self, port, state):
        super().__init__(daemon=True)
        self.port_arg = port
        self.state = state
        self.ser = None
        self.write_lock = threading.Lock()
        self.stop = threading.Event()

    def send(self, text):
        with self.write_lock:
            if self.ser is None:
                self.state.note(f"not connected, '{text}' not sent")
                return
            try:
                self.ser.write((text + "\n").encode())
            except serial.SerialException as exc:
                self.state.note(f"write failed: {exc}")

    def run(self):
        last_error = None
        while not self.stop.is_set():
            port = self.port_arg or find_port()
            if port is None:
                if last_error != "none":
                    self.state.note("no Arduino found, waiting for one to be plugged in")
                    last_error = "none"
                time.sleep(1)
                continue
            try:
                ser = serial.Serial(port, BAUD, timeout=0.2)
            except serial.SerialException as exc:
                if last_error != str(exc):
                    self.state.note(f"cannot open {port}: {exc}")
                    last_error = str(exc)
                time.sleep(1)
                continue
            last_error = None
            with self.write_lock:
                self.ser = ser
            with self.state.lock:
                self.state.connected, self.state.port = True, port
            self.state.note(f"connected to {port}")
            self.send("status")   # ask who it is, in case the boot message was missed
            buf = b""
            try:
                while not self.stop.is_set():
                    buf += ser.read(256)
                    while b"\n" in buf:
                        raw, buf = buf.split(b"\n", 1)
                        line = raw.decode(errors="replace").strip()
                        if line:
                            self.state.handle_line(line)
            except (serial.SerialException, OSError) as exc:
                self.state.note(f"lost {port}: {exc}")
            with self.write_lock:
                self.ser = None
            with self.state.lock:
                self.state.connected = False
            try:
                ser.close()
            except Exception:
                pass
            time.sleep(1)


# ---------------------------------------------------------------- drawing --

def fmt(value, spec, unit=""):
    return "--" if value is None else f"{value:{spec}}{unit}"


def age(t):
    if t is None:
        return "never"
    s = time.time() - t
    return f"{s:.1f} s ago" if s < 120 else f"{s / 60:.0f} min ago"


def uptime(ms):
    try:
        s = int(ms) // 1000
    except (TypeError, ValueError):
        return "--"
    return f"{s // 3600:d}:{s // 60 % 60:02d}:{s % 60:02d}"


def pa_name(v):
    try:
        return PA_NAMES[int(v)]
    except (TypeError, ValueError, IndexError):
        return "--"


class UI:
    def __init__(self, scr, state, link):
        self.scr, self.state, self.link = scr, state, link
        curses.curs_set(0)
        curses.use_default_colors()
        curses.init_pair(1, curses.COLOR_GREEN, -1)
        curses.init_pair(2, curses.COLOR_RED, -1)
        curses.init_pair(3, curses.COLOR_YELLOW, -1)
        curses.init_pair(4, curses.COLOR_CYAN, -1)
        self.GOOD, self.BAD = curses.color_pair(1), curses.color_pair(2)
        self.WARN, self.HEAD = curses.color_pair(3), curses.color_pair(4) | curses.A_BOLD
        scr.timeout(200)

    def put(self, y, x, text, attr=0):
        h, w = self.scr.getmaxyx()
        if 0 <= y < h and x < w:
            try:
                self.scr.addstr(y, x, text[: max(0, w - x - 1)], attr)
            except curses.error:
                pass

    def reading_block(self, y, x, title, r, extra):
        self.put(y, x, title, self.HEAD)
        if r is None:
            self.put(y + 1, x, "no data yet", self.WARN)
            return
        stale = time.time() - r["time"] > 5
        attr = self.WARN if stale else 0
        self.put(y + 1, x, f"Temperature  {fmt(r['temp_c'], '7.2f', ' °C')}", attr)
        p = r["pressure_pa"]
        self.put(y + 2, x, f"Pressure     {fmt(p / 100 if p else None, '7.2f', ' hPa')}", attr)
        self.put(y + 3, x, f"Altitude     {fmt(r['altitude_m'], '7.1f', ' m')}  (std. sea level)", attr)
        self.put(y + 4, x, "Sensor       ")
        self.put(y + 4, x + 13, "OK" if r["sensor_ok"] else "NOT RESPONDING",
                 self.GOOD if r["sensor_ok"] else self.BAD)
        self.put(y + 5, x, f"Sample #{r['seq']}  {age(r['time'])}", attr)
        for i, line in enumerate(extra):
            self.put(y + 6 + i, x, line)

    def health_block(self, y, x, h, width):
        put = lambda yy, xx, text, attr=0: self.put(yy, xx, text[: max(0, x + width - 1 - xx)], attr)
        put(y, x, "HEALTH", self.HEAD)
        if h is None:
            put(y, x + 7, "no report yet", self.WARN)
            return
        stale = time.time() - h["time"] > 25
        if stale:
            put(y, x + 7, f"last report {age(h['time'])}", self.WARN)
        vdd, rail = num(h["vdd_mv"], int), num(h["rail_3v3_mv"], int)
        losses, unreach, sfails = (num(h[k], int) or 0 for k in ("radio_power_losses", "radio_unreachable", "sensor_fails"))
        strong = h["strong_pct"]
        period = f"{h['period_ms'] / 1000:.0f}s" if h.get("period_ms") else "report"
        # Short lines so each board's block fits in half of an 80-column terminal.
        rows = [
            [(f"5V {vdd / 1000:.2f} V" if vdd else "5V --", self.WARN if vdd and vdd < 4300 else 0),
             (f"3V3 {rail / 1000:.2f} V" if rail else "3V3 --", self.WARN if rail and not 3000 <= rail <= 3600 else 0)],
            [(f"MCU {h['mcu_temp_c']} °C", 0), (f"RAM {h['free_ram']} B", 0), (f"loop {h['max_loop_ms']} ms", 0)],
            [(f"radio power losses {losses}", self.BAD if losses else 0), (f"unreachable {unreach}", self.BAD if unreach else 0)],
            [(f"sensor fails {sfails}", self.BAD if sfails else 0), (f"sends {h['sends']}/{period}", 0),
             (f"strong {'--' if strong == 'nan' else strong + '%'}", 0)],   # nan = no replies heard
        ]
        for i, row in enumerate(rows):
            x0 = x + 7 if i == 0 else x
            if i == 0 and stale:
                x0 += len(f"last report {age(h['time'])}") + 2
            for text, attr in row:
                put(y + i, x0, text, attr)
                x0 += len(text) + 2

    def draw(self):
        s = self.state
        scr = self.scr
        scr.erase()
        h, w = scr.getmaxyx()
        with s.lock:
            conn = s.connected
            role = s.role or "?"
            st = dict(s.status)
            local, remote = s.readings["L"], s.readings["R"]
            health_l, health_r = s.health["L"], s.health["R"]
            link, tx = dict(s.link), dict(s.tx)
            log = list(s.log)

        self.put(0, 0, "Weather link", curses.A_BOLD)
        self.put(0, 14, f"{s.port or 'no port'}", 0)
        self.put(0, 14 + len(s.port or 'no port') + 1,
                 "connected" if conn else "DISCONNECTED", self.GOOD if conn else self.BAD)
        self.put(1, 0, f"Board: {role}   up {uptime(st.get('uptime_ms'))}   "
                       f"radio {'OK' if st.get('radio_ok') == '1' else 'DOWN'}   "
                       f"PA {pa_name(st.get('pa'))}   interval {st.get('interval', '--')} ms   "
                       f"radio inits {st.get('inits', '--')}   last reset {st.get('reset', '--').strip()}")
        self.put(2, 0, f"Logging to {s.csv_path}", curses.A_DIM)

        col = max(40, w // 2)
        self.reading_block(4, 0, f"THIS BOARD ({role})", local, [])
        if role == "BASE":
            remote_extra = [f"PA {pa_name(link.get('remote_pa'))}   interval {link.get('remote_interval', '--')} ms",
                            f"radio inits {link.get('remote_inits', '--')}   last reset {link.get('remote_reset', '--').strip()}"]
            self.reading_block(4, col, "REMOTE", remote, remote_extra)
        self.health_block(12, 0, health_l, col if role == "BASE" else w)
        if role == "BASE":
            self.health_block(12, col, health_r, w - col)

        y = 17
        if role == "BASE":
            up = link.get("up") == "1"
            self.put(y, 0, "LINK ", self.HEAD)
            self.put(y, 5, "UP" if up else "DOWN", self.GOOD if up else self.BAD)
            rx, lost = num(link.get("rx", ""), int) or 0, num(link.get("lost", ""), int) or 0
            loss = 100.0 * lost / (rx + lost) if rx + lost else 0.0
            sig = {"1": "strong (> -64 dBm)", "0": "weak (< -64 dBm)"}.get(link.get("strong"), "--")
            # Readings the remote sent but got no reply for. The base usually has them anyway
            # ("lost" counts what the base never got); only commands need the reply.
            fails, rseq = num(link.get("remote_tx_fails", ""), int), remote["seq"] if remote else None
            missed = f"{fails}" if fails is not None else "--"
            if fails is not None and rseq and rseq >= fails:
                missed += f" ({100.0 * fails / rseq:.1f}%)"
            self.put(y, 11, f"received {rx}  lost {lost} ({loss:.1f}%)  signal {sig}  "
                            f"retries {link.get('arc', '--')}/15  replies missed by remote {missed}")
        elif role == "REMOTE":
            ok = tx.get("ok") == "1"
            self.put(y, 0, "LINK ", self.HEAD)
            self.put(y, 5, "ACKED" if ok else "NO ACK", self.GOOD if ok else self.BAD)
            self.put(y, 13, f"sent {tx.get('tx_ok', '--')}  failed {tx.get('tx_fails', '--')}  "
                            f"retries {tx.get('arc', '--')}/15  {age(tx.get('time'))}")

        keys = ("p ping remote   r reset remote   n remote radio re-init   s remote sensor re-init   a remote PA",
                "R reset this board   N radio re-init   S sensor re-init   A PA   d wiring check   i interval (both)   : raw   q quit")
        self.put(19, 0, keys[0] if role == "BASE" else "(remote commands are available on the base)", curses.A_DIM)
        self.put(20, 0, keys[1], curses.A_DIM)

        self.put(22, 0, "LOG", self.HEAD)
        room = h - 23
        for i, line in enumerate(log[-room:] if room > 0 else []):
            attr = self.BAD if any(k in line for k in ("lost", "FAIL", "TIMEOUT", "not ", "DOWN")) else 0
            self.put(23 + i, 0, line, attr)
        scr.refresh()

    def prompt(self, question):
        h, _ = self.scr.getmaxyx()
        self.scr.timeout(-1)
        curses.echo()
        curses.curs_set(1)
        self.put(h - 1, 0, " " * 200)
        self.put(h - 1, 0, question, curses.A_REVERSE)
        try:
            answer = self.scr.getstr(h - 1, len(question) + 1, 60).decode().strip()
        finally:
            curses.noecho()
            curses.curs_set(0)
            self.scr.timeout(200)
        return answer

    def confirm(self, question):
        return self.prompt(question + " [y/N]").lower().startswith("y")

    def need_base(self):
        if self.state.role != "BASE":
            self.state.note("remote commands only work from the base board")
            return False
        return True

    def cycle_pa(self, current):
        return ((num(current or "", int) or 0) + 1) % 4

    def run(self):
        send = self.link.send
        while True:
            self.draw()
            ch = self.scr.getch()
            if ch == -1:
                continue
            key = chr(ch) if 0 <= ch < 256 else ""
            if key == "q":
                return
            elif key == "p" and self.need_base():
                send("remote ping")
            elif key == "r" and self.need_base() and self.confirm("Reset the REMOTE board?"):
                send("remote reset")
            elif key == "n" and self.need_base():
                send("remote radio")
            elif key == "s" and self.need_base():
                send("remote sensor")
            elif key == "a" and self.need_base():
                with self.state.lock:
                    pa = self.cycle_pa(self.state.link.get("remote_pa"))
                send(f"remote pa {pa}")
            elif key == "R" and self.confirm("Reset THIS board?"):
                send("reset")
            elif key == "N":
                send("radio")
            elif key == "S":
                send("sensor")
            elif key == "A":
                with self.state.lock:
                    pa = self.cycle_pa(self.state.status.get("pa"))
                send(f"pa {pa}")
            elif key == "d":
                send("diag")
            elif key == "i":
                ms = num(self.prompt("Sample interval in ms (200-60000):"), int)
                if ms is None:
                    continue
                send(f"interval {ms}")
                if self.state.role == "BASE":
                    send(f"remote interval {ms}")
            elif key == ":":
                cmd = self.prompt("command (e.g. status, help, remote ping):")
                if cmd:
                    send(cmd)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", help="serial port, e.g. /dev/ttyACM0 or COM5 (default: auto-detect)")
    ap.add_argument("--logdir", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs"))
    args = ap.parse_args()

    os.makedirs(args.logdir, exist_ok=True)
    start = time.strftime("%Y%m%d_%H%M%S")
    state = State(os.path.join(args.logdir, f"weather_{start}.csv"),
                  os.path.join(args.logdir, f"serial_{start}.log"),
                  os.path.join(args.logdir, f"health_{start}.csv"))
    link = SerialLink(args.port, state)
    link.start()
    try:
        curses.wrapper(lambda scr: UI(scr, state, link).run())
    except KeyboardInterrupt:
        pass
    finally:
        link.stop.set()
        print(f"readings saved to {state.csv_path}")


if __name__ == "__main__":
    main()
