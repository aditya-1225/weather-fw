#!/bin/bash
# Build and upload to a Nano Every from the terminal, with the retries that
# today's uploads needed (avrdude sometimes hangs at "jtagmkII_getsync()").
#
#   debug/flash.sh base                       # weather firmware, BASE role
#   debug/flash.sh remote --pa max            # REMOTE role, starting at PA MAX (see below)
#   debug/flash.sh debug/sketches/radiodiag   # any sketch folder
#   ... --board 2CF4 | --port /dev/ttyACM1    # pick a board (default: the only one plugged in)
#   ... --no-upload                           # compile only
#
# --pa only applies to a board with no saved settings: the firmware keeps its
# settings (PA included) in EEPROM, and a re-upload doesn't erase them. Use
# `pa <0-3>` (or `remote pa <0-3>`) to change a board that has some.
#
# Close weather_ui.py / Serial Monitor first: only one program can have the port.
set -u
HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(dirname "$HERE")
FLATPAK_CLI=/var/lib/flatpak/app/cc.arduino.IDE2/current/active/files/arduino-ide/resources/app/lib/backend/resources/arduino-cli
CLI=${ARDUINO_CLI:-$( [ -x "$FLATPAK_CLI" ] && echo "$FLATPAK_CLI" || command -v arduino-cli )}
FQBN=arduino:megaavr:nona4809:mode=off

usage() { sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'; exit 1; }
[ $# -ge 1 ] || usage
[ -n "$CLI" ] || { echo "arduino-cli not found (set ARDUINO_CLI=/path/to/arduino-cli)"; exit 1; }

TARGET=$1; shift
PA=""; BOARD=""; PORT=""; UPLOAD=1
while [ $# -gt 0 ]; do
  case $1 in
    --pa) PA=$2; shift 2 ;;
    --board) BOARD=$2; shift 2 ;;
    --port) PORT=$2; shift 2 ;;
    --no-upload) UPLOAD=0; shift ;;
    *) usage ;;
  esac
done

FLAGS=""
case $TARGET in
  base|remote)
    SKETCH=$ROOT/firmware/weather_node
    ROLE=$(echo "$TARGET" | tr a-z A-Z)
    FLAGS="-DNODE_ROLE=ROLE_$ROLE"
    case ${PA,,} in
      "") ;;
      min|low|high|max) FLAGS="$FLAGS -DSTART_PA=RF24_PA_${PA^^}" ;;
      *) echo "--pa must be min, low, high or max"; exit 1 ;;
    esac
    NAME=weather_${TARGET}${PA:+_pa_${PA,,}} ;;
  *)
    SKETCH=$(cd "$TARGET" 2>/dev/null && pwd) || { echo "no such sketch folder: $TARGET"; exit 1; }
    [ -z "$PA" ] || { echo "--pa only applies to base/remote"; exit 1; }
    NAME=$(basename "$SKETCH") ;;
esac
BUILD=${XDG_CACHE_HOME:-$HOME/.cache}/weather-build/$NAME

echo "== compiling $SKETCH ${FLAGS:+($FLAGS)}"
"$CLI" compile --warnings all -b "$FQBN" --build-path "$BUILD" \
  ${FLAGS:+--build-property "compiler.cpp.extra_flags=$FLAGS"} "$SKETCH" 2>&1 \
  | grep -E 'warning|error|Sketch uses|Global variables' || true
[ "${PIPESTATUS[0]}" = 0 ] || { echo "compile failed"; exit 1; }
[ $UPLOAD = 1 ] || exit 0

if [ -z "$PORT" ]; then
  PORTS=()
  for d in /dev/ttyACM* /dev/ttyUSB*; do
    [ -e "$d" ] || continue
    SER=$(udevadm info -q property "$d" 2>/dev/null | sed -n 's/^ID_SERIAL_SHORT=//p')
    [ -z "$BOARD" ] || [[ ${SER^^} == ${BOARD^^}* ]] && PORTS+=("$d")
  done
  [ ${#PORTS[@]} = 1 ] || { echo "found ${#PORTS[@]} matching boards; use --board <serial start> or --port"; exit 1; }
  PORT=${PORTS[0]}
fi
for f in /proc/[0-9]*/fd/*; do
  [ "$(readlink "$f" 2>/dev/null)" = "$PORT" ] || continue
  PID=$(echo "$f" | cut -d/ -f3)
  echo "$PORT is open in another program: $(tr '\0' ' ' < /proc/$PID/cmdline). Close it first."; exit 1
done

for TRY in 1 2 3; do
  echo "== uploading to $PORT (try $TRY)"
  timeout 60 "$CLI" upload -b "$FQBN" -p "$PORT" --input-dir "$BUILD" "$SKETCH" 2>&1 | grep -vE '^\s*$'
  [ "${PIPESTATUS[0]}" = 0 ] && { echo "== done (the jtagmkII 'Cannot locate flash and boot' line is harmless)"; exit 0; }
  # a hung avrdude outlives the timeout; stop only the one using this port
  for P in $(pgrep -x avrdude); do grep -qa "$PORT" /proc/$P/cmdline 2>/dev/null && kill "$P"; done
  sleep 3
done
echo "upload failed 3 times; unplug the board, plug it back in and run again"
exit 1
