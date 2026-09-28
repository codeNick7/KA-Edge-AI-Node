// v7.6 — 2026-09-22: Clean fix for 1 Hz raw sensor debug output (write_log
// channel) and SD card access reporting (SD:OK/FAIL). Explicitly initializes
// Wire, Wire1, and SPI buses before sensor begin() calls. Removed problematic
// sd.exists() check; replaced with direct file open and size() == 0 header check.
// Streamlined report() to transmit immediately without in-memory queue delays.
// doStreamOneReading safely falls back to 0.0f for offline sensors.
//
// v7.5 — Added SKETCH_VERSION, reported over Bridge as the very first thing
// doInit() does (before any sensor/SD init), so the Python console always
// shows which sketch version is actually running on the board -- a comment
// at the top of this file isn't visible once deployed, and this session hit
// exactly that ambiguity while debugging a doInit() hang after a deploy.
// Also: doInit() no longer wipes sensor_log.csv on every restart (see the
// "Write CSV header only if..." block below).
//
// v7.4 — BH1750 Pot A/Pot B were swapped: the ADDR->GND unit (0x23) was
// coded as Pot A but is physically mounted at Pot B (the withering pot),
// confirmed by physical inspection 2026-08-22. Same bug class as the
// 2026-08-15 soil-pin swap below -- physical placement doesn't follow
// address/wiring-order assumptions, verify it directly. lightA is now
// 0x5C, lightB is now 0x23.
//
// Changes vs v7.3: fixed BH1750 init: both units were on the wrong I2C bus
// (Wire1/Qwiic instead of the standard header Wire bus they're actually
// wired to -- see wiring_schematic_v3, 2026-08-22), which is why both
// failed begin() on real hardware. Also: write_log debug line now prints
// light too, so real values are visible without checking the dashboard.
// And: QUALITY_LOW -> QUALITY_HIGH2 (0.5 lx vs ~4 lx resolution) -- LOW
// was coarse enough to visibly quantize into a binary on/off toggle at
// the low light levels this gallery setup actually sees; still
// comfortably within the 1 Hz loop budget.
//
// Changes vs v7.2: BH1750 light sensors physically wired (both pots) and
// read for real.
// I²C buses: BME688 pair on Wire1 (Qwiic connector, confirmed by hardware
// diagnostic 2026-08-14); BH1750 pair on the standard header Wire bus (a
// separate, physically different bus -- see BH1750 declaration comment).
// Soil pins CORRECTED 2026-08-15: the 2026-08-14 "confirmed by physical
// watering test" mapping (Pot A -> A0, Pot B -> A1) was backwards — traced
// again and found Pot A's probe is actually on A1, Pot B's (withering) is
// actually on A0. Was wrong from the very start of data collection; see
// scripts/migrate_soil_pin_swap.py for the historical-data fix this forced.
//
// Changes vs v7.1:
//   - BH1750 wired: Pot A's ADDR pin to GND (address 0x23, the library's
//     BH1750_TO_GROUND default), Pot B's ADDR pin to VCC (address 0x5C,
//     BH1750_TO_VCC) -- same dual-address-on-one-bus strategy the two
//     BME688s already use (0x76/0x77). LIGHT_PLACEHOLDER is gone; lightA/B
//     fields are real lux readings now.
//
// CSV header:
//   #,millis_ms,tempA_C,humA_pct,pressA_hPa,gasA_KOhm,
//   tempB_C,humB_pct,pressB_hPa,gasB_KOhm,
//   lightA_lux,lightB_lux,moistureA_pct,moistureB_pct
#include <SPI.h>
#include <SdFat.h>
#include <Wire.h>
#include <Adafruit_Sensor.h>
#include <Adafruit_BME680.h>
#include <hp_BH1750.h>
#include <Arduino_RouterBridge.h>

const int chipSelect = 10;
const char *LOG_FILENAME = "sensor_log.csv";
const char *SKETCH_VERSION = "v7.6 (2026-09-22)";

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
// Pot A → A1 | Pot B → A0  (corrected 2026-08-15 — see file header note;
// the 2026-08-14 A0/A1 mapping was backwards)
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

// ── BH1750 — Pot A @0x5C (ADDR->VCC), Pot B @0x23 (ADDR->GND) ──
// Confirmed by physical inspection 2026-08-22: the ADDR-open/GND (0x23)
// unit is the one actually mounted at Pot B (the withering pot), not Pot A
// -- opposite of the BME688 numbering assumption this was first coded
// against. Same bug class as the 2026-08-15 soil-pin swap (see file
// header) -- physical pot placement doesn't follow address numbering,
// verify it directly rather than assuming.
// On the standard header SDA/SCL pins (the default Wire bus), NOT the
// Qwiic connector (Wire1) the two BME688s are on -- these are two
// physically separate I2C buses on the UNO Q. See wiring_schematic_v3
// (2026-08-22): BH1750 SDA/SCL tap into the "header pins (Wire bus)"
// rail, not the Qwiic daisy chain.
// Non-blocking pipelined read (see doStreamOneReading): start() queues a
// measurement, hasValue() checks (cheaply, no bus traffic) whether it's
// ready, getLux() retrieves it. QUALITY_HIGH2 (0.5 lx resolution, ~120-180ms
// conversion -- upgraded 2026-08-22 from QUALITY_LOW's ~4 lx resolution,
// which was coarse enough to visibly quantize into a binary on/off toggle
// at the very low light levels this gallery setup actually sees) still
// comfortably finishes within the 1 Hz loop cadence, so hasValue() is
// essentially always true by the next cycle; lastLuxA/B carry the most
// recent reading forward on the rare cycle it isn't, rather than emitting
// a false zero.
hp_BH1750 lightA;
bool lightAReady = false;
float lastLuxA = 0.0f;

hp_BH1750 lightB;
bool lightBReady = false;
float lastLuxB = 0.0f;

// ── Bridge report helper ──────────────────────────────────────────────────
// Transmits init status messages immediately to the sd_status channel
void report(const char *msg) {
  Bridge.notify("sd_status", msg);
  Bridge.update();
}

// ── SD card free-space reporting ──────────────────────────────────────────
// NOT sent through the reportLines queue — sends directly on the "sd_status"
// Bridge channel on its own timer when enabled.
const unsigned long SD_SPACE_REPORT_INTERVAL_MS = 60000;
unsigned long lastSdSpaceReportMs = 0;

void reportSdSpace() {
  if (!cardReady) return;
  uint32_t freeClusters  = sd.vol()->freeClusterCount();
  uint32_t totalClusters = sd.vol()->clusterCount();
  uint32_t bytesPerCluster = sd.vol()->bytesPerCluster();
  double freeMB  = (double)freeClusters  * bytesPerCluster / (1024.0 * 1024.0);
  double totalMB = (double)totalClusters * bytesPerCluster / (1024.0 * 1024.0);
  double usedMB  = totalMB - freeMB;

  char msg[96];
  snprintf(msg, sizeof(msg), "SD_SPACE used=%.1fMB free=%.1fMB total=%.1fMB",
           usedMB, freeMB, totalMB);
  Bridge.notify("sd_status", msg);
}

// ── CSV row builder ───────────────────────────────────────────────────────
// 14 fields: #,millis,tempA,humA,pressA,gasA,tempB,humB,pressB,gasB,
//            lightA,lightB,moistureA,moistureB
// moistureA / moistureB must be pre-sampled by the caller so the SD write
// and the Bridge message carry exactly the same ADC reading.
void buildReadingLine(char *out, size_t outLen,
                      int index,
                      float moistA, float moistB,
                      float tA, float hA, float pA, float gA,
                      float tB, float hB, float pB, float gB) {
  snprintf(out, outLen,
           "%d,%lu,%.2f,%.2f,%.2f,%.2f,%.2f,%.2f,%.2f,%.2f,%.2f,%.2f,%.2f,%.2f",
           index, millis(),
           tA, hA, pA, gA,
           tB, hB, pB, gB,
           lastLuxA, lastLuxB,
           moistA, moistB);
}

bool appendReadingToSD(int index, float moistA, float moistB,
                       float tA, float hA, float pA, float gA,
                       float tB, float hB, float pB, float gB) {
  dataFile = sd.open(LOG_FILENAME, FILE_WRITE);
  if (!dataFile) return false;

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
  dataFile.print(lastLuxA, 2); dataFile.print(",");
  dataFile.print(lastLuxB, 2); dataFile.print(",");
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

  // Report the running sketch version FIRST, before anything that could
  // fail or hang (SD/sensor init below) -- so the Python console always
  // confirms which build actually made it to the board.
  snprintf(msg, sizeof(msg), "SKETCH_VERSION: %s", SKETCH_VERSION);
  report(msg);

  // Bring up I2C and SPI buses up front
  Wire.begin();
  Wire1.begin();
  SPI.begin();

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

  // BME688 Pot A — 0x76 on Wire1 (Qwiic)
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

  // BME688 Pot B — 0x77 on Wire1 (Qwiic)
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

  // BH1750 Pot A — 0x5C on Wire (ADDR pin -> VCC)
  lightAReady = lightA.begin(BH1750_TO_VCC, &Wire);
  snprintf(msg, sizeof(msg), "lightA (0x5C): %s", lightAReady ? "OK" : "FAILED");
  report(msg);
  if (lightAReady) lightA.start(BH1750_QUALITY_HIGH2, BH1750_MTREG_DEFAULT);

  // BH1750 Pot B — 0x23 on Wire (ADDR pin -> GND)
  lightBReady = lightB.begin(BH1750_TO_GROUND, &Wire);
  snprintf(msg, sizeof(msg), "lightB (0x23): %s", lightBReady ? "OK" : "FAILED");
  report(msg);
  if (lightBReady) lightB.start(BH1750_QUALITY_HIGH2, BH1750_MTREG_DEFAULT);

  // Soil sensors — initial ADC diagnostic readings
  int rawA = analogRead(SOIL_PIN_A);
  float pctA = soilPct(SOIL_PIN_A);
  snprintf(msg, sizeof(msg), "soilA (A1) init: %.1f%% (raw %d)", pctA, rawA);
  report(msg);

  int rawB = analogRead(SOIL_PIN_B);
  float pctB = soilPct(SOIL_PIN_B);
  snprintf(msg, sizeof(msg), "soilB (A0) init: %.1f%% (raw %d)", pctB, rawB);
  report(msg);

  // Write CSV header only if this SD card doesn't already have a log on it.
  // Using sd.open with FILE_WRITE and checking dataFile.size() == 0 avoids
  // calling sd.exists() which can hang on some FAT cards/configurations.
  if (cardReady && (bmeAReady || bmeBReady)) {
    dataFile = sd.open(LOG_FILENAME, FILE_WRITE);
    if (dataFile) {
      if (dataFile.size() == 0) {
        dataFile.println(
          "#,millis_ms,"
          "tempA_C,humA_pct,pressA_hPa,gasA_KOhm,"
          "tempB_C,humB_pct,pressB_hPa,gasB_KOhm,"
          "lightA_lux,lightB_lux,"
          "moistureA_pct,moistureB_pct");
        report("CSV header written — new sensor log started");
      } else {
        report("Existing sensor_log.csv found on SD — appending across restart, not overwriting");
      }
      dataFile.close();
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
  // BH1750 — pick up whatever measurement finished since the last start()
  // call, then immediately queue the next one (see lightA/lightB's
  // declaration comment for why this pipelined pattern instead of a
  // blocking start()+getLux() every cycle).
  if (lightAReady && lightA.hasValue()) {
    lastLuxA = lightA.getLux();
    lightA.start(BH1750_QUALITY_HIGH2, BH1750_MTREG_DEFAULT);
  }
  if (lightBReady && lightB.hasValue()) {
    lastLuxB = lightB.getLux();
    lightB.start(BH1750_QUALITY_HIGH2, BH1750_MTREG_DEFAULT);
  }

  // Perform BME readings — if sensor is offline or times out, safely emit 0.0f
  bool readA = bmeAReady && bmeA.performReading();
  bool readB = bmeBReady && bmeB.performReading();

  float tA = (bmeAReady && readA) ? bmeA.temperature              : 0.0f;
  float hA = (bmeAReady && readA) ? bmeA.humidity                 : 0.0f;
  float pA = (bmeAReady && readA) ? bmeA.pressure / 100.0f        : 0.0f;
  float gA = (bmeAReady && readA) ? bmeA.gas_resistance / 1000.0f : 0.0f;

  float tB = (bmeBReady && readB) ? bmeB.temperature              : 0.0f;
  float hB = (bmeBReady && readB) ? bmeB.humidity                 : 0.0f;
  float pB = (bmeBReady && readB) ? bmeB.pressure / 100.0f        : 0.0f;
  float gB = (bmeBReady && readB) ? bmeB.gas_resistance / 1000.0f : 0.0f;

  sampleIndex++;

  // Sample both soil sensors once — same value goes to SD and Bridge.
  float moistA = soilPct(SOIL_PIN_A);
  float moistB = soilPct(SOIL_PIN_B);

  // SD card write (best-effort; a single missed write isn't fatal)
  bool sdOk = false;
  if (cardReady) {
    sdOk = appendReadingToSD(sampleIndex, moistA, moistB, tA, hA, pA, gA, tB, hB, pB, gB);
  }

  // Bridge notification — parsed by KA-Edge-source/python/main.py
  char readingMsg[192];
  buildReadingLine(readingMsg, sizeof(readingMsg), sampleIndex, moistA, moistB, tA, hA, pA, gA, tB, hB, pB, gB);
  Bridge.notify("reading", readingMsg);

  // Human-readable debug line matching test-hardware diagnostic format
  char dbg[220];
  snprintf(dbg, sizeof(dbg),
           "#%02d | A[%.1fC %.1f%% %.0fhPa %.1fkOhm soil:%.1f%% light:%.1flx] | B[%.1fC %.1f%% %.0fhPa %.1fkOhm soil:%.1f%% light:%.1flx] | SD:%s",
           sampleIndex,
           tA, hA, pA, gA, moistA, lastLuxA,
           tB, hB, pB, gB, moistB, lastLuxB,
           sdOk ? "OK" : (cardReady ? "FAIL" : "NO_CARD"));
  Bridge.notify("write_log", dbg);
  Bridge.notify("diag", dbg);
  Bridge.update();
}

// ── Arduino entry points ──────────────────────────────────────────────────
void setup() {
  Bridge.begin();
  Wire.begin();
  Wire1.begin();
  SPI.begin();
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

  Bridge.update();
  delay(1000);  // 1 Hz logging cadence
}
