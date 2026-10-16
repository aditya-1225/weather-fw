/*
  weather_node.ino - Arduino Nano Every + BMP280 + nRF24L01+PA+LNA

  One sketch for both boards. Set NODE_ROLE below before uploading:
    ROLE_BASE   - the board plugged into the computer. Listens for the remote,
                  logs its own BMP280 and the remote's readings over USB serial,
                  and forwards commands to the remote.
    ROLE_REMOTE - sends its BMP280 reading to the base every interval and logs
                  its own reading to USB serial (if something is listening).

  Commands travel back to the remote inside the radio's ACK packets, so the
  remote never has to switch into receive mode and the base never has to
  transmit on its own. A command is repeated until the remote confirms it by
  sequence number, so a lost packet only delays it.

  Settings (sensor oversampling and filter, radio channel / data rate / CRC,
  retries, power, intervals) can be changed at runtime and are kept in EEPROM.
  A channel / rate / CRC change is made on both boards together: the base
  queues it, the remote confirms on the old settings and then switches, and the
  base switches when it sees the confirmation. If they can't hear each other
  afterwards, both search the settings they know (current, previous, defaults)
  until they meet again.

  Self-recovery (why "power cycle both in the right order" is no longer needed):
    * The nRF24 keeps its settings across an Arduino reset, and loses them on
      a brown-out while the Arduino keeps running. Both boards read the radio
      registers back every 2 s and fully re-initialise it if they don't match.
    * The remote re-initialises its radio after 5 failed sends in a row.
    * The base re-initialises its radio every 5 s while it hears nothing.
    * The BMP280 is re-detected (0x76 and 0x77) if it stops answering.
    * A 4 s hardware watchdog resets the board if the loop ever hangs
      (including a stuck I2C bus - the Every's Wire library has no timeout).

  Board: Tools > Board > Arduino megaAVR Boards > Arduino Nano Every,
         Tools > Registers emulation > None (ATMEGA4809).

  Libraries (Arduino IDE: Tools > Manage Libraries):
    RF24 by TMRh20 (1.4.x or newer)
    Adafruit BMP280 Library (pulls in Adafruit Unified Sensor + BusIO)

  Serial: 115200 baud, one CSV-style line per event, see README.md.
*/

#include <SPI.h>
#include <Wire.h>
#include <EEPROM.h>
#include <avr/wdt.h>
#include <RF24.h>
#include <Adafruit_BMP280.h>

// ---------------------------------------------------------------- settings --
#define ROLE_BASE   1
#define ROLE_REMOTE 2

#ifndef NODE_ROLE
#define NODE_ROLE ROLE_BASE      // <-- ROLE_BASE or ROLE_REMOTE
#endif

#if !defined(__AVR_ATmega4809__)
#error "This sketch is for the Arduino Nano Every (Tools > Board > Arduino Nano Every)"
#endif

#if NODE_ROLE != ROLE_BASE && NODE_ROLE != ROLE_REMOTE
#error "NODE_ROLE must be ROLE_BASE or ROLE_REMOTE"
#endif
#define IS_BASE (NODE_ROLE == ROLE_BASE)

const uint8_t PIN_RF_CE  = 9;    // match your PCB
const uint8_t PIN_RF_CSN = 8;

const uint8_t PIPE_ADDR[6] = "WXL01";             // both boards must agree; not changeable at runtime

// Everything below can be changed at runtime and is saved in EEPROM, so it
// survives a restart and a re-upload (the Every's EESAVE fuse keeps EEPROM
// through uploads). These are the values for a board with nothing saved.
struct SensorCfg { uint8_t osrsT, osrsP, filter, standby, forced; };   // BMP280 register codes; forced 0/1
struct LinkCfg   { uint8_t channel, rate, crc; };                       // RF24 enums; both boards must match
struct RetryCfg  { uint8_t delay, count, attempts; };                   // nRF24 ARD / ARC codes; only the remote sends

const SensorCfg DEFAULT_SENSOR = {2, 5, 4, 1, 0};   // temp x2, pressure x16, filter x16, 62.5 ms standby, continuous
const LinkCfg   DEFAULT_LINK   = {108, RF24_250KBPS, RF24_CRC_16};   // 2508 MHz, slowest = longest range
const RetryCfg  DEFAULT_RETRY  = {10, 15, 4};       // 2.75 ms apart, 15 retries, 4 sends per reading
#ifndef START_PA
#define START_PA RF24_PA_LOW                       // HIGH/MAX can deafen a receiver that's only a few metres away
#endif
const uint8_t  DEFAULT_PA          = START_PA;     // build with -DSTART_PA=RF24_PA_MAX to change, or `pa <0-3>` at runtime
const uint16_t DEFAULT_INTERVAL_MS = 1000;
const uint16_t MIN_INTERVAL_MS     = 200;
const uint16_t MAX_INTERVAL_MS     = 60000;

// Finding each other again when a link change didn't work, or one board lost
// its saved settings: after hearing nothing for a while a board cycles through
// the link settings it knows (current, previous, defaults). The remote stays on
// each for 6 s and probes every 300 ms; the base stays on each for 1.5 s, so it
// goes through all three while the remote sits on any one of them.
const uint32_t SEARCH_MIN_MS      = 15000;
const uint32_t BASE_DWELL_MS      = 1500;
const uint32_t REMOTE_DWELL_MS    = 6000;
const uint32_t PROBE_MS           = 300;
const uint8_t  SWITCH_AFTER_FAILS = 3;     // remote: switch anyway if the base never acknowledges the confirmation

// Readings in a row with no ACK at all before the remote re-initialises its radio.
// Kept high: on a weak link missed ACKs are normal, and the 2 s register check
// already catches a radio that really lost its settings.
const uint8_t  REMOTE_FAILS_BEFORE_REINIT = 30;
const uint32_t RADIO_CHECK_MS        = 2000;   // register read-back
const uint32_t RADIO_RETRY_MS        = 3000;   // retry when radio didn't start
const uint32_t BASE_RECOVER_MS       = 5000;   // re-init while link is down
const uint32_t SENSOR_RETRY_MS       = 5000;
const uint32_t STATUS_MS             = 5000;
const uint16_t DEFAULT_HEALTH_MS     = 10000;  // supply / radio / MCU health report; `health <ms>` changes it
const uint16_t MIN_HEALTH_MS         = 2000;
const uint16_t MAX_HEALTH_MS         = 60000;

// ---------------------------------------------------------------- protocol --
const uint8_t PROTO_VER = 1;
enum : uint8_t { PKT_DATA = 0xD1, PKT_HEALTH = 0xD3, PKT_ACK = 0xA1 };

enum : uint8_t {
  CMD_NONE = 0, CMD_PING, CMD_RESET, CMD_RADIO_REINIT, CMD_SENSOR_REINIT,
  CMD_SET_INTERVAL, CMD_SET_PA, CMD_SET_HEALTH, CMD_SET_SENSOR, CMD_SET_LINK, CMD_SET_RETRY,
  CMD_COUNT   // new ones go last: an older remote answers UNKNOWN
};
enum : uint8_t { RES_NONE = 0, RES_OK, RES_BAD_ARG, RES_FAILED, RES_UNKNOWN };

struct __attribute__((packed)) DataPacket {   // remote -> base, 32 bytes (28 before the settings fields)
  uint8_t  type;
  uint8_t  ver;
  uint16_t seq;
  uint32_t uptimeMs;
  float    tempC;
  float    pressPa;
  uint8_t  sensorOk;
  uint8_t  lastArc;        // retries the previous packet needed (15 = failed)
  uint16_t txFails;
  uint16_t radioInits;
  uint8_t  resetCause;     // RSTCTRL.RSTFR bits at boot
  uint8_t  paLevel;
  uint16_t intervalMs;
  uint8_t  cmdSeqDone;     // last command executed
  uint8_t  cmdResult;
  uint16_t sensorBits;     // SensorCfg: osrsT | osrsP << 3 | filter << 6 | standby << 9 | forced << 12
  uint8_t  retryBits;      // RetryCfg: delay << 4 | count
  uint8_t  attempts;
};
const uint8_t DATA_V1_LEN = 28;   // a remote with the older firmware sends no settings fields
// Top bit of DataPacket.sensorBits: the remote is searching, so it isn't hearing the base's ACKs.
const uint16_t SENSOR_BITS_SEARCHING = 0x8000;

struct __attribute__((packed)) AckPacket {    // base -> remote, inside ACK
  uint8_t type;
  uint8_t ver;
  uint8_t cmdSeq;
  uint8_t cmd;
  int32_t arg;
};

// Every healthMs, for the logs. The same numbers are printed locally as
// HEALTH,L; the base prints the remote's as HEALTH,R.
struct __attribute__((packed)) HealthPacket {  // remote -> base, 28 bytes
  uint8_t  type;
  uint8_t  ver;
  uint16_t seq;
  uint32_t uptimeMs;
  uint16_t vddMv;           // MCU supply: the 5 V rail after the Every's input diode
  uint16_t railMv;          // 3.3 V rail, measured on the BMP280's I2C pull-ups
  int16_t  mcuTempDeci;     // MCU die temperature, 0.1 degC
  uint16_t radioPowerLoss;  // times the radio was found back at power-on defaults
  uint16_t radioUnreach;    // times the radio stopped answering on SPI
  uint16_t sensorFails;     // times the BMP280 stopped answering
  uint16_t sends;           // remote: radio sends incl. re-sends; base: repeat copies received
  uint8_t  readings;        // remote: readings sent; base: remote readings received (stops at 255)
  uint8_t  strongPct;       // remote: ACKs heard above -64 dBm; base: packets received above -64 dBm
  uint16_t maxLoopMs;       // longest loop() pass
  uint16_t freeRam;
};

static_assert(sizeof(DataPacket) <= 32, "DataPacket too big for nRF24");
static_assert(sizeof(HealthPacket) <= 32, "HealthPacket too big for nRF24");
static_assert(sizeof(AckPacket) <= 32, "AckPacket too big for nRF24");

// The settings kept in EEPROM (address 0).
const uint16_t SAVED_MAGIC = 0x5E71;
struct __attribute__((packed)) Saved {
  uint16_t  magic;
  uint16_t  intervalMs, healthMs;
  uint8_t   paLevel;
  SensorCfg sensor;
  RetryCfg  retry;
  LinkCfg   link, prevLink;
  uint8_t   check;
};

// ----------------------------------------------------------------- globals --
RF24 radio(PIN_RF_CE, PIN_RF_CSN);
Adafruit_BMP280 bmp;

// Survives software and watchdog resets (not power loss), so the remote
// still knows which command it last ran after a "reset" command.
const uint16_t PERSIST_MAGIC = 0xBEE5;
struct Persist { uint16_t magic; uint8_t lastCmdSeq; uint8_t lastCmdResult; };
Persist persist __attribute__((section(".noinit")));
uint8_t resetFlags = 0;

uint8_t   paLevel    = DEFAULT_PA;
uint16_t  intervalMs = DEFAULT_INTERVAL_MS;
uint16_t  healthMs   = DEFAULT_HEALTH_MS;
SensorCfg sensorCfg  = DEFAULT_SENSOR;
RetryCfg  retryCfg   = DEFAULT_RETRY;
LinkCfg   linkCfg    = DEFAULT_LINK;     // the agreed settings
LinkCfg   prevLink   = DEFAULT_LINK;     // the ones before the last change, for the search
LinkCfg   radioLink  = DEFAULT_LINK;     // what the radio is set to: linkCfg, or a candidate while searching
bool      searching = false;
uint8_t   searchIdx = 0;
uint32_t  searchStepMs = 0, quietSinceMs = 0;

bool     radioOk = false;
uint16_t radioInits = 0;
uint32_t lastRadioCheckMs = 0, lastRadioRetryMs = 0;

bool     sensorOk = false;
uint8_t  sensorAddr = 0, sensorBadReads = 0;
uint32_t lastSensorRetryMs = 0;
float    tempC = NAN, pressPa = NAN;

uint16_t sampleSeq = 0;
uint32_t lastSampleMs = 0, lastStatusMs = 0, lastHealthMs = 0;

// Health counters. Cumulative since boot: radioPowerLoss, radioUnreach, sensorFails.
// Per healthMs interval: the h* ones and maxLoopMs. 16-bit so a 60 s interval
// at 200 ms sampling (300 readings) doesn't wrap.
uint16_t healthSeq = 0, radioPowerLoss = 0, radioUnreach = 0, sensorFails = 0;
uint16_t hSends = 0, maxLoopMs = 0;
uint16_t hReadings = 0, hStrong = 0, hStrongOf = 0;

char     lineBuf[48];
uint8_t  lineLen = 0;

#if IS_BASE
struct Pending { bool active; uint8_t seq; uint8_t cmd; int32_t arg; uint32_t sinceMs; };
Pending  pending = {false, 0, 0, 0, 0};
uint8_t  lastIssuedSeq = 0;
bool     heardRemote = false, linkUp = false;
uint32_t lastRxMs = 0, lastRecoverMs = 0, rxCount = 0, lostCount = 0, badPackets = 0, repeatCount = 0;
uint16_t recoverAttempts = 0;
DataPacket lastRemote;
bool     switchAfterRx = false;          // the remote confirmed a link change: follow it once the packet is handled
uint32_t deafSinceMs = 0, deafLastMs = 0; // hearing a remote that says it can't hear us, since / last time
bool     searchNow = false;              // ...for long enough: search without waiting for the quiet time
const uint32_t REMOTE_DEAF_MS = 3000;
LinkCfg  switchTo;
#else
uint32_t txOk = 0;
uint16_t txFails = 0;
uint8_t  consecutiveFails = 0, lastArc = 0;
DataPacket lastSent;                     // re-sent as the probe while searching
bool     haveSent = false;
bool     switchPending = false;          // accepted a link change; switch once the base has our OK
LinkCfg  switchNext;
uint8_t  switchFails = 0;
uint8_t  switchSeq = 0;                  // the base's command number for it (0 = typed here)
uint8_t  linkCmdSeq = 0;                 // command that switched the link, until an ACK proves it works
uint32_t lastProbeMs = 0;
bool     foundPending = false;           // a search ended on an ACK: tell the base (announceFound)
#endif

// ----------------------------------------------------------------- helpers --
const char *cmdName(uint8_t cmd) {
  switch (cmd) {
    case CMD_PING:          return "ping";
    case CMD_RESET:         return "reset";
    case CMD_RADIO_REINIT:  return "radio";
    case CMD_SENSOR_REINIT: return "sensor";
    case CMD_SET_INTERVAL:  return "interval";
    case CMD_SET_PA:        return "pa";
    case CMD_SET_HEALTH:    return "health";
    case CMD_SET_SENSOR:    return "bmp";
    case CMD_SET_LINK:      return "link";
    case CMD_SET_RETRY:     return "retry";
    default:                return "?";
  }
}

uint8_t cmdFromName(const char *s) {
  for (uint8_t c = CMD_PING; c < CMD_COUNT; c++)
    if (strcasecmp(s, cmdName(c)) == 0) return c;
  return CMD_NONE;
}

const __FlashStringHelper *resultName(uint8_t r) {
  switch (r) {
    case RES_OK:      return F("OK");
    case RES_BAD_ARG: return F("BAD_ARG");
    case RES_FAILED:  return F("FAILED");
    case RES_UNKNOWN: return F("UNKNOWN");
    default:          return F("NONE");
  }
}

void printFloat(float v, uint8_t digits) {
  if (isnan(v)) Serial.print(F("nan"));
  else Serial.print(v, digits);
}

void printResetCause(uint8_t f) {
  if (f == 0)                   { Serial.print(F("UNKNOWN")); return; }
  if (f & RSTCTRL_PORF_bm)      Serial.print(F("POWER "));
  if (f & RSTCTRL_EXTRF_bm)     Serial.print(F("RESET_PIN "));
  if (f & RSTCTRL_BORF_bm)      Serial.print(F("BROWNOUT "));
  if (f & RSTCTRL_WDRF_bm)      Serial.print(F("WATCHDOG "));
  if (f & RSTCTRL_SWRF_bm)      Serial.print(F("SOFT "));
  if (f & RSTCTRL_UPDIRF_bm)    Serial.print(F("UPLOAD "));
}

void logMsg(const __FlashStringHelper *msg) {
  Serial.print(F("LOG,"));
  Serial.println(msg);
}

void reportCmd(char where, uint8_t seq, uint8_t cmd, const __FlashStringHelper *result) {
  Serial.print(F("CMD,")); Serial.print(where); Serial.print(',');
  Serial.print(seq);       Serial.print(',');
  Serial.print(cmdName(cmd)); Serial.print(',');
  Serial.println(result);
}

void softReset() {
  Serial.flush();
  radio.powerDown();
  _PROTECTED_WRITE(RSTCTRL.SWRR, RSTCTRL_SWRE_bm);
  for (;;) {}
}

// Time from `since` to `now`, or 0 if `since` is the later one. loop() reads
// millis() once into `now`, but timestamps set while handling a packet or a
// command read millis() again, so they can be a few ms ahead; a plain
// `now - since` would then wrap to ~49 days and look like a timeout.
uint32_t elapsed(uint32_t since, uint32_t now) {
  int32_t d = (int32_t)(now - since);
  return d > 0 ? (uint32_t)d : 0;
}

// ---------------------------------------------------------------- settings --
// Settings travel as decimal codes, the same in commands and in STAT / LINK lines:
//   bmp   TPFSM  T, P oversampling 1-5 (x1 x2 x4 x8 x16); F filter 0-4 (off x2 x4 x8 x16);
//                S standby 0-7 (0.5 62.5 125 250 500 1000 2000 4000 ms); M 0 continuous, 1 one per reading
//                e.g. 25410
//   link  CCCRK  channel 0-125; rate 0 = 1 Mbps, 1 = 2 Mbps, 2 = 250 kbps; CRC 1 = 8-bit, 2 = 16-bit
//                e.g. 10822
//   retry DDCCA  delay 0-15 (250 us steps, 0 = 250 us); retries 0-15; sends per reading 1-9
//                e.g. 10154
bool validSensor(const SensorCfg &c) {
  // No "skipped" measurement: the pressure can't be worked out without the temperature.
  return c.osrsT >= 1 && c.osrsT <= 5 && c.osrsP >= 1 && c.osrsP <= 5
      && c.filter <= 4 && c.standby <= 7 && c.forced <= 1;
}
bool validLink(const LinkCfg &c) {
  return c.channel <= 125 && c.rate <= RF24_250KBPS && c.crc >= RF24_CRC_8 && c.crc <= RF24_CRC_16;
}
bool validRetry(const RetryCfg &c) {
  return c.delay <= 15 && c.count <= 15 && c.attempts >= 1 && c.attempts <= 9;
}
int32_t sensorCode(const SensorCfg &c) {
  return c.osrsT * 10000L + c.osrsP * 1000L + c.filter * 100 + c.standby * 10 + c.forced;
}
int32_t linkCode(const LinkCfg &c)   { return c.channel * 100L + c.rate * 10 + c.crc; }
int32_t retryCode(const RetryCfg &c) { return c.delay * 1000L + c.count * 10 + c.attempts; }
bool sensorFromCode(int32_t v, SensorCfg &c) {
  SensorCfg n = {uint8_t(v / 10000), uint8_t(v / 1000 % 10), uint8_t(v / 100 % 10), uint8_t(v / 10 % 10),
                 uint8_t(v % 10)};
  if (v < 0 || v > 99999L || !validSensor(n)) return false;
  c = n;
  return true;
}
bool linkFromCode(int32_t v, LinkCfg &c) {
  LinkCfg n = {uint8_t(v / 100), uint8_t(v / 10 % 10), uint8_t(v % 10)};
  if (v < 0 || v > 12599L || !validLink(n)) return false;
  c = n;
  return true;
}
bool retryFromCode(int32_t v, RetryCfg &c) {
  RetryCfg n = {uint8_t(v / 1000), uint8_t(v / 10 % 100), uint8_t(v % 10)};
  if (v < 0 || v > 15159L || !validRetry(n)) return false;
  c = n;
  return true;
}
bool sameLink(const LinkCfg &a, const LinkCfg &b) {
  return a.channel == b.channel && a.rate == b.rate && a.crc == b.crc;
}

void printLink(const LinkCfg &c) {
  static const char *const rates[] = {"1 Mbps", "2 Mbps", "250 kbps"};
  Serial.print(F("ch "));    Serial.print(c.channel);
  Serial.print(' ');         Serial.print(rates[c.rate]);
  Serial.print(F(" CRC "));  Serial.print(c.crc == RF24_CRC_8 ? 8 : 16);
}

// The wait between retries has to cover the base's ACK with its payload: 500 us
// is enough at 1 or 2 Mbps, 1500 us at 250 kbps (nRF24L01+ datasheet 7.4.2).
// A shorter setting is raised to that.
uint8_t ardCode() {
  uint8_t least = radioLink.rate == RF24_250KBPS ? 5 : 1;
  return retryCfg.delay > least ? retryCfg.delay : least;
}

uint8_t savedCheck(const Saved &s) {
  const uint8_t *b = (const uint8_t *)&s;
  uint8_t x = 0x5A;
  for (uint8_t i = 0; i < sizeof s - 1; i++) x = (uint8_t)(x << 1 | x >> 7) ^ b[i];
  return x;
}

void saveSettings() {
  Saved s = {SAVED_MAGIC, intervalMs, healthMs, paLevel, sensorCfg, retryCfg, linkCfg, prevLink, 0};
  s.check = savedCheck(s);
  EEPROM.put(0, s);              // writes only the bytes that changed
}

bool loadSettings() {
  Saved s;
  EEPROM.get(0, s);
  if (s.magic != SAVED_MAGIC || s.check != savedCheck(s)) return false;
  if (s.intervalMs < MIN_INTERVAL_MS || s.intervalMs > MAX_INTERVAL_MS || s.healthMs < MIN_HEALTH_MS
      || s.healthMs > MAX_HEALTH_MS || s.paLevel > RF24_PA_MAX || !validSensor(s.sensor) || !validRetry(s.retry)
      || !validLink(s.link) || !validLink(s.prevLink))
    return false;
  intervalMs = s.intervalMs;
  healthMs   = s.healthMs;
  paLevel    = s.paLevel;
  sensorCfg  = s.sensor;
  retryCfg   = s.retry;
  linkCfg    = s.link;
  prevLink   = s.prevLink;
  return true;
}

// ------------------------------------------------------------------ sensor --
bool sensorInit() {
  static const uint8_t addrs[] = {0x76, 0x77};
  static const uint8_t ids[]   = {0x58, 0x60};   // BMP280, BME280 (same registers)
  lastSensorRetryMs = millis();
  sensorBadReads = 0;
  for (uint8_t a = 0; a < sizeof addrs; a++) {
    for (uint8_t i = 0; i < sizeof ids; i++) {
      wdt_reset();
      if (bmp.begin(addrs[a], ids[i])) {
        sensorAddr = addrs[a];
        Serial.print(F("LOG,sensor found at 0x"));
        Serial.println(sensorAddr, HEX);
        applySensor();
        return sensorOk = true;
      }
    }
  }
  logMsg(F("sensor not found at 0x76 or 0x77"));
  return sensorOk = false;
}

int16_t sensorReg(uint8_t reg) {
  Wire.beginTransmission(sensorAddr);
  Wire.write(reg);
  if (Wire.endTransmission(false) != 0 || Wire.requestFrom((int)sensorAddr, 1) != 1) return -1;
  return Wire.read();
}

uint8_t ctrlMeas(uint8_t mode) { return sensorCfg.osrsT << 5 | sensorCfg.osrsP << 2 | mode; }

// Writes sensorCfg to the BMP280 and reads the registers back. Asleep first:
// the datasheet says writes to config (filter, standby) "may be ignored" in
// normal mode. (This chip took them either way: debug/results/2026-10-16.)
bool applySensor() {
  auto t  = (Adafruit_BMP280::sensor_sampling)sensorCfg.osrsT;
  auto p  = (Adafruit_BMP280::sensor_sampling)sensorCfg.osrsP;
  auto f  = (Adafruit_BMP280::sensor_filter)sensorCfg.filter;
  auto sb = (Adafruit_BMP280::standby_duration)sensorCfg.standby;
  bmp.setSampling(Adafruit_BMP280::MODE_SLEEP, t, p, f, sb);
  if (!sensorCfg.forced) bmp.setSampling(Adafruit_BMP280::MODE_NORMAL, t, p, f, sb);
  int16_t meas = sensorReg(0xF4), conf = sensorReg(0xF5);
  if (meas == ctrlMeas(sensorCfg.forced ? 0 : 3) && conf == (sensorCfg.standby << 5 | sensorCfg.filter << 2))
    return true;
  Serial.print(F("LOG,sensor did not take its settings: ctrl_meas 0x")); Serial.print(meas, HEX);
  Serial.print(F(" config 0x")); Serial.println(conf, HEX);
  return false;
}

// Longest a measurement can take, datasheet 3.8.1: 1.25 + 2.3 T + 2.3 P + 0.575 ms,
// T and P the oversampling counts. 76 ms at x16 / x16.
uint8_t measureMaxMs() {
  uint32_t us = 1250 + 2300UL * (1 << (sensorCfg.osrsT - 1)) + 2300UL * (1 << (sensorCfg.osrsP - 1)) + 575;
  return (us + 999) / 1000;
}

// One-per-reading mode: start a measurement and wait it out. Not the library's
// takeForcedMeasurement(): it can return before the chip has started, and spins
// forever if the sensor stops answering.
bool sensorMeasureNow() {
  Wire.beginTransmission(sensorAddr);
  Wire.write(0xF4);
  Wire.write(ctrlMeas(1));
  if (Wire.endTransmission() != 0) return false;
  delay(measureMaxMs());
  return true;
}

bool sensorRead() {
  if (!sensorOk) return false;
  bool started = !sensorCfg.forced || sensorMeasureNow();
  float t = bmp.readTemperature();
  float p = bmp.readPressure();
  bool good = started && !isnan(t) && !isnan(p) && t > -40 && t < 85 && p > 30000 && p < 110000;
  if (good) {
    tempC = t;
    pressPa = p;
    sensorBadReads = 0;
    return true;
  }
  tempC = pressPa = NAN;
  if (++sensorBadReads >= 3) {
    sensorOk = false;
    sensorFails++;
    lastSensorRetryMs = millis();
    logMsg(F("sensor stopped answering, will re-detect"));
  }
  return false;
}

// ------------------------------------------------------------------- radio --
bool radioConfigOk() {
  return radio.isChipConnected()
      && radio.getChannel() == radioLink.channel
      && radio.getDataRate() == radioLink.rate
      && radio.getCRCLength() == radioLink.crc;
}

#if IS_BASE
void loadAckPayload(bool replace);
#endif

// quiet: a step of the link search, which re-initialises every few seconds;
// not logged or counted as a radio restart.
bool radioInit(bool quiet = false) {
  wdt_reset();
  if (!quiet) radioInits++;
  lastRadioRetryMs = lastRadioCheckMs = millis();
  radioOk = false;
  if (!radio.begin()) {
    logMsg(F("radio not answering on SPI (wiring / 3.3V supply?)"));
    return false;
  }
  radio.setChannel(radioLink.channel);
  radio.setDataRate((rf24_datarate_e)radioLink.rate);
  radio.setPALevel(paLevel, true);
  radio.setCRCLength((rf24_crclength_e)radioLink.crc);
  radio.setAutoAck(true);
  radio.enableDynamicPayloads();
  radio.enableAckPayload();
  radio.setRetries(ardCode(), retryCfg.count);   // default 2.75 ms apart: spreading the 15 tries over
                                                 // ~60 ms rides out short fades and interference bursts
  radio.flush_rx();
  radio.flush_tx();
#if IS_BASE
  radio.openReadingPipe(1, PIPE_ADDR);
  radio.startListening();
  loadAckPayload(true);
#else
  radio.openWritingPipe(PIPE_ADDR);
  radio.stopListening();
#endif
  radioOk = radioConfigOk();
  if (radioOk) {
    if (quiet) return true;
    Serial.print(F("LOG,radio ready "));
    printLink(radioLink);
    Serial.print(F(" pa="));
    Serial.print(paLevel);
    Serial.print(F(" plus_variant="));
    Serial.println(radio.isPVariant());
  } else {
    logMsg(F("radio settings did not stick (supply too weak?)"));
  }
  return radioOk;
}

void radioMaintain(uint32_t now) {
  if (!radioOk) {
    if (elapsed(lastRadioRetryMs, now) >= RADIO_RETRY_MS) radioInit();
    return;
  }
  if (elapsed(lastRadioCheckMs, now) >= RADIO_CHECK_MS) {
    lastRadioCheckMs = now;
    if (!radioConfigOk()) {
      if (!radio.isChipConnected()) {
        radioUnreach++;
        logMsg(F("radio stopped answering on SPI, re-initialising"));
      } else if (radio.getChannel() == 2) {   // 2 = the nRF24's power-on default
        radioPowerLoss++;
        logMsg(F("radio back at power-on defaults (it lost power), re-initialising"));
      } else {
        logMsg(F("radio lost its settings (brown-out?), re-initialising"));
      }
      radioInit();
    }
  }
}

// ------------------------------------------------------------- link change --
void printStatus();

uint8_t linkCandidates(LinkCfg *out) {
  const LinkCfg all[] = {linkCfg, prevLink, DEFAULT_LINK};
  uint8_t n = 0;
  for (uint8_t i = 0; i < 3; i++) {
    bool dup = false;
    for (uint8_t j = 0; j < n; j++) dup |= sameLink(out[j], all[i]);
    if (!dup) out[n++] = all[i];
  }
  return n;
}

// How long a board hears nothing before it starts searching: three of the
// remote's readings plus 5 s, and never under 15 s.
uint32_t searchAfterMs() {
#if IS_BASE
  uint32_t every = heardRemote ? lastRemote.intervalMs : DEFAULT_INTERVAL_MS;
#else
  uint32_t every = intervalMs;
#endif
  return max(SEARCH_MIN_MS, 3 * every + 5000UL);
}

void switchLink(const LinkCfg &to) {
  Serial.print(F("LOG,link change "));
  printLink(linkCfg);
  Serial.print(F(" -> "));
  printLink(to);
  Serial.println();
  prevLink = linkCfg;
  linkCfg = radioLink = to;
  searching = false;
  quietSinceMs = millis();
  saveSettings();
  radioInit();
  printStatus();
}

// A packet (base) or an ACK (remote) came through on radioLink.
void linkHeard() {
  quietSinceMs = millis();
#if !IS_BASE
  if (searching) foundPending = true;
#endif
  bool moved = searching && !sameLink(radioLink, linkCfg);
  if (searching && !moved) logMsg(F("link found again on the same settings"));
  searching = false;
  if (moved) {
    Serial.print(F("LOG,link found on "));
    printLink(radioLink);
    Serial.print(F(", staying there instead of "));
    printLink(linkCfg);
    Serial.println();
    prevLink = linkCfg;
    linkCfg = radioLink;
    saveSettings();
#if !IS_BASE
    // The change that brought us to the old settings didn't work: tell the base, which may still be waiting.
    if (linkCmdSeq && persist.lastCmdSeq == linkCmdSeq) persist.lastCmdResult = RES_FAILED;
#endif
    printStatus();
  }
#if !IS_BASE
  linkCmdSeq = 0;
#endif
}

void searchTask(uint32_t now, uint32_t dwellMs) {
  LinkCfg c[3];
  uint8_t n = linkCandidates(c);
#if IS_BASE
  bool rightAway = searchNow;
  searchNow = false;
#else
  const bool rightAway = false;
#endif
  if (n < 2) return;
  if (!searching) {
    if (!rightAway && elapsed(quietSinceMs, now) < searchAfterMs()) return;
    searching = true;
    searchIdx = 0;                          // the current settings: stay on them for the first step
    searchStepMs = now;
    if (rightAway) {
      Serial.print(F("LOG,the remote can't hear us, searching "));
    } else {
      Serial.print(F("LOG,nothing heard for "));
      Serial.print(elapsed(quietSinceMs, now) / 1000);
      Serial.print(F(" s, searching "));
    }
    Serial.print(n);
    Serial.println(F(" link settings"));
    printStatus();
  }
  if (elapsed(searchStepMs, now) >= dwellMs) {
    searchStepMs = now;
    searchIdx = (searchIdx + 1) % n;
    radioLink = c[searchIdx];
    radioInit(true);
  }
}

// ------------------------------------------------------------- diagnostics --
// Electrical-level checks that tell "unpowered / not connected" apart from
// "connected but not answering". Output is LOG lines, so the UI shows them.

// 'H' pulled up by something outside the Nano, 'F' floating (nothing pulls it),
// 'L' held low even against our pull-up (unpowered chip clamping it, or a short).
// The line is discharged first: a floating pin keeps whatever charge it had, so
// reading it straight after our own pull-up would look like 'H'.
char pinState(uint8_t pin) {
  pinMode(pin, OUTPUT);
  digitalWrite(pin, LOW);
  delayMicroseconds(10);
  pinMode(pin, INPUT);
  delayMicroseconds(20);                   // a 10k pull-up recharges the line in < 1 us
  bool external = digitalRead(pin);
  pinMode(pin, INPUT_PULLUP);
  delayMicroseconds(200);
  bool withPull = digitalRead(pin);
  pinMode(pin, INPUT);
  if (!withPull) return 'L';
  return external ? 'H' : 'F';
}

uint8_t nrfRawRead(uint8_t reg, uint8_t *status) {
  SPI.beginTransaction(SPISettings(1000000, MSBFIRST, SPI_MODE0));
  digitalWrite(PIN_RF_CSN, LOW);
  *status = SPI.transfer(reg & 0x1F);     // R_REGISTER
  uint8_t v = SPI.transfer(0xFF);
  digitalWrite(PIN_RF_CSN, HIGH);
  SPI.endTransaction();
  return v;
}

void printHex2(uint8_t v) {
  if (v < 0x10) Serial.print('0');
  Serial.print(v, HEX);
}

void runDiag() {
  wdt_reset();
  // I2C: with Wire released, BMP280 module pull-ups should hold SDA/SCL high.
  Wire.end();
  char sda = pinState(SDA), scl = pinState(SCL);
  Wire.begin();
  Wire.setClock(100000);
  Serial.print(F("LOG,diag i2c SDA(A4)="));  Serial.print(sda);
  Serial.print(F(" SCL(A5)="));            Serial.print(scl);
  Serial.println(F("  (H=pulled up, F=floating, L=held low)"));

  Serial.print(F("LOG,diag i2c devices:"));
  uint8_t found = 0;
  if (sda == 'L' || scl == 'L') {
    Serial.print(F(" scan skipped, bus held low"));
  } else {
    for (uint8_t a = 0x08; a < 0x78; a++) {
      wdt_reset();
      Wire.beginTransmission(a);
      if (Wire.endTransmission() != 0) continue;
      found++;
      Serial.print(F(" 0x")); printHex2(a);
      if (a == 0x76 || a == 0x77) {
        Wire.beginTransmission(a);
        Wire.write(0xD0);                   // chip-id register
        if (Wire.endTransmission(false) == 0 && Wire.requestFrom(a, (size_t)1) == 1) {
          Serial.print(F("(id 0x")); printHex2(Wire.read()); Serial.print(')');
        }
      }
    }
    if (!found) Serial.print(F(" none"));
  }
  Serial.println();

  // SPI: with CSN high the radio leaves MISO alone; then read raw registers.
  wdt_reset();
  pinMode(PIN_RF_CSN, OUTPUT);
  digitalWrite(PIN_RF_CSN, HIGH);
  SPI.end();                                // SPI unit forces MISO to input; release it to measure
  char miso = pinState(MISO);
  SPI.begin();
  uint8_t st, cfg = nrfRawRead(0x00, &st), ch = nrfRawRead(0x05, &st), rfs = nrfRawRead(0x06, &st);
  Serial.print(F("LOG,diag radio CE=D")); Serial.print(PIN_RF_CE);
  Serial.print(F(" CSN=D"));              Serial.print(PIN_RF_CSN);
  Serial.print(F(" MISO(D12) idle="));    Serial.print(miso);
  Serial.print(F(" STATUS=0x"));   printHex2(st);
  Serial.print(F(" CONFIG=0x"));   printHex2(cfg);
  Serial.print(F(" RF_CH=0x"));    printHex2(ch);
  Serial.print(F(" RF_SETUP=0x")); printHex2(rfs);
  Serial.println();

  Serial.print(F("LOG,diag radio: "));
  if (st == 0x00 && cfg == 0x00 && ch == 0x00 && rfs == 0x00)
    Serial.println(F("all zeros = MISO stuck low: radio has no 3.3V, or MISO shorted to GND"));
  else if (st == 0xFF && cfg == 0xFF && ch == 0xFF && rfs == 0xFF)
    Serial.println(miso == 'H'
      ? F("all 0xFF and MISO pulled high = radio chip not running: typical of 3.3V present but GND not connected")
      : F("all 0xFF = nothing answering: radio unpowered, or MISO/SCK/MOSI/CSN not connected"));
  else if (ch > 0x7F || (st & 0x80))
    Serial.println(F("invalid replies: radio responding only partly (failing module or intermittent contact)"));
  else
    Serial.println(F("chip answers on SPI"));

  Serial.print(F("LOG,diag sensor: "));
  if (sda == 'L' || scl == 'L')
    Serial.println(F("I2C line held low = BMP280 has no power, or SDA/SCL shorted to GND"));
  else if (sda == 'F' || scl == 'F')
    Serial.println(F("I2C lines floating = no pull-ups: BMP280 unpowered or SDA/SCL not connected to A4/A5"));
  else if (!found)
    Serial.println(F("bus pulled up but nothing answers: check the BMP280's GND, then SDO/CSB (CSB must be at 3.3V for I2C)"));
  else
    Serial.println(F("a device answers on I2C"));
  if (sensorOk) {
    Serial.print(F("LOG,diag sensor registers ctrl_meas=0x")); printHex2(sensorReg(0xF4));
    Serial.print(F(" config=0x"));                            printHex2(sensorReg(0xF5));
    Serial.print(F(" for bmp "));                             Serial.println(sensorCode(sensorCfg));
  }

  bool radioSilent = (st == 0xFF && cfg == 0xFF) || (st == 0x00 && cfg == 0x00);
  if (radioSilent && !found)
    Serial.println(F("LOG,diag both modules silent: check the shared 3.3V/GND to the radio and BMP280, and that the Nano sits fully in its headers"));
}

// ------------------------------------------------------------- radio watch --
// For finding a loose radio joint by pressing on it (debug/radio_watch.py).
// Samples STATUS every 10 ms and prints "W,<valid>,<samples>,<last STATUS hex>"
// every 100 ms. Stops by itself 10 s after the last "watch" command.
const uint32_t WATCH_HOLD_MS = 10000;
uint32_t watchUntilMs = 0, lastWatchSampleMs = 0, lastWatchReportMs = 0;
bool     watching = false;
uint8_t  watchValid = 0, watchSamples = 0, watchLast = 0xFF;

uint8_t nrfStatus() {
  SPI.beginTransaction(SPISettings(1000000, MSBFIRST, SPI_MODE0));
  digitalWrite(PIN_RF_CSN, LOW);
  uint8_t st = SPI.transfer(0xFF);          // NOP: the chip answers with STATUS
  digitalWrite(PIN_RF_CSN, HIGH);
  SPI.endTransaction();
  return st;
}

void watchStart() {
  if (!watching) {
    SPI.begin();
    pinMode(PIN_RF_CSN, OUTPUT);
    digitalWrite(PIN_RF_CSN, HIGH);
    watchValid = watchSamples = 0;
    logMsg(F("watch on"));
  }
  watching = true;
  watchUntilMs = millis() + WATCH_HOLD_MS;
}

void watchTask(uint32_t now) {
  if (!watching) return;
  if ((int32_t)(now - watchUntilMs) >= 0) {
    watching = false;
    logMsg(F("watch off"));
    return;
  }
  if (elapsed(lastWatchSampleMs, now) >= 10) {
    lastWatchSampleMs = now;
    watchLast = nrfStatus();
    watchSamples++;
    // A live nRF24 always answers with bit 7 clear; 0x00 is MISO stuck low.
    if (!(watchLast & 0x80) && watchLast != 0x00) watchValid++;
  }
  if (elapsed(lastWatchReportMs, now) >= 100) {
    lastWatchReportMs = now;
    Serial.print(F("W,"));
    Serial.print(watchValid);   Serial.print(',');
    Serial.print(watchSamples); Serial.print(',');
    printHex2(watchLast);
    Serial.println();
    watchValid = watchSamples = 0;
  }
}

// ------------------------------------------------------------------ health --
// One ADC conversion with the given input and reference, leaving the ADC and
// VREF registers as they were. SAMPCAP and the long delays are what the
// datasheet asks for the temperature sensor and do no harm for other inputs.
uint16_t adcSample(uint8_t muxpos, uint8_t refsel, uint8_t vrefAdc0) {
  uint8_t ctrlc = ADC0.CTRLC, ctrld = ADC0.CTRLD, sampctrl = ADC0.SAMPCTRL;
  uint8_t mux = ADC0.MUXPOS, vrefa = VREF.CTRLA;
  VREF.CTRLA    = (vrefa & ~VREF_ADC0REFSEL_gm) | vrefAdc0;
  ADC0.CTRLC    = (ctrlc & ADC_PRESC_gm) | refsel | ADC_SAMPCAP_bm;
  ADC0.CTRLD    = ADC_INITDLY_DLY64_gc;
  ADC0.SAMPCTRL = 31;
  ADC0.MUXPOS   = muxpos;
  ADC0.CTRLA   |= ADC_ENABLE_bm;
  delayMicroseconds(100);
  uint16_t r = 0;
  for (uint8_t i = 0; i < 2; i++) {        // the first result after switching is thrown away
    ADC0.COMMAND = ADC_STCONV_bm;
    while (!(ADC0.INTFLAGS & ADC_RESRDY_bm)) {}
    r = ADC0.RES;
  }
  ADC0.CTRLC = ctrlc; ADC0.CTRLD = ctrld; ADC0.SAMPCTRL = sampctrl;
  ADC0.MUXPOS = mux;  VREF.CTRLA = vrefa;
  return r;
}

// MCU supply in mV: the 1.1 V reference (through AC0's DAC at full scale)
// measured against VDD. Accurate to the reference's few percent.
uint16_t mcuVddMv() {
  uint8_t vrefa = VREF.CTRLA;
  VREF.CTRLA = (vrefa & ~VREF_AC0REFSEL_gm) | VREF_AC0REFSEL_1V1_gc;
  VREF.CTRLB |= VREF_AC0REFEN_bm;
  AC0.DACREF = 0xFF;
  AC0.CTRLA |= AC_ENABLE_bm;
  uint16_t r = adcSample(ADC_MUXPOS_DACREF_gc, ADC_REFSEL_VDDREF_gc, vrefa & VREF_ADC0REFSEL_gm);
  AC0.CTRLA &= ~AC_ENABLE_bm;
  VREF.CTRLB &= ~VREF_AC0REFEN_bm;
  VREF.CTRLA = vrefa;
  return r ? (uint32_t)1100UL * 255UL * 1023UL / 256UL / r : 0;
}

// The 3.3 V rail as seen at the BMP280: SDA idles at its pull-up voltage
// between transfers. On the Nano Every, A4 is wired to both PA2 (TWI SDA)
// and PF2 (analog input 12), so the ADC can read it without stopping Wire.
// The core builds the Every with NO_EXTERNAL_I2C_PULLUP, so Wire.begin() also
// turns on the MCU's internal pull-up to VDD, which lifts SDA ~0.3 V above
// the rail (measured: 3.61 V vs 3.29 V). Switch it off just for the reading.
uint16_t railMv(uint16_t vddMv) {
  uint8_t sdaCtrl = PORTA.PIN2CTRL;
  PORTA.PIN2CTRL = sdaCtrl & ~PORT_PULLUPEN_bm;
  delayMicroseconds(200);
  uint16_t r = adcSample(ADC_MUXPOS_AIN12_gc, ADC_REFSEL_VDDREF_gc, VREF.CTRLA & VREF_ADC0REFSEL_gm);
  PORTA.PIN2CTRL = sdaCtrl;
  return (uint32_t)r * vddMv / 1023;
}

// MCU die temperature in 0.1 degC, with the factory calibration in SIGROW.
int16_t mcuTempDeci() {
  uint16_t r = adcSample(ADC_MUXPOS_TEMPSENSE_gc, ADC_REFSEL_INTREF_gc, VREF_ADC0REFSEL_1V1_gc);
  int32_t kelvin256 = ((int32_t)r - (int8_t)SIGROW.TEMPSENSE1) * (uint8_t)SIGROW.TEMPSENSE0;
  return (kelvin256 * 10 + 128) / 256 - 2732;
}

extern char __heap_start, *__brkval;
uint16_t freeRam() {
  char top;
  return &top - (__brkval ? __brkval : &__heap_start);
}

HealthPacket makeHealth() {
  HealthPacket h;
  h.type           = PKT_HEALTH;
  h.ver            = PROTO_VER;
  h.seq            = ++healthSeq;
  h.uptimeMs       = millis();
  h.vddMv          = mcuVddMv();
  h.railMv         = railMv(h.vddMv);
  h.mcuTempDeci    = mcuTempDeci();
  h.radioPowerLoss = radioPowerLoss;
  h.radioUnreach   = radioUnreach;
  h.sensorFails    = sensorFails;
  h.sends          = hSends;
  h.readings       = hReadings > 255 ? 255 : hReadings;
  h.strongPct      = hStrongOf ? (uint32_t)hStrong * 100 / hStrongOf : 255;   // 255 = nothing heard
  h.maxLoopMs      = maxLoopMs;
  h.freeRam        = freeRam();
  hSends = 0;
  hReadings = hStrong = hStrongOf = 0;
  maxLoopMs = 0;
  return h;
}

void printHealth(char who, const HealthPacket &h) {
  Serial.print(F("HEALTH,"));    Serial.print(who);              Serial.print(',');
  Serial.print(h.seq);           Serial.print(',');
  Serial.print(h.uptimeMs);      Serial.print(',');
  Serial.print(h.vddMv);         Serial.print(',');
  Serial.print(h.railMv);        Serial.print(',');
  Serial.print(h.mcuTempDeci / 10.0f, 1); Serial.print(',');
  Serial.print(h.radioPowerLoss); Serial.print(',');
  Serial.print(h.radioUnreach);  Serial.print(',');
  Serial.print(h.sensorFails);   Serial.print(',');
  Serial.print(h.readings);      Serial.print(',');
  Serial.print(h.sends);         Serial.print(',');
  if (h.strongPct == 255) Serial.print(F("nan")); else Serial.print(h.strongPct);
  Serial.print(',');
  Serial.print(h.maxLoopMs);     Serial.print(',');
  Serial.println(h.freeRam);
}

// ---------------------------------------------------------------- commands --
uint8_t runCommand(uint8_t cmd, int32_t arg) {
  switch (cmd) {
    case CMD_PING:
      return RES_OK;
    case CMD_RESET:
      softReset();
      return RES_FAILED;   // not reached
    case CMD_RADIO_REINIT:
      return radioInit() ? RES_OK : RES_FAILED;
    case CMD_SENSOR_REINIT:
      return sensorInit() ? RES_OK : RES_FAILED;
    case CMD_SET_INTERVAL:
      if (arg < MIN_INTERVAL_MS || arg > MAX_INTERVAL_MS) return RES_BAD_ARG;
      intervalMs = arg;
      saveSettings();
      return RES_OK;
    case CMD_SET_PA:
      if (arg < RF24_PA_MIN || arg > RF24_PA_MAX) return RES_BAD_ARG;
      paLevel = arg;
      radio.setPALevel(paLevel, true);
      saveSettings();
      return RES_OK;
    case CMD_SET_HEALTH:
      if (arg < MIN_HEALTH_MS || arg > MAX_HEALTH_MS) return RES_BAD_ARG;
      healthMs = arg;
      saveSettings();
      return RES_OK;
    case CMD_SET_SENSOR: {
      SensorCfg c;
      if (!sensorFromCode(arg, c)) return RES_BAD_ARG;
      sensorCfg = c;
      saveSettings();
      if (!sensorOk) return RES_OK;          // applied when the sensor is found again
      return applySensor() ? RES_OK : RES_FAILED;
    }
    case CMD_SET_RETRY: {
      RetryCfg c;
      if (!retryFromCode(arg, c)) return RES_BAD_ARG;
      retryCfg = c;
      saveSettings();
      radio.setRetries(ardCode(), retryCfg.count);
      return RES_OK;
    }
    case CMD_SET_LINK: {
      LinkCfg c;
      if (!linkFromCode(arg, c)) return RES_BAD_ARG;
      if (sameLink(c, linkCfg)) return RES_OK;
#if IS_BASE
      // Typed on the base itself. Through `remote link` the base switches when the remote confirms (checkCmdDone).
      switchLink(c);
#else
      // Switch once the base has heard our OK, which goes out in the next reading on the old settings.
      switchPending = true;
      switchNext = c;
      switchFails = 0;
      switchSeq = 0;
#endif
      return RES_OK;
    }
    default:
      return RES_UNKNOWN;
  }
}

// ------------------------------------------------------------------ status --
void printLocalData() {
  Serial.print(F("DATA,L,"));
  Serial.print(sampleSeq);     Serial.print(',');
  Serial.print(millis());      Serial.print(',');
  printFloat(tempC, 2);        Serial.print(',');
  printFloat(pressPa, 1);      Serial.print(',');
  Serial.println(sensorOk && !isnan(tempC) ? 1 : 0);
}

void printStatus() {
  Serial.print(F("STAT,"));
  Serial.print(IS_BASE ? F("BASE") : F("REMOTE")); Serial.print(',');
  Serial.print(radioOk);     Serial.print(',');
  Serial.print(sensorOk);    Serial.print(',');
  Serial.print(millis());    Serial.print(',');
  Serial.print(radioInits);  Serial.print(',');
  Serial.print(paLevel);     Serial.print(',');
  Serial.print(intervalMs);  Serial.print(',');
  printResetCause(resetFlags);
  Serial.print(',');
  Serial.print(healthMs);       // new fields go last so older parsers still read the line
  Serial.print(',');
  Serial.print(sensorCode(sensorCfg)); Serial.print(',');
  Serial.print(retryCode(retryCfg));   Serial.print(',');
  Serial.print(linkCode(linkCfg));     Serial.print(',');
  Serial.print(linkCode(prevLink));    Serial.print(',');
  Serial.println(searching);
}

void printHelp() {
  logMsg(F("commands: help status diag watch ping reset radio sensor interval <ms> pa <0-3> health <ms>"));
  logMsg(F("          bmp <TPFSM> retry <DDCCA> link <CCCRK>  (codes: see README)"));
#if IS_BASE
  logMsg(F("remote:   remote <any of the commands from ping on>, e.g. remote link 9022"));
#endif
}

// -------------------------------------------------------------------- base --
#if IS_BASE
void loadAckPayload(bool replace) {
  AckPacket ack = {PKT_ACK, PROTO_VER, 0, CMD_NONE, 0};
  if (pending.active) {
    ack.cmdSeq = pending.seq;
    ack.cmd = pending.cmd;
    ack.arg = pending.arg;
  }
  if (replace) radio.flush_tx();
  if (pending.active) {
    // The radio drops a queued ACK payload whenever the remote moves on to a new
    // packet, including after it gave up on 15 retries without hearing us. With a
    // single copy the next ACK then goes out empty, so on a weak link most ACKs
    // the remote does hear carry no command. Keep the FIFO full of identical
    // copies instead; the remote ignores repeats of a sequence number it has run.
    for (uint8_t i = 0; i < 3; i++)
      if (!radio.writeAckPayload(1, &ack, sizeof ack)) break;   // false = FIFO full
    return;
  }
  // No command: keep exactly one (empty) ACK payload queued.
  if (!replace && !radio.isFifo(true, true)) return;
  radio.writeAckPayload(1, &ack, sizeof ack);
}

void queueRemoteCommand(uint8_t cmd, int32_t arg) {
  if (!heardRemote) {
    logMsg(F("remote not heard yet, command not sent"));
    return;
  }
  if (pending.active) reportCmd('R', pending.seq, pending.cmd, F("REPLACED"));
  if (++lastIssuedSeq == 0) lastIssuedSeq = 1;
  pending = {true, lastIssuedSeq, cmd, arg, millis()};
  reportCmd('R', pending.seq, cmd, F("QUEUED"));
  loadAckPayload(true);
}

void checkCmdDone(const DataPacket &p) {
  if (pending.active && p.cmdSeqDone == pending.seq) {
    reportCmd('R', pending.seq, pending.cmd, resultName(p.cmdResult));
    pending.active = false;
    loadAckPayload(true);
    // The remote has accepted a link change and switches after this packet: follow it.
    if (pending.cmd == CMD_SET_LINK && p.cmdResult == RES_OK && linkFromCode(pending.arg, switchTo)
        && !sameLink(switchTo, linkCfg))
      switchAfterRx = true;
  }
}

// A remote that says it is searching isn't hearing our ACKs: the link works one way only.
// Its packets don't count as the link found (we'd stay on settings it can't use, while
// it keeps cycling through the others), and if it goes on, we search too.
void remoteCantHearUs(uint32_t now) {
  if (elapsed(deafLastMs, now) > 2000) deafSinceMs = now;    // a new stretch (probes come every 300 ms)
  deafLastMs = now;
  if (!searching && elapsed(deafSinceMs, now) >= REMOTE_DEAF_MS) searchNow = true;
}

void handleRemotePacket(const DataPacket &p, bool hasCfg, bool strongSignal) {
  if (p.type != PKT_DATA || p.ver != PROTO_VER) {
    badPackets++;
    return;
  }
  uint32_t now = millis();
  bool remoteSearching = hasCfg && (p.sensorBits & SENSOR_BITS_SEARCHING);
  if (remoteSearching) remoteCantHearUs(now);
  else linkHeard();
  // The remote re-sends a reading when it misses our ACK, but we usually got the
  // first copy. Count it as a sign of life and a command confirmation only.
  if (heardRemote && p.seq == lastRemote.seq && p.uptimeMs == lastRemote.uptimeMs) {
    repeatCount++;
    hSends++;
    lastRxMs = now;
    checkCmdDone(p);
    return;
  }
  if (heardRemote) {
    if (p.uptimeMs < lastRemote.uptimeMs) {
      Serial.print(F("LOG,remote rebooted, cause "));
      printResetCause(p.resetCause);
      Serial.println();
    } else {
      uint16_t gap = p.seq - lastRemote.seq - 1;
      if (gap < 1000) lostCount += gap;
    }
  } else {
    lastIssuedSeq = p.cmdSeqDone;   // carry on from the remote's numbering
    heardRemote = true;
  }
  lastRemote = p;
  rxCount++;
  hReadings++;
  hStrongOf++;
  if (strongSignal) hStrong++;
  lastRxMs = now;
  if (!linkUp) {
    linkUp = true;
    recoverAttempts = 0;
    logMsg(F("link up"));
  }

  checkCmdDone(p);

  Serial.print(F("DATA,R,"));
  Serial.print(p.seq);          Serial.print(',');
  Serial.print(p.uptimeMs);     Serial.print(',');
  printFloat(p.tempC, 2);       Serial.print(',');
  printFloat(p.pressPa, 1);     Serial.print(',');
  Serial.println(p.sensorOk);

  Serial.print(F("LINK,1,"));
  Serial.print(rxCount);        Serial.print(',');
  Serial.print(lostCount);      Serial.print(',');
  Serial.print(strongSignal);   Serial.print(',');
  Serial.print(p.lastArc);      Serial.print(',');
  Serial.print(p.txFails);      Serial.print(',');
  Serial.print(p.radioInits);   Serial.print(',');
  Serial.print(p.paLevel);      Serial.print(',');
  Serial.print(p.intervalMs);   Serial.print(',');
  printResetCause(p.resetCause); Serial.print(',');
  Serial.print(repeatCount);    Serial.print(',');
  if (hasCfg) {
    uint16_t b = p.sensorBits;
    SensorCfg s = {uint8_t(b & 7), uint8_t(b >> 3 & 7), uint8_t(b >> 6 & 7), uint8_t(b >> 9 & 7), uint8_t(b >> 12 & 1)};
    RetryCfg r = {uint8_t(p.retryBits >> 4), uint8_t(p.retryBits & 15), p.attempts};
    Serial.print(sensorCode(s)); Serial.print(',');
    Serial.print(retryCode(r));   Serial.print(',');
    Serial.println(remoteSearching);
  } else {
    Serial.println(F("-,-,-"));                // older remote firmware
  }
}

uint16_t lastHealthSeq = 0;
uint32_t lastHealthUptime = 0;

void handleRemoteHealth(const HealthPacket &h) {
  if (h.ver != PROTO_VER) {
    badPackets++;
    return;
  }
  lastRxMs = millis();
  linkHeard();
  if (h.seq == lastHealthSeq && h.uptimeMs == lastHealthUptime) {   // a re-send we already have
    repeatCount++;
    hSends++;
    return;
  }
  lastHealthSeq = h.seq;
  lastHealthUptime = h.uptimeMs;
  printHealth('R', h);
}

void baseRadioTask(uint32_t now) {
  if (radioOk) {
    uint8_t pipe;
    while (radio.available(&pipe)) {
      uint8_t len = radio.getDynamicPayloadSize();   // 0 = corrupt, already flushed
      if (len > 0) {
        uint8_t buf[32];
        radio.read(buf, len);
        if ((len == sizeof(DataPacket) || len == DATA_V1_LEN) && buf[0] == PKT_DATA) {
          DataPacket p;
          memset(&p, 0, sizeof p);
          memcpy(&p, buf, len);
          handleRemotePacket(p, len == sizeof(DataPacket), radio.testRPD());
        } else if (len == sizeof(HealthPacket) && buf[0] == PKT_HEALTH) {
          HealthPacket h;
          memcpy(&h, buf, sizeof h);
          handleRemoteHealth(h);
        } else {
          badPackets++;
        }
      }
      loadAckPayload(false);
    }
  }
  if (switchAfterRx) {
    switchAfterRx = false;
    switchLink(switchTo);
  }
  searchTask(now, BASE_DWELL_MS);

  if (pending.active) {
    // Long enough to ride out bad streaks on a weak link (seen: 10+ missed ACKs in a row).
    uint32_t limit = max(30000UL, 20UL * lastRemote.intervalMs + 5000UL);
    if (elapsed(pending.sinceMs, now) > limit) {
      reportCmd('R', pending.seq, pending.cmd, F("TIMEOUT"));
      pending.active = false;
      if (radioOk) loadAckPayload(true);
    }
  }

  uint32_t timeout = heardRemote ? max(4000UL, 3UL * lastRemote.intervalMs + 1000UL) : 4000UL;
  if (linkUp && elapsed(lastRxMs, now) > timeout) {
    linkUp = false;
    lastRecoverMs = now;
    logMsg(F("link lost"));
    Serial.print(F("LINK,0,"));
    Serial.print(rxCount);
    Serial.print(',');
    Serial.println(lostCount);
  }
  if (!linkUp && !searching && elapsed(lastRecoverMs, now) >= BASE_RECOVER_MS) {
    lastRecoverMs = now;
    if (recoverAttempts++ % 6 == 0) logMsg(F("no packets from remote, re-initialising radio"));
    radioInit();
  }
}
#endif

// ------------------------------------------------------------------ remote --
#if !IS_BASE
void handleAck(const AckPacket &a) {
  if (a.type != PKT_ACK || a.ver != PROTO_VER) return;
  if (a.cmd == CMD_NONE || a.cmdSeq == 0 || a.cmdSeq == persist.lastCmdSeq) return;
  // Record first: a reset doesn't return, and the next boot reports it done.
  persist.lastCmdSeq = a.cmdSeq;
  persist.lastCmdResult = RES_OK;
  uint8_t r = runCommand(a.cmd, a.arg);
  persist.lastCmdResult = r;
  if (a.cmd == CMD_SET_LINK && switchPending) switchSeq = a.cmdSeq;
  reportCmd('R', a.cmdSeq, a.cmd, resultName(r));
}

// Sends a packet, re-sending the identical one up to retryCfg.attempts times while no
// ACK comes back: a missed ACK usually means the base got the packet but we
// didn't hear the reply, and every try is another chance to hear it (and any
// command riding on it). The base drops the repeats.
bool radioSend(const void *buf, uint8_t len, uint8_t &attempts, AckPacket &ack, bool &gotAck) {
  bool ok = false;
  attempts = 0;
  gotAck = false;
  while (!ok && attempts < retryCfg.attempts) {
    if (attempts++) {
      wdt_reset();
      delay(random(3, 15));                  // don't retry in lock-step with a fade
    }
    ok = radio.write(buf, len);
  }
  hSends += attempts;
  if (!ok) return false;
  hStrongOf++;
  if (radio.testRPD()) hStrong++;            // the ACK we just heard came in above -64 dBm
  while (radio.available()) {
    uint8_t n = radio.getDynamicPayloadSize();
    if (n == sizeof ack && !gotAck) {
      radio.read(&ack, sizeof ack);
      gotAck = true;
    } else if (n > 0) {
      uint8_t junk[32];
      radio.read(junk, n);
    }
  }
  return true;
}

void remoteSendHealth(const HealthPacket &h) {
  // Not while searching: the health packet can't say so, and the base would take it as the link found.
  if (!radioOk || searching) return;
  uint8_t attempts;
  AckPacket ack;
  bool gotAck;
  if (radioSend(&h, sizeof h, attempts, ack, gotAck)) linkHeard();
  if (gotAck) handleAck(ack);
}

// While searching: re-send the last reading every PROBE_MS on whichever link
// settings the search is on. The base takes it as a repeat if it has it already.
void remoteProbe(uint32_t now) {
  if (!searching || !haveSent || !radioOk || elapsed(lastProbeMs, now) < PROBE_MS) return;
  lastProbeMs = now;
  lastSent.sensorBits |= SENSOR_BITS_SEARCHING;
  uint8_t attempts;
  AckPacket ack;
  bool gotAck;
  if (radioSend(&lastSent, sizeof lastSent, attempts, ack, gotAck)) linkHeard();
  announceFound();
  if (gotAck) handleAck(ack);
}

// The search just ended on an ACK, but the packet that got it still said "searching", and
// the base doesn't settle while we say that. Re-send it without the flag straight away,
// before the base's own search moves it on. The base takes it as a repeat of the reading.
void announceFound() {
  if (!foundPending) return;
  foundPending = false;
  if (!haveSent) return;
  lastSent.sensorBits &= ~SENSOR_BITS_SEARCHING;
  uint8_t attempts;
  AckPacket ack;
  bool gotAck;
  radioSend(&lastSent, sizeof lastSent, attempts, ack, gotAck);   // a command in this ACK comes again
}

void remoteTransmit() {
  if (!radioOk) return;
  hReadings++;
  DataPacket p;
  p.type       = PKT_DATA;
  p.ver        = PROTO_VER;
  p.seq        = sampleSeq;
  p.uptimeMs   = millis();
  p.tempC      = tempC;
  p.pressPa    = pressPa;
  p.sensorOk   = sensorOk && !isnan(tempC);
  p.lastArc    = lastArc;
  p.txFails    = txFails;
  p.radioInits = radioInits;
  p.resetCause = resetFlags;
  p.paLevel    = paLevel;
  p.intervalMs = intervalMs;
  p.cmdSeqDone = persist.lastCmdSeq;
  p.cmdResult  = persist.lastCmdResult;
  p.sensorBits = sensorCfg.osrsT | sensorCfg.osrsP << 3 | sensorCfg.filter << 6 | sensorCfg.standby << 9
               | sensorCfg.forced << 12;
  p.retryBits  = retryCfg.delay << 4 | retryCfg.count;
  p.attempts   = retryCfg.attempts;
  if (searching) p.sensorBits |= SENSOR_BITS_SEARCHING;
  lastSent = p;
  haveSent = true;

  uint8_t attempts;
  AckPacket ack;
  bool gotAck;
  bool ok = radioSend(&p, sizeof p, attempts, ack, gotAck);
  if (ok) {
    txOk++;
    consecutiveFails = 0;
    lastArc = radio.getARC();
    linkHeard();
  } else {
    txFails++;
    consecutiveFails++;
    lastArc = 15;
  }
  // A link change accepted earlier: this packet carried our OK. Once the base has
  // acknowledged it (or after a few tries, in case only its ACKs are getting lost), switch.
  if (switchPending && (ok || ++switchFails >= SWITCH_AFTER_FAILS)) {
    switchPending = false;
    switchLink(switchNext);
    linkCmdSeq = switchSeq;
  }

  Serial.print(F("TX,"));
  Serial.print(ok);          Serial.print(',');
  Serial.print(lastArc);     Serial.print(',');
  Serial.print(txOk);        Serial.print(',');
  Serial.print(txFails);     Serial.print(',');
  Serial.print(radioInits);  Serial.print(',');
  Serial.println(attempts);

  announceFound();
  if (gotAck) handleAck(ack);   // after the radio is idle; may re-init or reset it

  if (consecutiveFails >= REMOTE_FAILS_BEFORE_REINIT && !searching) {
    logMsg(F("base not answering, re-initialising radio"));
    consecutiveFails = 0;
    radioInit();
  }
}
#endif

// ------------------------------------------------------------ serial input --
void notACommand(const char *word) {
  Serial.print(F("LOG,not a command: '"));
  for (const char *c = word; c && *c; c++) {     // show control and garbled bytes as \xNN
    if (*c >= 32 && *c < 127) Serial.print(*c);
    else { Serial.print(F("\\x")); printHex2((uint8_t)*c); }
  }
  Serial.println('\'');
  printHelp();
}

void handleLine(char *line) {
  char *word = strtok(line, " \t");
  if (!word) return;
  char *a1 = strtok(NULL, " \t");
#if IS_BASE
  char *a2 = strtok(NULL, " \t");
#endif

  if (strcasecmp(word, "help") == 0) { printHelp(); return; }
  if (strcasecmp(word, "status") == 0) { printStatus(); return; }
  if (strcasecmp(word, "diag") == 0)   { runDiag(); return; }
  if (strcasecmp(word, "watch") == 0)  { watchStart(); return; }

#if IS_BASE
  if (strcasecmp(word, "remote") == 0) {
    uint8_t cmd = a1 ? cmdFromName(a1) : (uint8_t)CMD_NONE;
    if (cmd == CMD_NONE) { notACommand(a1); return; }
    queueRemoteCommand(cmd, a2 ? atol(a2) : 0);
    return;
  }
#endif

  uint8_t cmd = cmdFromName(word);
  if (cmd == CMD_NONE) { notACommand(word); return; }
  if (cmd == CMD_RESET) reportCmd('L', 0, cmd, F("OK"));   // before we vanish
  reportCmd('L', 0, cmd, resultName(runCommand(cmd, a1 ? atol(a1) : 0)));
}

void serialTask() {
  while (Serial.available()) {
    char c = Serial.read();
    if (c == '\r' || c == '\n') {
      if (lineLen) {
        lineBuf[lineLen] = 0;
        handleLine(lineBuf);
        lineLen = 0;
      }
    } else if (lineLen < sizeof lineBuf - 1) {
      lineBuf[lineLen++] = c;
    }
  }
}

// ------------------------------------------------------------- setup/loop --
void setup() {
  resetFlags = RSTCTRL.RSTFR;
  RSTCTRL.RSTFR = resetFlags;            // flags clear by writing 1s
  wdt_enable(WDT_PERIOD_4KCLK_gc);       // 4.1 s
  bool coldBoot = (resetFlags & (RSTCTRL_PORF_bm | RSTCTRL_BORF_bm | RSTCTRL_UPDIRF_bm))
               || persist.magic != PERSIST_MAGIC;
  if (coldBoot) persist = {PERSIST_MAGIC, 0, RES_NONE};

  Serial.begin(115200);
  Serial.print(F("LOG,boot role="));
  Serial.print(IS_BASE ? F("BASE") : F("REMOTE"));
  Serial.print(F(" cause="));
  printResetCause(resetFlags);
  Serial.println();

  bool saved = loadSettings();
  radioLink = linkCfg;
  logMsg(saved ? F("settings loaded from EEPROM") : F("no saved settings, using defaults"));

  Wire.begin();
  Wire.setClock(100000);
  sensorInit();
  radioInit();
  printStatus();
  if (!radioOk || !sensorOk) runDiag();
}

void loop() {
  wdt_reset();
  uint32_t now = millis();

  serialTask();
  watchTask(now);
  radioMaintain(now);
  if (!sensorOk && elapsed(lastSensorRetryMs, now) >= SENSOR_RETRY_MS) sensorInit();

#if IS_BASE
  baseRadioTask(now);
#endif

  if (elapsed(lastSampleMs, now) >= intervalMs) {
    lastSampleMs = now;
    sampleSeq++;
    sensorRead();
    printLocalData();
#if !IS_BASE
    remoteTransmit();
#endif
  }
#if !IS_BASE
  searchTask(now, REMOTE_DWELL_MS);
  remoteProbe(now);
#endif

  if (elapsed(lastStatusMs, now) >= STATUS_MS) {
    lastStatusMs = now;
    printStatus();
  }

  if (elapsed(lastHealthMs, now) >= healthMs) {
    lastHealthMs = now;
    HealthPacket h = makeHealth();
    printHealth('L', h);
#if !IS_BASE
    remoteSendHealth(h);
#endif
  }

  uint32_t took = millis() - now;
  if (took > maxLoopMs) maxLoopMs = took > 65535 ? 65535 : took;
}
