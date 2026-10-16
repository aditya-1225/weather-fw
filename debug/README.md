# Debugging the weather link

Tools, test sketches and recordings from tracking down why the boards wouldn't connect (2026-10-14).
**Short version: the code was never the problem. Both faults were cracked ground/power joints under the
coating.** Start with `diag`. It tells you in one second whether a module is alive, unpowered or disconnected.

| File | What it's for |
|---|---|
| `radio_watch.py` | Live view of the radio turning on/off while you press on it. Finds loose joints. |
| `serial_probe.py` | Send commands to a board and print the replies, without the UI. Picks a board by USB serial. |
| `flash.sh` | Compile and upload from the terminal (base / remote / any sketch), with the retries avrdude needs. |
| `sketches/radiodiag/` | Standalone deep check of the radio and the Nano's pins (no RF24 library). |
| `sketches/radiowatch/` | Standalone version of the press test (before `watch` was built into the firmware). |
| `sketches/rf24_getting_started/` | The RF24 library's own example with CE=9, as an independent "is the radio alive" check. |
| `tests/` | Fake-board tests for `weather_ui.py` and `radio_watch.py`; no hardware needed. |
| `results/2026-10-14/` | Raw recordings referred to below. |

Close `weather_ui.py` and Serial Monitor before using any of these. Only one program can have the port
open, and the tools refuse to start if another one has it.

## Which tool when

| Symptom | Do this |
|---|---|
| Something isn't working | Press `d` in the UI, or run `python3 debug/serial_probe.py --send diag`. Read it with the table below. |
| Radio comes and goes | `python3 debug/radio_watch.py`, then press/tilt/tap the module one spot at a time. |
| Need more detail on the radio | `debug/flash.sh debug/sketches/radiodiag`, then `python3 debug/serial_probe.py --secs 20` |
| Is it my code or the hardware? | `debug/flash.sh debug/sketches/rf24_getting_started` (prints "not responding" if the hardware is at fault) |
| Put the real firmware back | `debug/flash.sh base` or `debug/flash.sh remote --pa max` |

With both boards plugged in, add `--board 2CF4` or `--board B800` (start of the USB serial number);
`python3 debug/serial_probe.py --list` shows them.

## Reading `diag`

```
diag i2c SDA(A4)=H SCL(A5)=H
diag i2c devices: 0x76(id 0x58)
diag radio CE=D9 CSN=D8 MISO(D12) idle=F STATUS=0x0E CONFIG=0x7F RF_CH=0x6C RF_SETUP=0x23
```
`H` means something on the PCB pulls the line up, `F` means floating, `L` means held low.

| What you see | Meaning |
|---|---|
| `STATUS=0x0E`, MISO idle `F` | Radio healthy. `RF_CH=0x6C` is channel 108, so the firmware's settings stuck. |
| All `0xFF`, MISO idle `H` | Radio chip not running, but its pins float up to 3.3 V. **Typical of a broken GND joint.** |
| All `0x00` | MISO stuck low: radio has no 3.3 V, or MISO is shorted to GND. |
| Mixed values (`STATUS=0x00 CONFIG=0xFF…`) | Radio only half-responding: intermittent contact or a failing module. |
| `0x76(id 0x58)` | BMP280 found (0x60 would be a BME280). |
| SDA/SCL `H`, devices `none` | Sensor pull-ups have voltage but the chip doesn't answer: check the BMP280's GND. |
| SDA/SCL `L` | Sensor unpowered (its pull-ups pull to 0 V) or SDA/SCL shorted. |
| SDA/SCL `F` | Nothing connected to A4/A5. |

The radio sketch (`radiodiag`) adds one stronger test. A live nRF24 grabs MISO and pulls it **low** as
soon as CSN goes low. If MISO stays `H` with CSN low, the chip isn't running, whatever the wiring.

## What happened on 2026-10-14

| Board (USB serial) | Role | Fault | Fix |
|---|---|---|---|
| `B800D914…` | base | BMP280 **and** radio silent. Neither answered on any bus. | Cracked BMP280 GND joint. Wire soldered to Arduino GND. |
| `2CF48ABD…` | remote | Radio silent; BMP280 fine. | Intermittent radio power/GND joint. Started working by itself later; may come back. |

How it was narrowed down:

1. **Same code, old code, library example, all failing.** Your original sketches (`~/requires-backup/code/`)
   failed the same way: the old `bmp280.ino` gave `SensorID was: 0x0`. The RF24 `GettingStarted` example
   said "radio hardware is not responding!!". So the new firmware wasn't the cause.
2. **Base board came back after re-plugging.** Same firmware, sensor and radio suddenly fine. That pointed
   to a loose joint, and you found the cracked BMP280 GND. After the fix, 30 s with 31/31 good readings
   (`stability_B800D914_30s_after_gnd_fix.txt`).
3. **Side-by-side `radiodiag`** (`radiodiag_*.txt`). Both boards had identical supply (4.6 V USB, 3.29 V rail),
   and the Nano's D8/D9/D11/D13 all drove correctly. Only the radio differed: on the base its lines float and
   the chip drives MISO. On the remote every radio line read pulled **high**, the chip never drove MISO, and
   register writes failed. That was true with bit-banged SPI too, at every speed. So the PCB has no pull-ups
   and the Nano is fine; the remote's radio had lost power or ground.
4. **3-minute press test** (`press_test_2CF48ABD_remote_3min.txt`). While pressed, the radio answered
   `STATUS=0x0E` continuously for 18.7 s, 17.5 s and 4.2 s, plus short blips. It read the nRF24's **power-on defaults** (`CONFIG=08 EN_AA=3F
   RF_CH=02`), which only appear right after the chip loses power and comes back. It went silent when released.
   **The chip is fine; its VCC or GND contact is mechanically intermittent.**

This also explains the original "power-cycle both boards in a specific way" problem. Those joints connected
or not depending on how the boards sat.

If the remote's radio drops out again: run `radio_watch.py`, find the spot that brings it back, and reflow the
module's GND (pin 1) and VCC (pin 2) joints there. Or solder a wire from the antenna connector's shell to the
Arduino GND. Fit a 10–100 µF capacitor across the radio's VCC/GND while you're at it.

## Notes

- `avrdude: jtagmkII_initialize(): Cannot locate "flash" and "boot" memories in description` appears on every
  Nano Every upload and is harmless. Uploads sometimes hang at `jtagmkII_getsync()`; `flash.sh` retries.
- Uploads need your user in the `dialout` group (`sudo usermod -aG dialout $USER`, then log in again).
- The firmware's `watch` command (used by `radio_watch.py`) streams for 10 s after the last request, then
  stops by itself, so it never leaves the UI flooded.
- `flash.sh` uses the Arduino IDE's own arduino-cli (Flatpak) and libraries; set `ARDUINO_CLI` to use another.
  Builds are cached in `~/.cache/weather-build/`.
- Run the tests with `python3 debug/tests/test_ui_fake_board.py` and
  `python3 debug/tests/test_radio_watch_fake_board.py`.
