// nRF24 channel scanner for the Nano Every: how often each channel 0-125 has a
// strong signal on it, to find where the interference is and which channels are quiet.
//
// The radio listens on each channel in turn and reads RPD ("received power detector"):
// set when something above about -64 dBm at the chip's input was on the channel while it
// listened. A PA+LNA module's amplifier puts that threshold lower at the antenna. Wi-Fi,
// Bluetooth and microwave ovens send in bursts, so even a busy channel shows a few %.
//
// Switch the other board off while scanning: while it can't reach this one it probes the
// link settings it knows, and those channels would show up as busy.
//
// Prints a report every 10 s (this window and everything since the start), 115200 baud:
//   debug/flash.sh debug/sketches/channelscan --board B800
//   python3 debug/serial_probe.py --board B800 --secs 65
#include <SPI.h>
#include <RF24.h>

const uint8_t  PIN_RF_CE = 9, PIN_RF_CSN = 8;   // as in weather_node.ino
const uint8_t  CHANNELS = 126;
const uint16_t LISTEN_US = 300;                 // per look: ~130 us to settle into RX, then listening
const uint32_t REPORT_MS = 10000;
const uint8_t  WATCH[] = {80, 90, 108};         // channels tried so far

RF24 radio(PIN_RF_CE, PIN_RF_CSN);
uint16_t hits[CHANNELS], total[CHANNELS];       // this window; since the start (in looks with a hit)
uint32_t looks = 0, looksTotal = 0;             // looks per channel
uint32_t windowStart = 0;

// One character per channel: '.' nothing at all, else the share of looks that heard something.
//   1 under 1 %   2 1-2 %   3 2-5 %   4 5-10 %   5 10-20 %   6 20-35 %   7 35-50 %   8 50-75 %   9 75 % and up
char level(uint32_t h, uint32_t n) {
  if (!h || !n) return '.';
  static const uint8_t limit[] = {1, 2, 5, 10, 20, 35, 50, 75};   // percent
  uint32_t perMille = h * 1000UL / n;
  uint8_t d = 1;
  while (d <= 8 && perMille >= limit[d - 1] * 10UL) d++;
  return '0' + d;
}

void printPercent(uint32_t h, uint32_t n) {
  if (!n) { Serial.print('-'); return; }
  Serial.print(h * 100.0f / n, h * 100UL < n * 10UL ? 2 : 1);
  Serial.print('%');
}

void printRow(const char *name, const uint16_t *counts, uint32_t n) {
  Serial.print(name);
  for (uint8_t ch = 0; ch < CHANNELS; ch++) Serial.print(level(counts[ch], n));
  Serial.println();
}

void report() {
  uint32_t secs = (millis() - windowStart + 500) / 1000;
  Serial.println();
  Serial.print(F("LOG,scan: ")); Serial.print(looks); Serial.print(F(" looks per channel in "));
  Serial.print(secs); Serial.print(F(" s, ")); Serial.print(looksTotal);
  Serial.println(F(" since the start. '.' nothing, 1 <1%, 2 <2%, 3 <5%, 4 <10%, 5 <20%, 6 <35%, 7 <50%, 8 <75%, 9 more"));
  Serial.println(F("channel  0         1         2         3         4         5         6         7         8         9         10        11        12"));
  Serial.println(F("         012345678901234567890123456789012345678901234567890123456789012345678901234567890123456789012345678901234567890123456789012345"));
  Serial.println(F("MHz      2400      2410      2420      2430      2440      2450      2460      2470      2480      2490      2500      2510      2520"));
  printRow("window   ", hits, looks);
  printRow("total    ", total, looksTotal);
  Serial.println(F("         Wi-Fi 1 [2401-2423]  6 [2426-2448]  11 [2451-2473]  13 [2461-2483]  licence-free band ends at ch 83"));

  Serial.print(F("LOG,since the start:"));
  for (uint8_t w : WATCH) {
    Serial.print(F("  ch ")); Serial.print(w); Serial.print(' ');
    printPercent(total[w], looksTotal);
  }
  Serial.println();

  // The quietest in-band channels (0-83), by everything heard since the start.
  Serial.print(F("LOG,quietest in band:"));
  bool used[84] = {false};
  for (uint8_t k = 0; k < 5; k++) {
    int16_t best = -1;
    for (uint8_t ch = 0; ch <= 83; ch++)
      if (!used[ch] && (best < 0 || total[ch] < total[best])) best = ch;
    used[best] = true;
    Serial.print(F("  ch ")); Serial.print(best); Serial.print(F(" (")); Serial.print(2400 + best);
    Serial.print(F(" MHz) ")); printPercent(total[best], looksTotal);
  }
  Serial.println();
  // The busiest channels overall.
  Serial.print(F("LOG,busiest:"));
  bool top[CHANNELS] = {false};
  for (uint8_t k = 0; k < 5; k++) {
    int16_t worst = -1;
    for (uint8_t ch = 0; ch < CHANNELS; ch++)
      if (!top[ch] && (worst < 0 || total[ch] > total[worst])) worst = ch;
    top[worst] = true;
    Serial.print(F("  ch ")); Serial.print(worst); Serial.print(F(" (")); Serial.print(2400 + worst);
    Serial.print(F(" MHz) ")); printPercent(total[worst], looksTotal);
  }
  Serial.println();

  memset(hits, 0, sizeof hits);
  looks = 0;
  windowStart = millis();
}

void setup() {
  Serial.begin(115200);
  Serial.println(F("LOG,boot channelscan"));
  if (!radio.begin()) {
    Serial.println(F("LOG,radio not responding: check wiring and power (debug/sketches/radiodiag)"));
    while (true) {}
  }
  radio.setAutoAck(false);
  radio.setDataRate(RF24_250KBPS);   // as the weather link; RPD doesn't depend much on it
  radio.setPALevel(RF24_PA_MIN, true);
  radio.stopListening();
  Serial.print(F("LOG,radio ready, plus_variant="));
  Serial.println(radio.isPVariant());
  Serial.println(F("LOG,switch the other board off while scanning; first report in 10 s"));
  windowStart = millis();
}

void loop() {
  for (uint8_t ch = 0; ch < CHANNELS; ch++) {
    radio.setChannel(ch);
    radio.startListening();
    delayMicroseconds(LISTEN_US);
    bool heard = radio.testRPD();    // latched while in RX; cleared when RX ends
    radio.stopListening();
    if (heard) {
      if (hits[ch] < 65535) hits[ch]++;
      if (total[ch] < 65535) total[ch]++;
    }
  }
  radio.flush_rx();                  // noise that happened to look like a packet
  looks++;
  looksTotal++;
  if (millis() - windowStart >= REPORT_MS) report();
}
