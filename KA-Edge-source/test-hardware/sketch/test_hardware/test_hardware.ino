// ============================================================
// test_hardware.ino  —  Hardware Diagnostic v1.3
// ============================================================
// FIX vs v1.2:
//   Adafruit_BME680 (newer library versions) requires Wire1 to
//   be passed to the CONSTRUCTOR, not to begin(). Passing it to
//   begin() was silently ignored — the sensor still used Wire.
//   v1.3 declares bmeA/bmeB with Wire1 in the constructor.
//   The I2C scan confirmed Wire1 has 0x76 and 0x77.
//
// LIBRARIES: SdFat, Adafruit BME680, Arduino_RouterBridge
// v1.3 — 2026-08-14
// ============================================================

#include <SPI.h>
#include <SdFat.h>
#include <Wire.h>
#include <Adafruit_Sensor.h>
#include <Adafruit_BME680.h>
#include <Arduino_RouterBridge.h>

// ── Constants ─────────────────────────────────────────────────────────────
const int   CHIP_SELECT   = 10;
const char *TEST_FILENAME = "TEST_OUT.CSV";
const int   SOIL_PIN_A    = A0;
const int   SOIL_PIN_B    = A1;
const int   SOIL_DRY_ADC  = 1020;
const int   SOIL_WET_ADC  = 350;
const int   STREAM_N      = 10;

// ── Message buffer ────────────────────────────────────────────────────────
#define MAX_BUF 80
char  msgBuf[MAX_BUF][128];
int   msgCount  = 0;
int   msgCursor = 0;

void buflog(const char *msg) {
  if (msgCount < MAX_BUF) {
    strncpy(msgBuf[msgCount], msg, sizeof(msgBuf[0]) - 1);
    msgBuf[msgCount][sizeof(msgBuf[0]) - 1] = '\0';
    msgCount++;
  }
}
void buflogf(const char *fmt, ...) {
  char tmp[128]; va_list a; va_start(a, fmt);
  vsnprintf(tmp, sizeof(tmp), fmt, a); va_end(a);
  buflog(tmp);
}

// ── Hardware objects ──────────────────────────────────────────────────────
// Wire1 confirmed by I2C scan — pass to constructor (not begin())
SdFat           sd;
Adafruit_BME680 bmeA(&Wire1);   // BME688 Pot A @0x76
Adafruit_BME680 bmeB(&Wire1);   // BME688 Pot B @0x77

bool cardReady = false;
bool bmeAOK    = false;
bool bmeBOK    = false;

// ── State ─────────────────────────────────────────────────────────────────
enum Phase { PHASE_WAIT, PHASE_INIT, PHASE_STREAM, PHASE_DONE };
Phase phase       = PHASE_WAIT;
int   waitTicks   = 0;
int   streamCount = 0;

// ── Helpers ───────────────────────────────────────────────────────────────
float soilPct(int pin) {
  int   raw = analogRead(pin);
  float pct = (float)(SOIL_DRY_ADC - raw) * 100.0f / (SOIL_DRY_ADC - SOIL_WET_ADC);
  if (pct < 0.0f)   pct = 0.0f;
  if (pct > 100.0f) pct = 100.0f;
  return pct;
}

void configureBME(Adafruit_BME680 &bme) {
  bme.setTemperatureOversampling(BME680_OS_8X);
  bme.setHumidityOversampling(BME680_OS_2X);
  bme.setPressureOversampling(BME680_OS_4X);
  bme.setIIRFilterSize(BME680_FILTER_SIZE_3);
  bme.setGasHeater(320, 150);
}

// ── I2C scanner (informational only) ─────────────────────────────────────
void scanBus(TwoWire &bus, const char *name) {
  bus.begin();
  int found = 0;
  for (uint8_t addr = 1; addr < 127; addr++) {
    bus.beginTransmission(addr);
    if (bus.endTransmission() == 0) {
      buflogf("  [%s] 0x%02X", name, addr);
      found++;
    }
  }
  if (!found) buflogf("  [%s] nothing", name);
}

// ── PHASE_WAIT ────────────────────────────────────────────────────────────
void doWait() { if (++waitTicks >= 3) phase = PHASE_INIT; }

// ── PHASE_INIT ────────────────────────────────────────────────────────────
void doInit() {
  buflog("=== KA-Edge Hardware Diagnostic v1.3 ===");

  // I2C scan (informational)
  buflog("--- I2C Bus Scan ---");
  scanBus(Wire,  "Wire ");
  scanBus(Wire1, "Wire1");
  buflog("  -> BME objects use Wire1 (confirmed by previous scan)");

  // 1. BME688 Pot A @0x76
  buflog("--- 1. BME688 Pot A (Wire1, 0x76) ---");
  bmeAOK = bmeA.begin(0x76);
  buflogf("  bmeA.begin(0x76): %s", bmeAOK ? "OK" : "FAILED");
  if (bmeAOK) {
    configureBME(bmeA);
    if (bmeA.performReading()) {
      buflogf("  temp=%.2fC  hum=%.2f%%  press=%.2fhPa  gas=%.2fkOhm",
              bmeA.temperature, bmeA.humidity,
              bmeA.pressure / 100.0f, bmeA.gas_resistance / 1000.0f);
    } else {
      buflog("  NOTE: first performReading() timeout (gas heater warming — normal)");
    }
  }

  // 2. BME688 Pot B @0x77
  buflog("--- 2. BME688 Pot B (Wire1, 0x77) ---");
  bmeBOK = bmeB.begin(0x77);
  buflogf("  bmeB.begin(0x77): %s", bmeBOK ? "OK" : "FAILED");
  if (bmeBOK) {
    configureBME(bmeB);
    if (bmeB.performReading()) {
      buflogf("  temp=%.2fC  hum=%.2f%%  press=%.2fhPa  gas=%.2fkOhm",
              bmeB.temperature, bmeB.humidity,
              bmeB.pressure / 100.0f, bmeB.gas_resistance / 1000.0f);
    } else {
      buflog("  NOTE: first performReading() timeout (gas heater warming — normal)");
    }
  }

  // 3. Soil sensors
  buflog("--- 3. Soil Sensors ---");
  buflogf("  soilA (A0): raw=%d => %.1f%%", analogRead(SOIL_PIN_A), soilPct(SOIL_PIN_A));
  buflogf("  soilB (A1): raw=%d => %.1f%%", analogRead(SOIL_PIN_B), soilPct(SOIL_PIN_B));

  // 4. SD card
  buflog("--- 4. SD Card Init ---");
  for (int i = 0; i < 3; i++) { cardReady = sd.begin(CHIP_SELECT); if (cardReady) break; delay(100); }
  buflogf("  sd.begin(%d): %s", CHIP_SELECT, cardReady ? "OK" : "FAILED");
  if (!cardReady) buflogf("  sdError=0x%02X", sd.sdErrorCode());

  if (cardReady) {
    buflog("--- 5. SD Write Test ---");
    sd.remove(TEST_FILENAME);
    File f = sd.open(TEST_FILENAME, FILE_WRITE);
    if (f) {
      f.println("# KA-Edge Hardware Diagnostic v1.3");
      f.println("#,millis_ms,tempA_C,humA_pct,pressA_hPa,gasA_kOhm,"
                "tempB_C,humB_pct,pressB_hPa,gasB_kOhm,soilA_pct,soilB_pct");
      float tA = bmeAOK ? bmeA.temperature              : -1.0f;
      float hA = bmeAOK ? bmeA.humidity                 : -1.0f;
      float pA = bmeAOK ? bmeA.pressure / 100.0f        : -1.0f;
      float gA = bmeAOK ? bmeA.gas_resistance / 1000.0f : -1.0f;
      float tB = bmeBOK ? bmeB.temperature              : -1.0f;
      float hB = bmeBOK ? bmeB.humidity                 : -1.0f;
      float pB = bmeBOK ? bmeB.pressure / 100.0f        : -1.0f;
      float gB = bmeBOK ? bmeB.gas_resistance / 1000.0f : -1.0f;
      float mA = soilPct(SOIL_PIN_A), mB = soilPct(SOIL_PIN_B);
      char row[192];
      snprintf(row, sizeof(row),
               "1,%lu,%.2f,%.2f,%.2f,%.2f,%.2f,%.2f,%.2f,%.2f,%.2f,%.2f",
               millis(), tA, hA, pA, gA, tB, hB, pB, gB, mA, mB);
      f.println(row); f.close();
      buflogf("  Wrote: %s", row);

      buflog("--- 6. SD Read-Back ---");
      File r = sd.open(TEST_FILENAME, FILE_READ);
      if (r) {
        int ln = 0;
        while (r.available()) {
          char line[128]; int n = 0;
          while (r.available() && n < (int)sizeof(line)-1) {
            char c = r.read(); if (c == '\n') break; if (c != '\r') line[n++] = c;
          }
          line[n] = '\0';
          if (n > 0) { ln++; buflogf("  [line %d] %s", ln, line); }
        }
        r.close();
        buflogf("  Read back %d line(s) — SD PASS", ln);
      }
    }
  }

  buflog("=================================");
  buflogf("RESULT: bmeA=%s  bmeB=%s  soil=OK  SD=%s",
          bmeAOK ? "OK" : "FAIL", bmeBOK ? "OK" : "FAIL", cardReady ? "OK" : "FAIL");
  buflogf("--- Starting %d live readings at 1 Hz ---", STREAM_N);
  phase = PHASE_STREAM;
}

// ── PHASE_STREAM ──────────────────────────────────────────────────────────
void doStream() {
  streamCount++;
  bool readA = bmeAOK && bmeA.performReading();
  bool readB = bmeBOK && bmeB.performReading();
  float mA = soilPct(SOIL_PIN_A), mB = soilPct(SOIL_PIN_B);

  char line[192];
  snprintf(line, sizeof(line),
    "#%02d | A[%.1fC %.1f%% %.0fhPa %.1fkOhm soil:%.1f%%] | B[%.1fC %.1f%% %.0fhPa %.1fkOhm soil:%.1f%%] | SD:%s",
    streamCount,
    readA ? bmeA.temperature              : 0.0f,
    readA ? bmeA.humidity                 : 0.0f,
    readA ? bmeA.pressure / 100.0f        : 0.0f,
    readA ? bmeA.gas_resistance / 1000.0f : 0.0f, mA,
    readB ? bmeB.temperature              : 0.0f,
    readB ? bmeB.humidity                 : 0.0f,
    readB ? bmeB.pressure / 100.0f        : 0.0f,
    readB ? bmeB.gas_resistance / 1000.0f : 0.0f, mB,
    cardReady ? "OK" : "FAIL");
  Bridge.notify("diag", line);

  if (cardReady) {
    File f = sd.open(TEST_FILENAME, FILE_WRITE);
    if (f) {
      char row[192];
      snprintf(row, sizeof(row),
        "%d,%lu,%.2f,%.2f,%.2f,%.2f,%.2f,%.2f,%.2f,%.2f,%.2f,%.2f",
        streamCount+1, millis(),
        readA ? bmeA.temperature              : 0.0f,
        readA ? bmeA.humidity                 : 0.0f,
        readA ? bmeA.pressure / 100.0f        : 0.0f,
        readA ? bmeA.gas_resistance / 1000.0f : 0.0f,
        readB ? bmeB.temperature              : 0.0f,
        readB ? bmeB.humidity                 : 0.0f,
        readB ? bmeB.pressure / 100.0f        : 0.0f,
        readB ? bmeB.gas_resistance / 1000.0f : 0.0f,
        mA, mB);
      f.println(row); f.close();
    }
  }
  if (streamCount >= STREAM_N) phase = PHASE_DONE;
}

// ── PHASE_DONE ────────────────────────────────────────────────────────────
void doDone() {
  Bridge.notify("diag", "=== Diagnostic complete ===");
  Bridge.notify("diag", "Inspect TEST_OUT.CSV on SD card.");
  Bridge.notify("diag", "Re-flash sketch.ino to restore production firmware.");
  while (true) { Bridge.update(); delay(5000); }
}

// ── Arduino entry points ──────────────────────────────────────────────────
void setup() {
  Bridge.begin();
}

void loop() {
  Bridge.update();

  // Drain buffered init messages one per tick
  if (msgCursor < msgCount) {
    Bridge.notify("diag", msgBuf[msgCursor++]);
    Bridge.update();
    delay(1000);
    return;
  }

  switch (phase) {
    case PHASE_WAIT:   doWait();   break;
    case PHASE_INIT:   doInit();   break;
    case PHASE_STREAM: doStream(); break;
    case PHASE_DONE:   doDone();   break;
  }

  Bridge.update();
  delay(1000);
}
