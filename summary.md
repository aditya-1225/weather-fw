# Sensor and radio controls in the weather GUI: summary (2026-10-16)

## The request

Give the desktop GUI (`host/weather_gui.py`) control over the BMP280's oversampling and smoothing filter, and over the radio. Flashing both boards was understood to be part of it.

## Where things stand

| | State |
|---|---|
| Base board (B800, on USB) | **Flashed with the new firmware** (built with `--pa max`). Settings now: sensor `25410` (the default: temp ×2, pressure ×16, filter ×16, continuous, 62.5 ms rest), interval 1000 ms, PA MAX, link channel 108 / 250 kbps / CRC 16. |
| Remote board (2CF4) | **Not flashed. Still on the old firmware.** Linked over radio, 200 ms interval, PA MAX. It answers the new commands with `UNKNOWN`, as expected. |
| Open bug | One known bug in how the base reads commands (below). The GUI no longer triggers it (follow-up, below); the firmware cause is still unknown. |
| Tests | 101 checks across 6 test files, all passing (no hardware needed). |

## What was checked first

The old firmware set the filter while the BMP280 was measuring, and the datasheet says the chip may ignore filter writes then. A test sketch (`debug/sketches/bmpconfig/`) read the registers back after writing them both ways. This chip kept them both times (`F4=0x57 F5=0x30`, 5 rounds each). So filter ×16 really was active, and the earlier explanation of the 200 ms sampling holds. Result saved in `debug/results/2026-10-16/bmpconfig_B800D914_register_readback.txt`.

## Firmware changes (`firmware/weather_node/weather_node.ino`)

**New commands.** Each works locally, or on the remote with `remote ` in front. Settings travel as decimal codes, and the same codes appear in `STAT` and `LINK` lines:

| Command | Code | Example |
|---|---|---|
| `bmp <TPFSM>` | temp / pressure oversampling 1-5 (×1…×16), filter 0-4 (off…×16), standby 0-7 (0.5…4000 ms), mode 0 continuous / 1 one per reading | `bmp 25410` (default) |
| `link <CCCRK>` | channel 0-125, rate 0 = 1 Mbps / 1 = 2 Mbps / 2 = 250 kbps, CRC 1 = 8-bit / 2 = 16-bit | `link 10822` (default) |
| `retry <DDCCA>` | wait between tries 0-15 (250 µs steps), retries 0-15, sends per reading 1-9 | `retry 10154` (default) |

- **Settings are now saved in EEPROM**: interval, health interval, PA, sensor, retries, link, and the previous link. They survive a restart and a re-upload (the Every's EESAVE fuse is set: `SYSCFG0=0xC9`). Before, everything went back to defaults on restart. One consequence: `flash.sh --pa` now only applies to a board with nothing saved.
- **The sensor settings are read back from the chip** after every change. A mismatch is logged and the command reports `FAILED`. `diag` also prints the registers now.
- **"One per reading" mode** starts a measurement for each reading and waits for the datasheet's maximum measurement time. It doesn't use the library's `takeForcedMeasurement()`, which can return early and never times out.
- **Retry wait minimum:** the firmware raises the wait between tries to what the base's ACK reply needs: 1500 µs at 250 kbps, 500 µs at 1 or 2 Mbps.
- **Link changes are made on both boards together.** The base queues the change. The remote confirms on the old settings, then switches once that confirmation is acknowledged (or after 3 failed sends, in case only the ACKs are lost). The base switches when it sees the confirmation.
- **Self-healing search:** if either board hears nothing for max(15 s, 3 readings + 5 s), it cycles through the link settings it knows: current, previous, defaults. The base stays 1.5 s on each. The remote stays 6 s on each and probes every 300 ms. Whichever settings they meet on become the current ones. If a link change caused the problem, the remote reports it to the base as `FAILED`.
- **Data packet grows from 28 to 32 bytes** to carry the remote's sensor and retry settings. The base still accepts the old 28-byte packets: it prints `-,-` for the missing fields. That's why the new base works with the old remote.
- **STAT** gains `bmp,retry,link,prev_link,searching`. **LINK** gains `repeats,remote_bmp,remote_retry`. New fields go at the end, so older parsers still work.
- **Lines it doesn't understand** are now logged as `LOG,not a command: '…'` before the help text. This was added to chase the open bug.
- Build: 62% flash, 14% RAM, no warnings, both roles.

## GUI changes

- **`host/weather_settings.py`** (new, no Qt): the codes, plus what each setting means in practice. It covers measurement time, how often the chip makes a new value, how long the filter takes to show 75% of a sudden change, resolution, channel frequency (it flags channels above 2483.5 MHz as outside the licence-free band, which includes the current channel 108), receiver sensitivity per rate, and how long a reading takes to give up on a dead link. Numbers are from the BMP280 and nRF24L01+ datasheets.
- **`host/weather_gui.py`**: the controls card became tabs.
  - **Sensor:** oversampling ×2, filter, mode, rest time, and "apply to" (both boards / this board / remote). A live explanation updates as you change the boxes, e.g. "a reading every 200 ms holds about 2.0 new measurements". It shows each board's current settings and warns when settings don't make sense (e.g. the chip is slower than the reading interval).
  - **Radio:** channel, data rate and CRC, applied with a "Move both boards…" button that asks for confirmation first. The button is turned off while the remote has the old firmware. Also transmit power per board (shown with dBm), and the remote's retries with the worst-case give-up time. It shows the current link, the previous link, and a warning while a board is searching.
  - **Timing** and **Boards:** the existing controls, regrouped.
  - **Remote commands are queued.** The base holds only one remote command at a time, and a newer one replaces it, so the GUI sends them one by one.
  - The radio link strip shows "⚠ Searching" and uses the remote's real retry count instead of a fixed 15.
- **`host/weather_ui.py`**: parses the new STAT and LINK fields. The terminal UI has no new controls; new commands can be typed with `:`.

## Tests

| Test | Checks | What it covers |
|---|---|---|
| `debug/tests/test_settings_codes.py` (new) | 22 | Compiles the code functions straight out of the `.ino` and checks that firmware and GUI accept and decode the same codes for every number 0-99999 (2000 sensor, 756 link, 2304 retry). Also checks the GUI's numbers against values worked out by hand. When I broke the GUI decoder on purpose, it failed 4 checks. |
| `debug/tests/test_gui_settings_fake_board.py` (new) | 28 | The Sensor and Radio tabs against a fake base: boxes fill from the board, Apply sends the right codes to the right board, remote commands go one at a time, link changes ask first and go only through the remote, and failures and old remote firmware are explained. |
| `debug/tests/test_gui_fake_board.py` | 16 | Existing. Fixed to click the health Apply button by name: it used "the second Apply button", which now finds a different one. |
| `test_gui_history`, `test_ui_fake_board`, `test_radio_watch_fake_board` | 16, 9, 5 | Existing, unchanged, passing. |

## Checked on the real hardware (base flashed, remote old)

- `bmp`, `retry` and `link` commands work, bad codes give `BAD_ARG`, and the chip's registers match what was set (`bmp 15210` read back `ctrl_meas=0x37 config=0x28`).
- Settings survived a `reset` ("settings loaded from EEPROM").
- **Self-healing:** I moved the base alone to channel 90 on purpose. After 15 s of silence it started searching, found the remote on channel 108 1.5 s later, and the link came back up.
- The new base works with the old remote: readings arrive, and new commands to the remote return `UNKNOWN`. The GUI explains that and turns off "Move both boards".
- The real GUI code, driving the real base, put the base's sensor back to the defaults.

## Open bug

If the base is in **one-per-reading mode** and receives a command switching it back to **continuous**, a `remote …` command sent straight after it (no gap) isn't recognised: the base prints its help text instead. It happened 4 out of 4 times with no gap. With a 1 s gap it works. A local command like `ping` straight after works too, and so do all other mode combinations. The GUI triggers it because Apply sends the local and remote commands back to back. You'd see it when switching the sensor from "One per reading" to "Continuous" with "Both boards" selected: the remote half never confirms.

- The cause isn't found yet. The 64-byte serial receive buffer rules out overflow.
- The base now runs the build that prints `LOG,not a command: '…'`, but its output hasn't been captured yet. That's the next step: rerun `burst4.py` (in the session scratchpad) showing `LOG,not` lines.
- Once one try with a 50 ms gap worked, so a short pause between the two commands in the GUI may be a workaround. The real cause should still be found.

### Follow-up (cloud session, no hardware)

- **GUI no longer sends lines back to back.** `host/weather_gui.py` now gives the base one command line at a time: the next goes only after the base has answered the one before (`CMD,L,…`, or `CMD,R,…,QUEUED` for a remote command) plus 0.2 s, or after 2 s without an answer (a base with older firmware prints its help instead of a `CMD` line). So Apply with "Both boards" sends `bmp …`, waits for the base to finish it, then sends `remote bmp …`. Remote commands still also wait for the previous remote command to finish, as before.
- Tests updated for the new order: `test_gui_settings_fake_board.py` now has 31 checks (new: the remote half waits for this board's answer, and goes anyway once the answer wait runs out); `test_gui_fake_board.py` still 16.
- The firmware cause is **not** found: it needs the board's `LOG,not a command: '…'` output (rerun `burst4.py`), which this session couldn't capture. Reading the code found nothing in the line reader itself (no re-entry, no buffer overflow, `strtok` restarted per line). The README now tells other scripts to wait for each `CMD` answer too.
- Docs done: README (Sensor/Radio tabs, settings codes, EEPROM persistence, link changes and the search, STAT/LINK fields, one-line-at-a-time rule) and the `--pa` note in `debug/flash.sh`.

### Follow-up: one-way link (2026-10-16, after flashing the remote)

- Moving to channel 80 failed cleanly: neither board heard the other there, both searched and met on 108 again after about 23 s. Most likely something strong near 2480 MHz where the boards are.
- On channel 90 the link went up and down for minutes. The link-up times came every 18-21 s, which matches the remote cycling through its 3 link settings (6 s each): the base heard the remote while it was on 90, but the remote didn't hear the base's ACKs there, so it never stopped searching. The base took the remote's packets as "link found" and stayed on 90, so they never met on 108, which works both ways. A `remote link` sent then timed out, as it rides on those ACKs.
- **Fix (firmware, both boards):** the remote marks its packets while it searches (top bit of `sensorBits`). The base doesn't count marked packets as the link found, and after 3 s of them it searches too. When the remote's search ends on an ACK it re-sends the reading unmarked at once, so the base settles on the same settings. No health packets while searching (they can't carry the mark). `LINK` gains `r_searching`; the GUI shows "⚠ One way" and explains it in the Radio tab.
- Compiled for both roles with avr-gcc 14 and the megaAVR core and libraries from GitHub (no warnings); not run on the boards yet. Needs flashing on both.
- Still open: the GUI says "✓ The remote confirmed and switched" even when the change doesn't hold (the base clears the command on the remote's OK, so the later `FAILED` never reaches it).

## Not done yet

- Flashing the remote. The GUI no longer triggers the open bug, so this no longer has to wait for the firmware cause. Steps: close the GUI, plug the remote into USB, then run
  ```
  debug/flash.sh remote --pa max --board 2CF4
  python3 debug/serial_probe.py --board 2CF4 --send status --secs 5
  ```
  Expect `no saved settings, using defaults`, and a STAT line ending in `,25410,10154,10822,10822,0`. Then unplug it, power it as before, and the base should show its settings instead of `-,-`.
- On hardware, with both boards flashed: a real "Move both boards" link change, the remote's search and probes, and the case where a change fails and both go back.
- Leftover from testing: the base's saved "previous link" is channel 90. It only adds one setting to the search list, and the GUI shows it as "Before that: channel 90". There's no command to clear it. It goes away after the next real link change.

## Files

- Changed: `firmware/weather_node/weather_node.ino`, `host/weather_gui.py`, `host/weather_ui.py`, `debug/tests/test_gui_fake_board.py`
- New: `host/weather_settings.py`, `debug/tests/test_settings_codes.py`, `debug/tests/test_gui_settings_fake_board.py`, `debug/sketches/bmpconfig/bmpconfig.ino`, `debug/results/2026-10-16/bmpconfig_B800D914_register_readback.txt`, `summary.md`
