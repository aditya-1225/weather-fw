"""The boards' runtime settings: the decimal codes the firmware uses, and what
each setting means in practice (measurement time, smoothing delay, how long a
send can take). No Qt here, so it can be tested on its own.

Codes, as in weather_node.ino and its STAT / LINK lines:
    bmp   TPFSM  e.g. 25410 = temp x2, pressure x16, filter x16, 62.5 ms standby, continuous
    link  CCCRK  e.g. 10822 = channel 108, 250 kbps, 16-bit CRC
    retry DDCCA  e.g. 10154 = 2750 us between tries, 15 retries, 4 sends per reading
Numbers are from the Bosch BMP280 and Nordic nRF24L01+ datasheets.
"""

from dataclasses import dataclass

OVERSAMPLING = [None, 1, 2, 4, 8, 16]        # code -> measurements averaged (code 0 = skipped, not allowed)
FILTER_COEF = [1, 2, 4, 8, 16]               # code -> IIR coefficient (1 = off)
FILTER_75 = [1, 2, 5, 11, 22]                # measurements until a step shows 75 % (BMP280 datasheet, filter table)
STANDBY_MS = [0.5, 62.5, 125, 250, 500, 1000, 2000, 4000]
PRESSURE_RES_PA = {1: 2.62, 2: 1.31, 4: 0.66, 8: 0.33, 16: 0.16}    # datasheet, by oversampling
TEMP_RES_C = {1: 0.005, 2: 0.0025, 4: 0.0012, 8: 0.0006, 16: 0.0003}
RATE_NAMES = ["1 Mbps", "2 Mbps", "250 kbps"]   # RF24 enum order
RATE_KBPS = [1000, 2000, 250]
SENSITIVITY_DBM = [-85, -82, -94]        # nRF24L01+ receiver sensitivity at each rate
PA_DBM = ["-18 dBm", "-12 dBm", "-6 dBm", "0 dBm"]   # the nRF24 chip; a PA+LNA module adds its amplifier's gain


def _digits(code, n):
    try:
        v = int(code)
    except (TypeError, ValueError):
        return None
    if not 0 <= v < 10 ** n:
        return None
    return [int(c) for c in f"{v:0{n}d}"]


@dataclass(frozen=True)
class Sensor:
    osrs_t: int = 2
    osrs_p: int = 5
    filter: int = 4
    standby: int = 1
    forced: int = 0

    @property
    def code(self):
        return self.osrs_t * 10000 + self.osrs_p * 1000 + self.filter * 100 + self.standby * 10 + self.forced

    @classmethod
    def from_code(cls, code):
        d = _digits(code, 5)
        if d is None:
            return None
        s = cls(*d)
        return s if s.valid() else None

    def valid(self):
        return (1 <= self.osrs_t <= 5 and 1 <= self.osrs_p <= 5 and 0 <= self.filter <= 4
                and 0 <= self.standby <= 7 and self.forced in (0, 1))

    def measure_ms(self):
        """(typical, maximum) time of one measurement, datasheet 3.8.1."""
        t, p = OVERSAMPLING[self.osrs_t], OVERSAMPLING[self.osrs_p]
        return 1 + 2 * t + 2 * p + 0.5, 1.25 + 2.3 * t + 2.3 * p + 0.575

    def period_ms(self, interval_ms):
        """Time between the chip's own measurements: continuous, or one per reading."""
        if self.forced:
            return float(interval_ms)
        return self.measure_ms()[0] + STANDBY_MS[self.standby]

    def settle_s(self, interval_ms):
        """Time until a sudden change shows 75 % in the output."""
        return FILTER_75[self.filter] * self.period_ms(interval_ms) / 1000

    def summary(self):
        f = "no filter" if self.filter == 0 else f"filter ×{FILTER_COEF[self.filter]}"
        mode = "one per reading" if self.forced else f"continuous, {STANDBY_MS[self.standby]:g} ms rest"
        return (f"temp ×{OVERSAMPLING[self.osrs_t]}, pressure ×{OVERSAMPLING[self.osrs_p]}, {f}, {mode}")

    def explain(self, interval_ms):
        """Plain sentences about what these settings do at this reading interval. Warnings start with ⚠."""
        typ, mx = self.measure_ms()
        lines = []
        if self.forced:
            lines.append(f"The chip sleeps and measures once per reading, taking about {typ:.0f} ms "
                         f"(the board waits up to {mx:.0f} ms).")
        else:
            period = self.period_ms(interval_ms)
            lines.append(f"Each measurement takes about {typ:.0f} ms; with the {STANDBY_MS[self.standby]:g} ms rest "
                         f"the chip makes a new one every {period:.0f} ms on its own.")
            per = interval_ms / period
            if per < 1:
                lines.append(f"⚠ That is slower than a reading every {interval_ms} ms: some readings repeat "
                             f"the previous value.")
            else:
                lines.append(f"A reading every {interval_ms} ms holds about {per:.1f} new measurements.")
        if self.filter == 0:
            lines.append("No smoothing: each measurement stands alone, so short gusts and door slams show.")
        else:
            unit = "readings" if self.forced else "measurements"
            lines.append(f"Smoothing ×{FILTER_COEF[self.filter]}: a sudden change shows 75 % after "
                         f"{FILTER_75[self.filter]} {unit}, about {self.settle_s(interval_ms):.1f} s.")
        p = OVERSAMPLING[self.osrs_p]
        lines.append(f"Pressure resolution {PRESSURE_RES_PA[p]:g} Pa (about {PRESSURE_RES_PA[p] * 8.3:.0f} cm "
                     f"of height), temperature {TEMP_RES_C[OVERSAMPLING[self.osrs_t]]:g} °C.")
        return lines


@dataclass(frozen=True)
class Link:
    channel: int = 108
    rate: int = 2
    crc: int = 2

    @property
    def code(self):
        return self.channel * 100 + self.rate * 10 + self.crc

    @classmethod
    def from_code(cls, code):
        d = _digits(code, 5)
        if d is None:
            return None
        s = cls(d[0] * 100 + d[1] * 10 + d[2], d[3], d[4])
        return s if s.valid() else None

    def valid(self):
        return 0 <= self.channel <= 125 and 0 <= self.rate <= 2 and self.crc in (1, 2)

    def summary(self):
        return f"channel {self.channel} ({2400 + self.channel} MHz), {RATE_NAMES[self.rate]}, {8 * self.crc}-bit CRC"

    def explain(self):
        mhz = 2400 + self.channel
        lines = []
        if mhz > 2483:
            lines.append(f"{mhz} MHz is above 2483.5 MHz: outside the licence-free 2.4 GHz band in most countries.")
        else:
            lines.append(f"{mhz} MHz is inside the 2.4 GHz band, shared with Wi-Fi and Bluetooth.")
        sens = f"the receiver hears down to {SENSITIVITY_DBM[self.rate]} dBm"
        lines.append({2: f"250 kbps: {sens}, the longest range.",
                      0: f"1 Mbps: {sens} (-94 at 250 kbps), so less range.",
                      1: f"2 Mbps: {sens}, the shortest range; it takes 2 MHz, so keep 2 channels clear."}
                     [self.rate])
        lines.append("16-bit CRC catches more damaged packets." if self.crc == 2
                     else "8-bit CRC: one byte less on air, but more damaged packets get through.")
        return lines


@dataclass(frozen=True)
class Retry:
    delay: int = 10
    count: int = 15
    attempts: int = 4

    @property
    def code(self):
        return self.delay * 1000 + self.count * 10 + self.attempts

    @classmethod
    def from_code(cls, code):
        d = _digits(code, 5)
        if d is None:
            return None
        s = cls(d[0] * 10 + d[1], d[2] * 10 + d[3], d[4])
        return s if s.valid() else None

    def valid(self):
        return 0 <= self.delay <= 15 and 0 <= self.count <= 15 and 1 <= self.attempts <= 9

    def delay_us(self, link=None):
        """The wait between tries the radio really uses. The firmware raises it to what an ACK with
        its payload needs: 1500 us at 250 kbps, 500 us at 1 or 2 Mbps (nRF24L01+ datasheet 7.4.2)."""
        d = self.delay
        if link is not None:
            d = max(d, 5 if link.rate == 2 else 1)
        return 250 * (d + 1)

    def give_up_ms(self, link):
        """Longest one reading can take when nothing answers: every try of every send, plus the firmware's
        3-14 ms pause between sends (counted as its 8.5 ms average)."""
        air_us = (8 * (1 + 5 + 32 + link.crc) + 9) * 1000 / RATE_KBPS[link.rate]   # preamble, address, payload, CRC
        per_try_us = 130 + air_us + self.delay_us(link)                            # 130 us transmitter start-up
        return self.attempts * (self.count + 1) * per_try_us / 1000 + (self.attempts - 1) * 8.5

    def summary(self, link=None):
        return (f"{self.delay_us(link)} µs apart, {self.count} retries, "
                f"{self.attempts} send{'s' if self.attempts > 1 else ''} per reading")

    def explain(self, link, interval_ms):
        lines = []
        if self.delay_us(link) != self.delay_us():
            lines.append(f"⚠ At {RATE_NAMES[link.rate]} the base's reply needs at least {self.delay_us(link)} µs "
                         f"between tries; the remote uses that instead of {self.delay_us()} µs.")
        tries = self.attempts * (self.count + 1)
        give_up = self.give_up_ms(link)
        lines.append(f"When the base can't be heard, a reading makes {tries} tries and gives up after "
                     f"about {give_up:.0f} ms.")
        if give_up > interval_ms:
            lines.append(f"⚠ That is longer than a reading every {interval_ms} ms: on a dead link readings "
                         f"come late.")
        return lines


DEFAULT_SENSOR, DEFAULT_LINK, DEFAULT_RETRY = Sensor(), Link(), Retry()
