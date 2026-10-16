/*
  bmpconfig.ino - does the BMP280 keep the filter / standby settings?

  The datasheet (5.4.6) says writes to the config register (filter, standby)
  "may be ignored" in normal mode. Adafruit's begin() leaves the chip in normal
  mode, and weather_node.ino then called setSampling() straight away, which
  writes config while the chip is measuring. This writes the same settings two
  ways and reads the registers back:
    old   : setSampling(NORMAL, ...) right after begin()   (weather_node before 2026-10-16)
    sleep : setSampling(SLEEP, ...) first, then NORMAL     (weather_node from 2026-10-16)
  Wanted: ctrl_meas 0xF4 = 0x57 (temp x2, pressure x16, normal), config 0xF5 = 0x30
  (standby 62.5 ms, filter x16). Prints 5 rounds of each.
*/
#include <Wire.h>
#include <Adafruit_BMP280.h>

Adafruit_BMP280 bmp;
uint8_t addr = 0;

uint8_t reg(uint8_t r) {
  Wire.beginTransmission(addr);
  Wire.write(r);
  if (Wire.endTransmission(false) != 0 || Wire.requestFrom((int)addr, 1) != 1) return 0xEE;
  return Wire.read();
}

bool start() {
  static const uint8_t addrs[] = {0x76, 0x77}, ids[] = {0x58, 0x60};
  for (uint8_t a = 0; a < 2; a++)
    for (uint8_t i = 0; i < 2; i++)
      if (bmp.begin(addrs[a], ids[i])) { addr = addrs[a]; return true; }
  return false;
}

void show(const char *how) {
  Serial.print(how);
  Serial.print(F(" F4=0x")); Serial.print(reg(0xF4), HEX);
  Serial.print(F(" F5=0x")); Serial.print(reg(0xF5), HEX);
  delay(300);
  Serial.print(F("  after 300 ms F4=0x")); Serial.print(reg(0xF4), HEX);
  Serial.print(F(" F5=0x")); Serial.println(reg(0xF5), HEX);
}

void setup() {
  Serial.begin(115200);
  delay(1500);
  Wire.begin();
  Wire.setClock(100000);
  for (uint8_t round = 1; round <= 5; round++) {
    if (!start()) { Serial.println(F("sensor not found")); return; }
    bmp.setSampling(Adafruit_BMP280::MODE_NORMAL, Adafruit_BMP280::SAMPLING_X2, Adafruit_BMP280::SAMPLING_X16,
                    Adafruit_BMP280::FILTER_X16, Adafruit_BMP280::STANDBY_MS_63);
    Serial.print(round); show(" old  ");

    if (!start()) { Serial.println(F("sensor not found")); return; }
    bmp.setSampling(Adafruit_BMP280::MODE_SLEEP, Adafruit_BMP280::SAMPLING_X2, Adafruit_BMP280::SAMPLING_X16,
                    Adafruit_BMP280::FILTER_X16, Adafruit_BMP280::STANDBY_MS_63);
    bmp.setSampling(Adafruit_BMP280::MODE_NORMAL, Adafruit_BMP280::SAMPLING_X2, Adafruit_BMP280::SAMPLING_X16,
                    Adafruit_BMP280::FILTER_X16, Adafruit_BMP280::STANDBY_MS_63);
    Serial.print(round); show(" sleep");
  }
  Serial.println(F("done"));
}

void loop() {}
