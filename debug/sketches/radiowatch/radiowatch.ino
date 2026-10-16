// Standalone nRF24 / board diagnostics for the Nano Every (no RF24 library).
// Runs the full report at boot and on any serial byte, then polls STATUS.
#include <SPI.h>

const uint8_t CE = 9, CSN = 8, MOSI_P = 11, MISO_P = 12, SCK_P = 13;

void hex2(uint8_t v) { if (v < 0x10) Serial.print('0'); Serial.print(v, HEX); }

char pinState(uint8_t pin) {
  pinMode(pin, OUTPUT); digitalWrite(pin, LOW); delayMicroseconds(10);
  pinMode(pin, INPUT);  delayMicroseconds(20);
  bool ext = digitalRead(pin);
  pinMode(pin, INPUT_PULLUP); delayMicroseconds(200);
  bool pu = digitalRead(pin);
  pinMode(pin, INPUT);
  if (!pu) return 'L';
  return ext ? 'H' : 'F';
}

// MCU supply: measure the 1.1 V reference (through AC0's DAC at full scale) against VDD.
float vdd() {
  VREF.CTRLA = (VREF.CTRLA & ~VREF_AC0REFSEL_gm) | VREF_AC0REFSEL_1V1_gc;
  VREF.CTRLB |= VREF_AC0REFEN_bm;
  AC0.DACREF = 0xFF;
  AC0.CTRLA |= AC_ENABLE_bm;
  ADC0.CTRLC = (ADC0.CTRLC & ~ADC_REFSEL_gm) | ADC_REFSEL_VDDREF_gc;
  ADC0.MUXPOS = ADC_MUXPOS_DACREF_gc;
  delay(2);
  ADC0.COMMAND = ADC_STCONV_bm; while (!(ADC0.INTFLAGS & ADC_RESRDY_bm));
  ADC0.COMMAND = ADC_STCONV_bm; while (!(ADC0.INTFLAGS & ADC_RESRDY_bm));
  uint16_t r = ADC0.RES;
  AC0.CTRLA &= ~AC_ENABLE_bm;
  return r ? 1.1f * (255.0f / 256.0f) * 1023.0f / r : 0;
}

float pinVolts(uint8_t apin) {
  analogReference(INTERNAL4V3);
  delay(2);
  analogRead(apin);
  long s = 0;
  for (uint8_t i = 0; i < 8; i++) s += analogRead(apin);
  return (s / 8.0f) * 4.34f / 1023.0f;
}

// ---- hardware SPI
uint8_t hwXfer2(uint8_t cmd, uint8_t data, uint8_t *status, uint32_t hz) {
  SPI.beginTransaction(SPISettings(hz, MSBFIRST, SPI_MODE0));
  digitalWrite(CSN, LOW); delayMicroseconds(5);
  *status = SPI.transfer(cmd);
  uint8_t v = SPI.transfer(data);
  digitalWrite(CSN, HIGH);
  SPI.endTransaction();
  return v;
}

// ---- bit-banged SPI (bypasses the SPI peripheral and its pin mux)
uint8_t bbByte(uint8_t out) {
  uint8_t in = 0;
  for (int8_t b = 7; b >= 0; b--) {
    digitalWrite(MOSI_P, (out >> b) & 1);
    delayMicroseconds(5);
    digitalWrite(SCK_P, HIGH);
    delayMicroseconds(5);
    in = (in << 1) | digitalRead(MISO_P);
    digitalWrite(SCK_P, LOW);
  }
  return in;
}
uint8_t bbXfer2(uint8_t cmd, uint8_t data, uint8_t *status) {
  digitalWrite(CSN, LOW); delayMicroseconds(10);
  *status = bbByte(cmd);
  uint8_t v = bbByte(data);
  digitalWrite(CSN, HIGH); delayMicroseconds(10);
  return v;
}

void report() {
  Serial.println(F("===== RADIODIAG ====="));
  Serial.print(F("MCU VDD = ")); Serial.print(vdd(), 2); Serial.println(F(" V"));

  // I2C pull-up rail (BMP280 side of the PCB). Wire is not started, so A4/A5 are free.
  pinMode(SDA, INPUT); pinMode(SCL, INPUT); pinMode(A4, INPUT); pinMode(A5, INPUT);
  Serial.print(F("SDA/A4 pull-up rail = ")); Serial.print(pinVolts(A4), 2);
  Serial.print(F(" V   SCL/A5 = "));         Serial.print(pinVolts(A5), 2); Serial.println(F(" V"));

  // Nano's own pin drivers, with the radio deselected.
  SPI.end();
  pinMode(CSN, OUTPUT); digitalWrite(CSN, HIGH);
  pinMode(CE, OUTPUT);  digitalWrite(CE, LOW);
  const uint8_t outs[] = {CE, MOSI_P, SCK_P};
  Serial.print(F("drive test (pin: hi/lo readback):"));
  for (uint8_t i = 0; i < 3; i++) {
    uint8_t p = outs[i];
    pinMode(p, OUTPUT);
    digitalWrite(p, HIGH); delayMicroseconds(20); bool h = digitalRead(p);
    digitalWrite(p, LOW);  delayMicroseconds(20); bool l = digitalRead(p);
    Serial.print(F(" D")); Serial.print(p); Serial.print(':'); Serial.print(h); Serial.print('/'); Serial.print(l);
    Serial.print(h && !l ? F(" ok") : F(" FAIL"));
  }
  digitalWrite(CSN, LOW); delayMicroseconds(20); bool cl = digitalRead(CSN);
  digitalWrite(CSN, HIGH); delayMicroseconds(20); bool ch = digitalRead(CSN);
  Serial.print(F(" D8:")); Serial.print(ch); Serial.print('/'); Serial.print(cl);
  Serial.println(ch && !cl ? F(" ok") : F(" FAIL"));

  // Released line states (H=pulled up outside, F=floating, L=held low).
  Serial.print(F("released lines: CE(D9)="));  Serial.print(pinState(CE));
  Serial.print(F(" MOSI(D11)="));              Serial.print(pinState(MOSI_P));
  Serial.print(F(" SCK(D13)="));               Serial.print(pinState(SCK_P));
  pinMode(SCK_P, OUTPUT); digitalWrite(SCK_P, LOW);
  pinMode(MOSI_P, OUTPUT); digitalWrite(MOSI_P, LOW);
  pinMode(CSN, INPUT);
  Serial.print(F(" CSN(D8)="));                Serial.println(pinState(CSN));
  pinMode(CSN, OUTPUT); digitalWrite(CSN, HIGH);

  // Does the radio take MISO when selected? A live nRF24 drives STATUS bit 7 (=0)
  // onto MISO as soon as CSN goes low, so MISO should go from F to L.
  Serial.print(F("MISO with CSN high = ")); Serial.print(pinState(MISO_P));
  digitalWrite(CSN, LOW); delayMicroseconds(20);
  char misoSel = pinState(MISO_P);
  digitalWrite(CSN, HIGH);
  Serial.print(F("   with CSN low = ")); Serial.print(misoSel);
  Serial.println(misoSel == 'L' ? F("  (radio drives MISO: alive)") : F("  (radio does NOT drive MISO)"));

  // Bit-banged register reads.
  uint8_t st, v;
  Serial.print(F("bit-bang SPI:"));
  const uint8_t regs[] = {0x00, 0x01, 0x03, 0x05, 0x06};
  for (uint8_t i = 0; i < sizeof regs; i++) {
    v = bbXfer2(regs[i], 0xFF, &st);
    Serial.print(F(" r")); hex2(regs[i]); Serial.print('='); hex2(v);
  }
  Serial.print(F(" status=")); hex2(st); Serial.println();
  // Write RF_CH then read back (W_REGISTER = 0x20 | reg).
  uint8_t orig = bbXfer2(0x05, 0xFF, &st);
  bbXfer2(0x20 | 0x05, 0x2A, &st);
  uint8_t back = bbXfer2(0x05, 0xFF, &st);
  bbXfer2(0x20 | 0x05, orig & 0x7F, &st);
  Serial.print(F("bit-bang write RF_CH=0x2A, read back 0x")); hex2(back);
  Serial.println(back == 0x2A ? F("  (write OK)") : F("  (write FAILED)"));

  // Hardware SPI at several speeds.
  SPI.begin();
  const uint32_t speeds[] = {125000UL, 1000000UL, 4000000UL, 8000000UL};
  for (uint8_t i = 0; i < 4; i++) {
    uint8_t s, c = hwXfer2(0x00, 0xFF, &s, speeds[i]);
    uint8_t r5 = hwXfer2(0x05, 0xFF, &s, speeds[i]);
    Serial.print(F("hw SPI ")); Serial.print(speeds[i] / 1000); Serial.print(F(" kHz: STATUS="));
    hex2(s); Serial.print(F(" CONFIG=")); hex2(c); Serial.print(F(" RF_CH=")); hex2(r5); Serial.println();
  }
  SPI.end();
  pinMode(SCK_P, OUTPUT); digitalWrite(SCK_P, LOW);
  pinMode(MOSI_P, OUTPUT);
  Serial.println(F("===== END (now polling STATUS every 250 ms; send any key to rerun) ====="));
}

void setup() {
  Serial.begin(115200);
  delay(1500);
  report();
}

// Watch mode: poll STATUS every 20 ms; print every change, plus a summary each second.
uint32_t lastPoll, lastSec, polls, answers;
int16_t lastSt = -1;
void loop() {
  if (millis() - lastPoll >= 20) {
    lastPoll = millis();
    uint8_t st; bbXfer2(0xFF, 0xFF, &st);       // NOP returns STATUS
    polls++;
    bool ok = (st & 0x80) == 0 && st != 0x00;   // bit 7 is always 0 on a live chip
    if (ok) answers++;
    if (st != lastSt) {
      Serial.print(F("CHANGE t=")); Serial.print(millis()); Serial.print(F(" STATUS=")); hex2(st);
      Serial.println(st == 0x0E ? F("  <-- healthy reply") : (st == 0xFF ? F("  (silent)") : F("  (partial)")));
      lastSt = st;
    }
  }
  if (millis() - lastSec >= 1000) {
    lastSec = millis();
    Serial.print(F("SEC t=")); Serial.print(millis() / 1000); Serial.print(F(" valid replies "));
    Serial.print(answers); Serial.print('/'); Serial.println(polls);
    polls = answers = 0;
  }
}
