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
link, a log with a command line, and buttons for every command. **Timing** sets the sample interval and the
health-report interval on both boards and shows when each board confirms. Both go back to their defaults
(1000 ms, 10 s) when a board restarts. The remote's health interval isn't in any packet, so the window works it
out from the time between its reports.

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
* **Weak-link tolerance.** If the remote hears no ACK, it sends the same reading again, up to 4 times, a few ms
  apart. That gives it more chances to hear the base and any command riding on the ACK; the base drops the
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
   and packets get lost. Default is LOW. Raise it with `a`/`A` when the boards are far apart.
4. Fit the antennas, and keep the modules away from the USB cable and the Every's regulator.

## Serial protocol (115200 baud)

The base's serial is the source of truth. The UI only parses these lines:

```
DATA,L|R,seq,uptime_ms,temp_c,pressure_pa,sensor_ok           L = this board, R = remote (base only)
LINK,up,rx,lost,strong,arc,r_tx_fails,r_radio_inits,r_pa,r_interval_ms,r_reset_cause,repeats   (base)
TX,ok,arc,tx_ok,tx_fails,radio_inits,attempts                                           (remote)
STAT,role,radio_ok,sensor_ok,uptime_ms,radio_inits,pa,interval_ms,reset_cause,health_ms
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

Commands (one per line): `help`, `status`, `diag`, `watch`, `ping`, `reset`, `radio`, `sensor`, `interval <ms>`,
`pa <0-3>`, `health <ms>` (2000-60000). On the base, put `remote ` in front of any of them to run it on the remote board.

Radio settings: channel 108, 250 kbps, 16-bit CRC, auto-ack with ACK payloads, 15 retries 2.75 ms apart,
up to 4 sends per reading.
To move both boards to another channel or address, change `RF_CHANNEL` / `PIPE_ADDR` in the sketch and
re-upload to both.
