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

// ── SD card free-space reporting ──────────────────────────────────────────
// NOT sent through the reportLines[] queue above — that queue is a bounded
// (MAX_REPORT_LINES=40) one-shot buffer for init diagnostics; once drained
// it never accepts more, so a periodic reading would silently stop being
// delivered after the first ~40 messages. This sends directly on the same
// "sd_status" Bridge channel instead, on its own timer.
//
// freeClusterCount() walks the FAT, which can take a noticeable amount of
// time on some cards/library versions — do NOT call this every 1 Hz loop
// tick, or it risks stalling the sensor sampling cadence. 60s is a starting
// guess; this has not been timed on the real board (no hardware access in
// this session) — if it visibly hitches the 1 Hz log on real hardware,
// raise SD_SPACE_REPORT_INTERVAL_MS further.
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
           lastLuxA, lastLuxB,
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

  // BH1750s are on the standard header Wire bus, not Wire1/Qwiic (see
  // their declaration comment) -- that bus needs its own explicit begin(),
  // unlike Wire1 which the board's core/Bridge framework already brings up
  // for the Qwiic BME688 chain.
  Wire.begin();

  // BH1750 Pot A — 0x5C (ADDR pin -> VCC)
  lightAReady = lightA.begin(BH1750_TO_VCC, &Wire);
  snprintf(msg, sizeof(msg), "lightA (0x5C): %s", lightAReady ? "OK" : "FAILED");
  report(msg);
  if (lightAReady) lightA.start(BH1750_QUALITY_HIGH2, BH1750_MTREG_DEFAULT);

  // BH1750 Pot B — 0x23 (ADDR pin -> GND)
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
  // 2026-09-18: previously did sd.remove(LOG_FILENAME) unconditionally here
  // ("fresh log each run") -- since this runs on every sketch restart, and
  // session_log.jsonl (Python side) shows dozens of restarts across the real
  // collection period, this card never actually held more than "since the
  // last restart" despite being intended as a physically-removable backup of
  // the full raw history (the whole reason it's on removable SD media
  // instead of the board's soldered eMMC -- see
  // docs/plan/board-disk-backup-routine.md for the corresponding Linux-side
  // durability gap this mirrors). appendReadingToSD()'s sd.open(...,
  // FILE_WRITE) already
  // appends rather than overwrites for an existing file (standard SdFat/SD
  // library FILE_WRITE semantics) -- the header-write path just needed the
  // same "don't clobber what's already there" treatment.
  //
  // Known limitation, not fixed here: rows only carry millis()-since-boot,
  // not a wall-clock timestamp, so recovering this file after a board
  // failure tells you relative timing within each boot session but not
  // calendar time -- cross-reference against the Python side's
  // session_log.jsonl (which does have wall-clock session_start times) to
  // anchor a given stretch of rows to an actual date/time.
  if (cardReady && (bmeAReady || bmeBReady)) {
    if (!sd.exists(LOG_FILENAME)) {
      dataFile = sd.open(LOG_FILENAME, FILE_WRITE);
      if (dataFile) {
        dataFile.println(
          "#,millis_ms,"
          "tempA_C,humA_pct,pressA_hPa,gasA_KOhm,"
          "tempB_C,humB_pct,pressB_hPa,gasB_KOhm,"
          "lightA_lux,lightB_lux,"
          "moistureA_pct,moistureB_pct");
        dataFile.close();
        report("CSV header written — new sensor log started");
      } else {
        report("WARN: could not open file for CSV header");
      }
    } else {
      report("Existing sensor_log.csv found on SD — appending across restart, not overwriting");
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

  // SD free-space report — DISABLED 2026-08-15. Confirmed on real hardware:
  // sd.vol()->freeClusterCount() (inside reportSdSpace()) took long enough
  // to collapse the 1 Hz sampling loop to ~once/minute once it started
  // firing, right around the first SD_SPACE_REPORT_INTERVAL_MS=60000
  // threshold — exactly the risk flagged in reportSdSpace()'s comment when
  // this was added, now confirmed rather than just suspected. Sampling
  // integrity matters more than a live SD-space number this close to the
  // 16-17 Aug trial data collection window. Re-enable only after timing
  // freeClusterCount() in isolation and either finding it's fast enough or
  // moving it off the 1 Hz hot path entirely.
  // unsigned long nowMs = millis();
  // if (cardReady && (nowMs - lastSdSpaceReportMs >= SD_SPACE_REPORT_INTERVAL_MS)) {
  //   reportSdSpace();
  //   lastSdSpaceReportMs = nowMs;
  // }

  // Human-readable debug line on the write_log channel
  String dbg = "A[" + String(bmeA.temperature, 1) + "C " +
               String(bmeA.humidity, 1) + "% " +
               String(bmeA.pressure / 100.0, 0) + "hPa " +
               String(bmeA.gas_resistance / 1000.0, 1) + "kΩ soil:" +
               String(moistA, 1) + "% light:" + String(lastLuxA, 1) + "lx] | B[" +
               String(bmeB.temperature, 1) + "C " +
               String(bmeB.humidity, 1) + "% " +
               String(bmeB.pressure / 100.0, 0) + "hPa " +
               String(bmeB.gas_resistance / 1000.0, 1) + "kΩ soil:" +
               String(moistB, 1) + "% light:" + String(lastLuxB, 1) + "lx] SD:" + (cardReady ? "OK" : "FAIL");
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
