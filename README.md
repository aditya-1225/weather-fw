# Weather link: two Nano Everys, two BMP280s, nRF24L01+PA+LNA

```
 REMOTE board  ── reading every 1 s ──▶  BASE board ──USB──▶ computer (weather_ui.py)
               ◀── commands in the ACK ──
```

Both boards measure and print their own BMP280 readings. The remote also sends
its reading by radio. The base prints both and is the board you plug into the computer.

## Upload

1. Arduino IDE → Boards Manager: install **Arduino megaAVR Boards**. Library Manager: install **RF24**
   (by TMRh20) and **Adafruit BMP280 Library** (accept its dependencies).
2. Open `firmware/weather_node/weather_node.ino`. Check `PIN_RF_CE` / `PIN_RF_CSN` against your PCB
   (set to CE=D9, CSN=D8).
3. Board: *Arduino Nano Every*. Registers emulation: *None (ATMEGA4809)*. The sketch refuses to
   build for any other board.
4. Set `#define NODE_ROLE ROLE_BASE`, then upload to the board that stays on the computer.
5. Set `#define NODE_ROLE ROLE_REMOTE`, then upload to the other board.

With arduino-cli you can skip the edits:

```
arduino-cli compile -u -p /dev/ttyACM0 -b arduino:megaavr:nona4809:mode=off --build-property "compiler.cpp.extra_flags=-DNODE_ROLE=ROLE_BASE"   firmware/weather_node
arduino-cli compile -u -p /dev/ttyACM0 -b arduino:megaavr:nona4809:mode=off --build-property "compiler.cpp.extra_flags=-DNODE_ROLE=ROLE_REMOTE" firmware/weather_node
```

## Desktop window

```
pip install pyserial PySide6
python3 host/weather_gui.py     # auto-detects the port; or --port /dev/ttyACM0; --theme dark|light
```

Same serial reader and the same logs as the terminal UI below, in a window: each board's reading with a
history chart (temperature or pressure, last 5 min to 24 hours; hover for values), its health report, the radio
link, a log with a command line, and buttons for every command. The controls are in four tabs:

- **Sensor:** the BMP280's oversampling, smoothing filter, mode (continuous, or one measurement per reading)
  and rest time, applied to both boards, this board or the remote. A live explanation says what the settings
  mean in practice (measurement time, how many new measurements each reading holds, how long the filter takes
  to follow a sudden change, resolution) and warns when they don't make sense, e.g. when the chip is slower
  than the readings.
- **Radio:** channel, data rate and CRC. **Move both boards…** asks first, then moves both boards together
  (see [Changing the radio link](#changing-the-radio-link)); it is off while the remote runs older firmware.
  Also the transmit power per board, and the remote's retries with the longest a reading can take to give up.
  Shows the current and previous link, and a warning while a board is searching.
- **Timing:** the sample interval and the health-report interval on both boards. The remote's health interval
  isn't in any packet, so the window works it out from the time between its reports.
- **Boards:** ping the remote, radio re-init, sensor re-detect, the wiring check and restart, per board.

Each Apply shows when each board confirms. The window sends the base one command line at a time and waits for
its answer before the next (see [Serial protocol](#serial-protocol-115200-baud)), and remote commands one at a
time, because the base holds only one remote command and a newer one replaces it. All settings are kept in
each board's EEPROM, so they survive a restart.

On start the charts are filled with the last 24 hours from the earlier `host/logs/weather_*.csv` files
(this window's and the terminal UI's), once the window knows which board is plugged in. A break in the line
means no readings: the window was closed, or the sensor failed.

## Terminal UI

```
pip install pyserial            # Windows also: pip install windows-curses
python3 host/weather_ui.py      # auto-detects the port; or --port /dev/ttyACM0
```

| key | action |
|---|---|
| `p` | ping remote |
| `r` / `R` | soft-reset remote / this board (asks first) |
| `n` / `N` | re-initialise radio on remote / this board |
| `s` / `S` | re-detect BMP280 on remote / this board |
| `a` / `A` | step PA level (MIN→LOW→HIGH→MAX) on remote / this board |
| `d` | wiring check on this board (see below) |
| `i` | sample interval for both boards |
| `:` | type any firmware command (`help`, `status`, `remote ping`, …) |
| `q` | quit |

The **HEALTH** panel under each board's readings updates every 10 s (change it with `health <ms>`, or in the desktop window). It shows the 5 V and 3.3 V rails, the MCU
temperature, free RAM and longest loop time, plus how often the radio lost power, stopped answering, or the
sensor failed. Non-zero failure counts show in red. `strong` is the share of radio replies heard above -64 dBm;
`--` means none were heard since the last report.

Readings are logged to `host/logs/weather_<time>.csv`, health reports to `host/logs/health_<time>.csv`. Every raw serial line is logged to
`host/logs/serial_<time>.log`. Unplugging the cable or resetting the board does not stop the UI. It
reconnects on its own. Opening the port normally leaves the board running. On 2026-10-14 the base did
reboot on every connect for a while (reset cause `UPLOAD`, cause not found); if that happens, its link
counters restart from zero and a remote command still waiting is dropped. The UI asks for `status` when it
connects, so it shows the right state either way.

The UI works with either board plugged in. On the remote it shows the remote's own readings and
whether its packets are being acknowledged.

## Why the link used to need a power-cycle ritual, and what the code does now

The nRF24 **keeps its settings when the Arduino resets** and **loses them when its own supply dips**,
and the Arduino doesn't notice either. Two ways this leaves the boards in a state that only a full
power cycle fixes:

* The Arduino resets, but the radio keeps half-configured state from before.
* The PA+LNA module pulls about 115 mA while transmitting. That is more than the Nano Every's 3.3 V pin is
  meant to supply. The radio browns out, comes back on default settings (channel 2, 2 Mbps), and from then on it
  can't hear the other board.

What the firmware does about it:

* **Register read-back every 2 s** on both boards: channel, data rate, CRC and chip presence. If any of
  them doesn't match, the board re-initialises the radio.
* **Weak-link tolerance.** If the remote hears no ACK, it sends the same reading again, up to 4 times by
  default (`retry` changes it), a few ms apart. That gives it more chances to hear the base and any command riding on the ACK; the base drops the
  repeats. The remote only re-initialises its radio after 30 readings in a row get no ACK at all.
* The **base** re-initialises its radio every 5 s while it hears nothing, so it doesn't matter which
  board is switched on first.
* **Commands are retried until confirmed.** Each command has a sequence number. The base repeats it in
  every ACK until the remote reports that number as done, or it times out (≥30 s). The remote ignores
  repeats, so a command never runs twice. The remote's "last command run" survives its own reset, so a
  `reset` command still gets confirmed.
* **BMP280** at 0x76 or 0x77 (BME280 is accepted too). Readings are range-checked. After 3 bad readings
  the sensor is re-detected every 5 s.
* **Watchdog** (4 s): if the loop hangs, the board resets itself. This includes a stuck I2C bus, because
  the Every's Wire library has no timeout.
* Each board reports its **reset cause** (POWER / RESET_PIN / BROWNOUT / WATCHDOG / SOFT / UPLOAD). If you see
  `BROWNOUT`, the supply is too weak.

### Wiring check (`d` in the UI, or `diag`)

Runs on its own at boot if the radio or the sensor doesn't start, and on demand. It measures the bus lines
directly instead of asking the chips, so it can tell a dead supply from a bad wire:

```
LOG,diag i2c SDA(A4)=H SCL(A5)=H   H = pulled up from outside, F = floating, L = held low
LOG,diag i2c devices: 0x76(id 0x58)
LOG,diag radio CE=D9 CSN=D8 MISO(D12) idle=F STATUS=0x0E CONFIG=0x0E RF_CH=0x6C RF_SETUP=0x23
```

A healthy nRF24 answers `STATUS=0x0E` and leaves MISO floating (`F`) while CSN is high. All `0xFF` with
MISO `H` usually means the radio has 3.3 V but no GND. All `0x00` means MISO is stuck low, usually because the radio has no
3.3 V. All `0xFF` means nothing is answering at all. Two `L` or `F` readings on the I2C lines mean the BMP280
module has no power or isn't connected. When both the radio and the sensor fail at once, check the shared 3.3 V
and GND first.

### Finding a loose radio joint (`debug/radio_watch.py`)

```
python3 debug/radio_watch.py            # close weather_ui.py first; Ctrl+C to stop
```

The board checks the radio 100 times a second while you press, tilt or tap the module. Each change
prints a timestamped line, with a beep when the radio turns on:

```
20:38:59.821  ON       STATUS=0x0E  (was OFF for 1.01 s)
20:39:01.332  FLICKER  STATUS=0xFF  (was ON for 1.51 s)
OFF     STATUS=0xFF  [··········███████████████▒▒▒▒▒·····]  alive  49% (10 s)  ON events 1  longest ON 1.5 s
```

`ON` means every sample answered, `FLICKER` means some did, `OFF` means none did. If the radio turns on only
while you press one spot, the joint under that spot is cracked. The board leaves watch mode by itself 10 s
after the script stops. `--quiet` turns off the beep.

### Hardware checklist (software can't fix these)

1. **Capacitor across the radio's VCC/GND:** 10–100 µF electrolytic, as close to the module pins as
   possible. This is the most common fix.
2. **Separate 3.3 V regulator** (AMS1117-3.3 or similar, fed from 5 V) for the PA+LNA module, not the
   Every's 3V3 pin.
3. **Boards close together (< ~5 m):** use PA `MIN` or `LOW`. At HIGH/MAX the receiver's LNA saturates
   and packets get lost. Default is LOW. Raise it with `a`/`A` when the boards are far apart. The level is
   saved on the board.
4. Fit the antennas, and keep the modules away from the USB cable and the Every's regulator.

## Serial protocol (115200 baud)

The base's serial is the source of truth. The UI only parses these lines:

```
DATA,L|R,seq,uptime_ms,temp_c,pressure_pa,sensor_ok           L = this board, R = remote (base only)
LINK,up,rx,lost,strong,arc,r_tx_fails,r_radio_inits,r_pa,r_interval_ms,r_reset_cause,repeats,
     r_bmp,r_retry                                                                       (base)
TX,ok,arc,tx_ok,tx_fails,radio_inits,attempts                                           (remote)
STAT,role,radio_ok,sensor_ok,uptime_ms,radio_inits,pa,interval_ms,reset_cause,health_ms,
     bmp,retry,link,prev_link,searching
CMD,L|R,seq,name,QUEUED|OK|BAD_ARG|FAILED|UNKNOWN|TIMEOUT|REPLACED
LOG,text
W,valid,samples,last_status_hex                                     (watch mode, every 100 ms)
HEALTH,L|R,seq,uptime_ms,vdd_mv,rail_3v3_mv,mcu_temp_c,radio_power_losses,radio_unreachable,
       sensor_fails,readings,sends,strong_pct,max_loop_ms,free_ram          (every health_ms, default 10 s)
```

`HEALTH` is for the logs; the UI writes it to `host/logs/health_<time>.csv`. Each board prints its own
(`L`); the remote also sends its report by radio, and the base prints that as `R`.

- `vdd_mv` is the MCU's supply, measured against its internal 1.1 V reference (a few % accuracy). On USB
  it reads ~4.6 V because of the Every's input diode.
- `rail_3v3_mv` is the 3.3 V rail measured on the BMP280's I2C pull-ups. No pin can measure the radio's
  own supply, so `radio_power_losses` is the radio's health signal: the count of 2-second checks that
  found the radio back at its power-on defaults, meaning it lost power. `radio_unreachable` counts checks
  where it didn't answer at all. `sensor_fails` counts times the BMP280 stopped answering. All three are
  totals since boot.
- `readings`, `sends`, `strong_pct`, `max_loop_ms` cover the time since the previous report (`readings` stops at 255). Remote: readings sent, radio sends
  including re-sends, and % of ACKs heard above -64 dBm. Base: remote readings received, repeat copies
  received, and % of them above -64 dBm.

The settings fields at the end of `STAT` and `LINK` use the same codes as the commands below:

- `STAT`: `bmp`, `retry` and `link` are this board's settings, `prev_link` the link before the last change
  (tried while searching), `searching` 1 while the board is looking for the other one.
- `LINK`: `r_bmp`, `r_retry` are the remote's sensor and retry settings. A remote with firmware from before
  these commands sends a shorter packet, and the base prints `-,-` for them.

New fields always go at the end of a line, so older parsers keep working.

Commands (one per line): `help`, `status`, `diag`, `watch`, `ping`, `reset`, `radio`, `sensor`, `interval <ms>`,
`pa <0-3>`, `health <ms>` (2000-60000), and the settings below. On the base, put `remote ` in front of any of
them from `ping` on to run it on the remote board. Each command answers with a `CMD` line. Send the next line
only after that answer: a line sent straight after another could arrive garbled (seen once, as a
`remote bmp …` sent straight after a `bmp` that moved the base from one per reading to continuous). A line the
board doesn't understand is shown as `LOG,not a command: '…'`, with control bytes as `\xNN`, followed by the
help text.

### Settings codes

Each setting is one decimal number, one digit (or group of digits) per part:

| Command | Parts | Default |
|---|---|---|
| `bmp <TPFSM>` | **T**, **P** temperature / pressure oversampling 1-5 (×1 ×2 ×4 ×8 ×16); **F** filter 0-4 (off ×2 ×4 ×8 ×16); **S** rest time between measurements 0-7 (0.5 62.5 125 250 500 1000 2000 4000 ms); **M** mode 0 = continuous, 1 = one measurement per reading | `25410`: temp ×2, pressure ×16, filter ×16, 62.5 ms, continuous |
| `link <CCCRK>` | **CCC** channel 0-125 (2400 + channel MHz); **R** rate 0 = 1 Mbps, 1 = 2 Mbps, 2 = 250 kbps; **K** CRC 1 = 8-bit, 2 = 16-bit | `10822`: channel 108, 250 kbps, 16-bit CRC |
| `retry <DDCCA>` | **DD** wait between tries 0-15 (250 µs steps, 0 = 250 µs); **CC** retries 0-15; **A** sends per reading 1-9 | `10154`: 2.75 ms, 15 retries, 4 sends |

- `bmp` writes the BMP280 and reads its registers back. If they don't match it logs both and answers
  `FAILED`. `diag` prints the registers too. In one-per-reading mode the board starts a measurement for each
  reading and waits the datasheet's longest measurement time (up to 76 ms at ×16 / ×16).
- `retry`: the wait between tries is raised to what the base's ACK needs, 1500 µs at 250 kbps and 500 µs at
  1 or 2 Mbps.
- Channels above 83 (2483.5 MHz) are outside the licence-free 2.4 GHz band in most countries. The default,
  108, is one of them.
- An unknown or out-of-range code answers `BAD_ARG`.

### Saved settings

The interval, health interval, PA level, sensor, retry and link settings, and the previous link, are kept
in EEPROM. They survive a restart and a re-upload (the Every's EESAVE fuse is set). The boot log says
`settings loaded from EEPROM` or `no saved settings, using defaults`. So the build-time defaults, including
`flash.sh --pa`, only apply to a board that has nothing saved; use `pa <0-3>` to change a board that has.

### Changing the radio link

Use `remote link <code>` on the base (or **Move both boards…** in the window), not `link` on one board:
the boards have to move together. The base passes the change to the remote, the remote confirms on the old
settings and switches once that confirmation is acknowledged (or after 3 failed sends, in case only the ACKs
are lost), and the base switches when it sees the confirmation. `link` typed on one board moves only that
board, which then can't hear the other.

If either board hears nothing for max(15 s, 3 readings + 5 s), it searches: it cycles through the link
settings it knows, the current ones, the previous ones and the defaults. The base stays 1.5 s on each; the
remote stays 6 s on each and sends a probe every 300 ms. The settings they meet on become the current ones.
If a link change caused it, the remote reports that command to the base as `FAILED`. The pipe address
(`PIPE_ADDR`) can only be changed in the sketch, on both boards.

Other radio settings: auto-ack with ACK payloads.
