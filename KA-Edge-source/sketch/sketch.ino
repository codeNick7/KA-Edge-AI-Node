// v7.0 — dual-pot: BME688@0x76 + A1 (Pot A), BME688@0x77 + A0 (Pot B)
// I²C bus: Wire1 (Qwiic connector) — confirmed by hardware diagnostic 2026-08-14.
// Soil pins: Pot A → A1, Pot B → A0 (confirmed by hardware diagnostic).
// BH1750 light sensor: placeholder fields (0.00) until physically wired.
//   TODO BH1750: when wired, add #include <hp_BH1750.h>, call begin() in doInit(),
//   replace LIGHT_PLACEHOLDER with real lux readings. Supply I2C address per pot.
// CSV header:
//   #,millis_ms,tempA_C,humA_pct,pressA_hPa,gasA_KOhm,
//   tempB_C,humB_pct,pressB_hPa,gasB_KOhm,
//   lightA_lux,lightB_lux,moistureA_pct,moistureB_pct
#include <SPI.h>
#include <SdFat.h>
#include <Wire.h>
#include <Adafruit_Sensor.h>
#include <Adafruit_BME680.h>
#include <Arduino_RouterBridge.h>

const int chipSelect = 10;
const char *LOG_FILENAME = "sensor_log.csv";

SdFat sd;
File dataFile;

// Pot A — BME688 at I2C address 0x76, on Wire1 (Qwiic connector)
Adafruit_BME680 bmeA(&Wire1);
bool bmeAReady = false;

// Pot B — BME688 at I2C address 0x77, on Wire1 (Qwiic connector)
Adafruit_BME680 bmeB(&Wire1);
bool bmeBReady = false;

bool cardReady = false;

// ── Soil moisture — Cytron capacitive sensor (inverted: dry = high ADC) ──
// Pot A → A1 | Pot B → A0  (confirmed by hardware diagnostic 2026-08-14)
const int SOIL_PIN_A = A1;
const int SOIL_PIN_B = A0;

// Calibration: dry air ≈ 1020 ADC (5 V), saturated ≈ 350 ADC (~1.71 V).
// These values are typical for the Cytron CACTUS sensor on a 5 V supply.
// Re-calibrate per unit if readings look wrong (dry LED lights at DRY end).
const int SOIL_DRY_ADC = 1020;
const int SOIL_WET_ADC  = 350;

float soilPct(int pin) {
  int raw = analogRead(pin);
  float pct = (float)(SOIL_DRY_ADC - raw) * 100.0f / (SOIL_DRY_ADC - SOIL_WET_ADC);
  if (pct < 0.0f)   pct = 0.0f;
  if (pct > 100.0f) pct = 100.0f;
  return pct;
}

// ── BH1750 — not yet wired; placeholder until physically connected ──
// Both lightA_lux and lightB_lux are emitted as 0.00.
// When the sensor is wired, add #include <hp_BH1750.h>, initialise here,
// and replace the 0.0f literals in buildReadingLine / appendReadingToSD.
const float LIGHT_PLACEHOLDER = 0.0f;

// ── Report / reading message queues ──────────────────────────────────────
#define MAX_REPORT_LINES 40
char reportLines[MAX_REPORT_LINES][96];
int reportCount  = 0;
int reportCursor = 0;

void report(const char *msg) {
  if (reportCount < MAX_REPORT_LINES) {
    snprintf(reportLines[reportCount], sizeof(reportLines[reportCount]), "%s", msg);
    reportCount++;
  }
}

// ── CSV row builder ───────────────────────────────────────────────────────
// 14 fields: #,millis,tempA,humA,pressA,gasA,tempB,humB,pressB,gasB,
//            lightA,lightB,moistureA,moistureB
// moistureA / moistureB must be pre-sampled by the caller so the SD write
// and the Bridge message carry exactly the same ADC reading.
void buildReadingLine(char *out, size_t outLen,
                      int index,
                      float moistA, float moistB) {
  float tA = bmeAReady ? bmeA.temperature          : 0.0f;
  float hA = bmeAReady ? bmeA.humidity              : 0.0f;
  float pA = bmeAReady ? bmeA.pressure / 100.0f    : 0.0f;
  float gA = bmeAReady ? bmeA.gas_resistance / 1000.0f : 0.0f;

  float tB = bmeBReady ? bmeB.temperature          : 0.0f;
  float hB = bmeBReady ? bmeB.humidity              : 0.0f;
  float pB = bmeBReady ? bmeB.pressure / 100.0f    : 0.0f;
  float gB = bmeBReady ? bmeB.gas_resistance / 1000.0f : 0.0f;

  snprintf(out, outLen,
           "%d,%lu,%.2f,%.2f,%.2f,%.2f,%.2f,%.2f,%.2f,%.2f,%.2f,%.2f,%.2f,%.2f",
           index, millis(),
           tA, hA, pA, gA,
           tB, hB, pB, gB,
           LIGHT_PLACEHOLDER, LIGHT_PLACEHOLDER,
           moistA, moistB);
}

bool appendReadingToSD(int index, float moistA, float moistB) {
  dataFile = sd.open(LOG_FILENAME, FILE_WRITE);
  if (!dataFile) return false;

  float tA = bmeAReady ? bmeA.temperature          : 0.0f;
  float hA = bmeAReady ? bmeA.humidity              : 0.0f;
  float pA = bmeAReady ? bmeA.pressure / 100.0f    : 0.0f;
  float gA = bmeAReady ? bmeA.gas_resistance / 1000.0f : 0.0f;

  float tB = bmeBReady ? bmeB.temperature          : 0.0f;
  float hB = bmeBReady ? bmeB.humidity              : 0.0f;
  float pB = bmeBReady ? bmeB.pressure / 100.0f    : 0.0f;
  float gB = bmeBReady ? bmeB.gas_resistance / 1000.0f : 0.0f;

  dataFile.print(index);        dataFile.print(",");
  dataFile.print(millis());     dataFile.print(",");
  dataFile.print(tA, 2);       dataFile.print(",");
  dataFile.print(hA, 2);       dataFile.print(",");
  dataFile.print(pA, 2);       dataFile.print(",");
  dataFile.print(gA, 2);       dataFile.print(",");
  dataFile.print(tB, 2);       dataFile.print(",");
  dataFile.print(hB, 2);       dataFile.print(",");
  dataFile.print(pB, 2);       dataFile.print(",");
  dataFile.print(gB, 2);       dataFile.print(",");
  dataFile.print(LIGHT_PLACEHOLDER, 2); dataFile.print(",");
  dataFile.print(LIGHT_PLACEHOLDER, 2); dataFile.print(",");
  dataFile.print(moistA, 2);   dataFile.print(",");
  dataFile.println(moistB, 2);
  dataFile.close();
  return true;
}

// ── Application states ────────────────────────────────────────────────────
// STATE_INIT     — one-time hardware setup
// STATE_STREAMING — continuous 1 Hz loop (no readback demo phase)
enum AppState { STATE_INIT, STATE_STREAMING };
AppState state = STATE_INIT;

int sampleIndex = 0;

// ── doInit ────────────────────────────────────────────────────────────────
void doInit() {
  char msg[96];

  // SD card
  for (int retry = 0; retry < 3; retry++) {
    cardReady = sd.begin(chipSelect);
    if (cardReady) break;
    delay(100);
  }
  snprintf(msg, sizeof(msg), "sd.begin(%d): %s", chipSelect, cardReady ? "OK" : "FAILED");
  report(msg);
  if (!cardReady) {
    snprintf(msg, sizeof(msg), "sdErrorCode: 0x%02X sdErrorData: 0x%08lX",
             sd.sdErrorCode(), (unsigned long)sd.sdErrorData());
    report(msg);
  }

  // BME688 Pot A — 0x76
  bmeAReady = bmeA.begin(0x76);
  snprintf(msg, sizeof(msg), "bmeA (0x76): %s", bmeAReady ? "OK" : "FAILED");
  report(msg);
  if (bmeAReady) {
    bmeA.setTemperatureOversampling(BME680_OS_8X);
    bmeA.setHumidityOversampling(BME680_OS_2X);
    bmeA.setPressureOversampling(BME680_OS_4X);
    bmeA.setIIRFilterSize(BME680_FILTER_SIZE_3);
    bmeA.setGasHeater(320, 150);
  }

  // BME688 Pot B — 0x77
  bmeBReady = bmeB.begin(0x77);
  snprintf(msg, sizeof(msg), "bmeB (0x77): %s", bmeBReady ? "OK" : "FAILED");
  report(msg);
  if (bmeBReady) {
    bmeB.setTemperatureOversampling(BME680_OS_8X);
    bmeB.setHumidityOversampling(BME680_OS_2X);
    bmeB.setPressureOversampling(BME680_OS_4X);
    bmeB.setIIRFilterSize(BME680_FILTER_SIZE_3);
    bmeB.setGasHeater(320, 150);
  }

  // BH1750 placeholder — log intent clearly for future wiring
  report("BH1750: not wired yet — light fields emitted as 0.00 (placeholder)");

  // Soil sensors — initial ADC diagnostic readings
  int rawA = analogRead(SOIL_PIN_A);
  float pctA = soilPct(SOIL_PIN_A);
  snprintf(msg, sizeof(msg), "soilA (A0) init: %.1f%% (raw %d)", pctA, rawA);
  report(msg);

  int rawB = analogRead(SOIL_PIN_B);
  float pctB = soilPct(SOIL_PIN_B);
  snprintf(msg, sizeof(msg), "soilB (A1) init: %.1f%% (raw %d)", pctB, rawB);
  report(msg);

  // Write CSV header — fresh log each run
  if (cardReady && (bmeAReady || bmeBReady)) {
    sd.remove(LOG_FILENAME);
    dataFile = sd.open(LOG_FILENAME, FILE_WRITE);
    if (dataFile) {
      dataFile.println(
        "#,millis_ms,"
        "tempA_C,humA_pct,pressA_hPa,gasA_KOhm,"
        "tempB_C,humB_pct,pressB_hPa,gasB_KOhm,"
        "lightA_lux,lightB_lux,"
        "moistureA_pct,moistureB_pct");
      dataFile.close();
      report("CSV header written — starting 1 Hz sensor log");
    } else {
      report("WARN: could not open file for CSV header");
    }
  }

  state = STATE_STREAMING;
}

// ── doStreamOneReading ────────────────────────────────────────────────────
// Called every loop iteration in STATE_STREAMING; the outer loop has a
// 1000 ms delay so this fires ~once per second.
void doStreamOneReading() {
  // Perform BME readings — skip individual sensor if it failed init or
  // if performReading() times out this cycle. Zero-filled fields are clearly
  // labelled in the CSV header; the Python side can detect offline sensors.
  bool readA = bmeAReady && bmeA.performReading();
  bool readB = bmeBReady && bmeB.performReading();

  // Nothing to log if both sensors are offline — don't write empty rows.
  if (!readA && !readB) return;

  sampleIndex++;

  // Sample both soil sensors once — same value goes to SD and Bridge.
  float moistA = soilPct(SOIL_PIN_A);
  float moistB = soilPct(SOIL_PIN_B);

  // SD card write (best-effort; a single missed write isn't fatal)
  if (cardReady) {
    appendReadingToSD(sampleIndex, moistA, moistB);
  }

  // Bridge notification — parsed by KA-Edge-source/python/main.py
  char readingMsg[192];
  buildReadingLine(readingMsg, sizeof(readingMsg), sampleIndex, moistA, moistB);
  Bridge.notify("reading", readingMsg);

  // Human-readable debug line on the write_log channel
  String dbg = "A[" + String(bmeA.temperature, 1) + "C " +
               String(bmeA.humidity, 1) + "% " +
               String(bmeA.pressure / 100.0, 0) + "hPa " +
               String(bmeA.gas_resistance / 1000.0, 1) + "kΩ soil:" +
               String(moistA, 1) + "%] | B[" +
               String(bmeB.temperature, 1) + "C " +
               String(bmeB.humidity, 1) + "% " +
               String(bmeB.pressure / 100.0, 0) + "hPa " +
               String(bmeB.gas_resistance / 1000.0, 1) + "kΩ soil:" +
               String(moistB, 1) + "%] SD:" + (cardReady ? "OK" : "FAIL");
  Bridge.notify("write_log", dbg.c_str());
}

// ── Arduino entry points ──────────────────────────────────────────────────
void setup() {
  Bridge.begin();
}

void loop() {
  Bridge.update();

  switch (state) {
    case STATE_INIT:
      doInit();
      break;
    case STATE_STREAMING:
      doStreamOneReading();
      break;
  }

  // Flush any queued report messages (init diagnostics) to Bridge
  if (reportCursor < reportCount) {
    Bridge.notify("sd_status", reportLines[reportCursor]);
    reportCursor++;
  }

  Bridge.update();
  delay(1000);  // 1 Hz logging cadence
}
