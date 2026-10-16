#!/usr/bin/env python3
"""The GUI and the firmware must agree on every settings code.

Compiles the code functions straight out of firmware/weather_node/weather_node.ino
with g++, asks them about every number from 0 to 99999, and compares with
host/weather_settings.py. Also checks the numbers the GUI shows against values
worked out by hand. Exit code 0 = pass.

    python3 debug/tests/test_settings_codes.py
"""

import os
import re
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "host"))

from weather_settings import Link, Retry, Sensor   # noqa: E402

INO = os.path.join(ROOT, "firmware", "weather_node", "weather_node.ino")


def firmware_codes():
    src = open(INO).read()
    structs = "\n".join(re.findall(r"^struct +(?:SensorCfg|LinkCfg|RetryCfg) *\{[^}]*\};", src, re.M))
    a = src.index("bool validSensor(")
    b = src.index("void printLink(")
    harness = f"""
#include <cstdint>
#include <cstdio>
enum {{ RF24_1MBPS, RF24_2MBPS, RF24_250KBPS }};
enum {{ RF24_CRC_DISABLED, RF24_CRC_8, RF24_CRC_16 }};
{structs}
{src[a:b]}
int main() {{
  for (int32_t v = -1; v <= 99999; v++) {{
    SensorCfg s; LinkCfg l; RetryCfg r;
    if (sensorFromCode(v, s)) printf("S %ld %d %d %d %d %d %ld\\n", (long)v, s.osrsT, s.osrsP, s.filter, s.standby, s.forced, (long)sensorCode(s));
    if (linkFromCode(v, l))   printf("L %ld %d %d %d %ld\\n", (long)v, l.channel, l.rate, l.crc, (long)linkCode(l));
    if (retryFromCode(v, r))  printf("R %ld %d %d %d %ld\\n", (long)v, r.delay, r.count, r.attempts, (long)retryCode(r));
  }}
}}
"""
    d = tempfile.mkdtemp()
    with open(os.path.join(d, "codes.cpp"), "w") as f:
        f.write(harness)
    subprocess.run(["g++", "-std=c++11", "-Wall", "-o", os.path.join(d, "codes"), os.path.join(d, "codes.cpp")],
                   check=True)
    out = subprocess.run([os.path.join(d, "codes")], check=True, capture_output=True, text=True).stdout
    fw = {"S": {}, "L": {}, "R": {}}
    for line in out.split("\n"):
        if line:
            kind, v, *fields = line.split()
            fw[kind][int(v)] = tuple(int(x) for x in fields)
    return fw


def main():
    checks = []
    fw = firmware_codes()
    py = {"S": {}, "L": {}, "R": {}}
    for v in range(0, 100000):
        for kind, cls in (("S", Sensor), ("L", Link), ("R", Retry)):
            obj = cls.from_code(v)
            if obj:
                fields = {"S": lambda o: (o.osrs_t, o.osrs_p, o.filter, o.standby, o.forced),
                          "L": lambda o: (o.channel, o.rate, o.crc),
                          "R": lambda o: (o.delay, o.count, o.attempts)}[kind](obj)
                py[kind][v] = fields + (obj.code,)
    for kind, name, count in (("S", "bmp", 5 * 5 * 5 * 8 * 2), ("L", "link", 126 * 3 * 2), ("R", "retry", 16 * 16 * 9)):
        checks.append((f"{name}: firmware and GUI accept the same {len(fw[kind])} codes, decoded the same",
                       fw[kind] == py[kind]))
        checks.append((f"{name}: {count} valid settings, each with exactly one code",
                       len(fw[kind]) == count and all(f[-1] == v for v, f in fw[kind].items())))
    checks += [
        ("defaults are the firmware's defaults", (Sensor().code, Link().code, Retry().code) == (25410, 10822, 10154)),
        ("no 'skipped' oversampling (pressure needs the temperature)",
         Sensor.from_code(5410) is None and Sensor.from_code(20410) is None),
        ("channel 126 and CRC off are refused", Link.from_code(12622) is None and Link.from_code(10820) is None),
        ("0 sends per reading is refused", Retry.from_code(10150) is None),
        ("'-' (older remote) decodes to nothing", Sensor.from_code("-") is None and Retry.from_code(None) is None),
    ]

    # Numbers shown in the window, against hand-worked values.
    s = Sensor()
    typ, mx = s.measure_ms()
    checks += [
        ("x2/x16 measurement: typ 1 + 4 + 32.5 = 37.5 ms, max 1.25 + 4.6 + 37.375 = 43.225 ms",
         abs(typ - 37.5) < 1e-9 and abs(mx - 43.225) < 1e-9),
        ("continuous: new value every 37.5 + 62.5 = 100 ms", abs(s.period_ms(1000) - 100) < 1e-9),
        ("filter x16 settles 75 % in 22 x 100 ms = 2.2 s", abs(s.settle_s(1000) - 2.2) < 1e-9),
        ("one per reading: filter steps once per reading (22 x 1 s)",
         abs(Sensor(forced=1).settle_s(1000) - 22.0) < 1e-9),
        ("standby 1000 ms at 200 ms readings is flagged",
         any(x.startswith("⚠") for x in Sensor(standby=5).explain(200))),
    ]
    link = Link()
    # 250 kbps, 16-bit CRC: (8 * (1 + 5 + 32 + 2) + 9) bits = 329 bits = 1316 us; try = 130 + 1316 + 2750 us
    want = 4 * 16 * (130 + 1316 + 2750) / 1000 + 3 * 8.5
    checks += [
        (f"default give-up time = 4 x 16 tries x 4.196 ms + 3 pauses = {want:.1f} ms",
         abs(Retry().give_up_ms(link) - want) < 1e-6),
        ("250 kbps raises a 500 us retry wait to 1500 us", Retry(delay=1).delay_us(link) == 1500),
        ("1 Mbps keeps 500 us", Retry(delay=1).delay_us(Link(rate=0)) == 500),
        ("1 Mbps raises 250 us to 500 us", Retry(delay=0).delay_us(Link(rate=0)) == 500),
        ("channel 108 is flagged as outside the licence-free band", "outside" in link.explain()[0]),
        ("channel 76 isn't", "inside" in Link(channel=76).explain()[0]),
    ]

    ok = True
    for name, passed in checks:
        print(("PASS " if passed else "FAIL ") + name)
        ok &= passed
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
