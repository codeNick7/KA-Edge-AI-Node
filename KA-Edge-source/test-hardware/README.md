# KA-Edge Hardware Diagnostic

Standalone sensor checkout — **does not modify any production files**.

```
test-hardware/
├── sketch/
│   └── test_hardware.ino   ← flash this for testing
└── python/
    └── test_reader.py      ← App Lab console reader
```

## What It Tests

| # | Test | What to expect |
|---|------|----------------|
| 1 | BME688 Pot A `0x76` | `bmeA.begin(0x76): OK` + temp/hum/press/gas values |
| 2 | BME688 Pot B `0x77` | `bmeB.begin(0x77): OK` + temp/hum/press/gas values |
| 3 | Soil sensor A (A0)  | `raw=NNN => X.X%` — dry desk ≈ 0–5% |
| 4 | Soil sensor B (A1)  | `raw=NNN => X.X%` — dry desk ≈ 0–5% |
| 5 | SD card init        | `sd.begin(10): OK` |
| 6 | SD write → `TEST_OUT.CSV` | one header row + one data row written |
| 7 | SD read-back        | all written lines echoed back to console |
| 8 | 10× live 1 Hz stream| `#01 … #10` lines with all sensor values |

After the 10-reading stream, the sketch halts and prints `=== Diagnostic complete ===`.

## How to Use

### Step 1 — Flash diagnostic sketch
Open `test-hardware/sketch/` as an App Lab project and flash `test_hardware.ino`.

### Step 2 — Watch the console
Either:
- Use the built-in App Lab console (shows `diag` channel messages), **or**
- Open `test-hardware/` as an App Lab project with `test_reader.py` as the Python script.

### Step 3 — Check the SD card (optional)
Remove the SD card, mount it on your computer, and open `TEST_OUT.CSV`.
You should see:
```
# KA-Edge Hardware Diagnostic — SD write/read test
#,millis_ms,tempA_C,humA_pct,pressA_hPa,gasA_kOhm,tempB_C,humB_pct,pressB_hPa,gasB_kOhm,soilA_pct,soilB_pct
1,<ms>,<values…>
2,<ms>,<values…>
…
11,<ms>,<values…>   ← 10 streaming rows + 1 init row = 11 data rows total
```

### Step 4 — Restore production firmware
Re-flash `KA-Edge-source/sketch/sketch.ino` to resume normal operation.
The production files are **unchanged**.

## Troubleshooting

| Symptom | Likely cause |
|---------|-------------|
| `bmeA.begin(0x76): FAILED` | I2C address conflict or loose Qwiic cable |
| `bmeB.begin(0x77): FAILED` | Second BME688 not jumpered to 0x77 (SDO pin) |
| `sd.begin(10): FAILED` | SD card not inserted or wrong CS pin |
| soil reading stuck at 0% or 100% | Recalibrate `SOIL_DRY_ADC` / `SOIL_WET_ADC` |
| `performReading() timeout` on first read | Normal — gas heater needs 1–2 warm-up cycles |
