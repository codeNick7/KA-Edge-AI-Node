# Karbon Assure Edge Node — Soil Health & Carbon Sequestration Proxy
### Product Requirements Document
**Context:** DigiKey/EFY AIoT Design Challenge 2026 entry, feeding into the Karbon Assure digital MRV platform
**Status:** Abstract submitted (29 Jul 2026); hardware ordered

---

## 1. Overview

This node is a low-cost, edge-AI-capable soil monitoring device that continuously infers a **soil health / microbial activity trend**, flags risk conditions (drought onset, waterlogging, degrading activity, sensor faults), and reports these semantic signals to the Karbon Assure cloud for MRV dashboard visualization and trend storage.

**Explicit non-claim:** this device does not measure soil organic carbon (SOC) directly. No sensor at this price point can. It measures established physical/chemical proxies (moisture, gas/VOC index, temperature, light) that the literature ties to soil respiration and microbial activity, and infers a directional trend from them. All documentation, the contest abstract, and the Karbon Assure integration should describe it this way — as an **activity/health proxy signal**, not a carbon quantification instrument. This distinction is the single most important thing to get right; overclaiming here is the most likely point of technical pushback from judges or MRV auditors.

---

## 2. Hardware

| Component | Role |
|---|---|
| Arduino UNO Q (2GB RAM / 16GB eMMC, ABX00162) | Hybrid board: Qualcomm Dragonwing QRB2210 (quad-core, Debian Linux) runs the edge-AI inference; STM32U585 MCU handles real-time sensor polling |
| Capacitive soil moisture sensor | Primary proxy — volumetric moisture trend |
| BME688 | Temperature, humidity, pressure, and gas/VOC index (via Bosch BSEC), used as a microbial-activity proxy |
| Light sensor | Confound correction — separates "sunny afternoon" effects from genuine soil-state change |

**Why the UNO Q fits well:** its split architecture (real-time MCU + Linux MPU) maps directly onto the two jobs this node needs to do — deterministic sensor acquisition, and actual on-device model inference — without needing a separate compute module.

---

## 3. System Architecture

```
[Capacitive sensor] ---\
[BME688 (I2C)]       ----> STM32U585 (MCU, real-time polling/averaging)
[Light sensor]        --/         |
                                   | Arduino Bridge
                                   v
                    Qualcomm QRB2210 (Linux, Debian)
                    ├─ Feature engineering (relative indices)
                    ├─ Anomaly/fault check (reading validity)
                    ├─ Trend model (small trained classifier)
                    └─ Flag + confidence output
                                   |
                                   v (WiFi)
                    Karbon Assure ingestion API
                    ├─ Stores raw telemetry + flags
                    └─ Dashboard: trend lines + flag history
```

---

## 4. Feature Engineering

Raw sensor values are converted to soil-type-agnostic relative indices before anything touches the model, so the node doesn't need per-field recalibration:

- **Relative Soil Moisture Index (RSMI)** = (moisture − wilting_point) / (field_capacity − wilting_point), clipped to [0,1]
  - Wilting point / field capacity are looked up from a texture table (sandy/loam/clay) set once at deployment, or defaulted to loam if unknown.
- **Gas/VOC Index** — taken directly from BME688's on-chip BSEC output (IAQ / gas index). Do not attempt to train a custom VOC classifier from raw resistance values — BSEC's pretrained output is the right level of abstraction for this board.
- **Temperature** — raw °C, used mainly as a secondary feature and for anomaly bounds checking.
- **Light** — used as a derived feature: `light_delta` (rate of change) rather than absolute lux, since it's a confound-corrector, not a primary signal.

---

## 5. Labeling Strategy & Training Data

There is no existing public dataset pairing capacitive moisture + BME688 gas index + light against verified soil-carbon or microbial-activity ground truth — this combination is too new/niche. The realistic, honest approach for a contest-timeline prototype is a **hybrid of literature-seeded thresholds and a short controlled self-collected trial**:

1. **Seed labels from published thresholds** (see Section 8 for citations):
   - RSMI < ~0.2 (roughly the global critical soil moisture threshold for plant stress) → candidate "drought onset"
   - RSMI > ~0.9 sustained → candidate "waterlogged"
   - Gas index dropping alongside falling RSMI → candidate "activity degrading" (moisture is the dominant driver of soil respiration in semi-arid systems, ~90% of variance in one study)
   - Sudden gas index or moisture spike inconsistent with sensor physics (e.g. moisture jumps 40% in one reading with no rain event) → "sensor fault," not a soil-state flag
2. **Run a short controlled trial**: two comparable pots/plots, one watered normally, one deliberately under-watered, logged for 1–2 weeks. This gives real paired examples of "healthy" vs "degrading" trajectories without needing external data, and is standard practice for this class of DIY sensor calibration.
3. **Train a small classifier** on the combined seed-labeled + self-collected set — a decision tree or logistic regression is appropriate at this data scale; there's no case for a larger model here.
4. **Document the label provenance explicitly** in the project writeup (literature thresholds vs. self-collected trial) — this transparency is a strength, not a weakness, for MRV-style credibility.

---

## 6. Decision Tree (initial model)

A shallow, interpretable decision tree is the right model choice here: small training set, need for explainability in an MRV context, and cheap enough to run on the QRB2210 without any acceleration.

```
Root: is_reading_valid(moisture, gas_index, temp)?
├─ NO → FLAG: "sensor_fault" (confidence = validity_score)
└─ YES
   └─ RSMI < 0.20?
      ├─ YES
      │  └─ light_delta > threshold_bright?
      │     ├─ YES → FLAG: "drought_onset" (bright + dry)
      │     └─ NO  → FLAG: "moisture_low" (dry, not yet critical)
      └─ NO
         └─ RSMI > 0.90?
            ├─ YES → FLAG: "waterlogged_risk"
            └─ NO
               └─ gas_index_trend (7-reading slope) < 0
                  AND RSMI_trend < 0?
                  ├─ YES → FLAG: "soil_health_degrading"
                  └─ NO  → FLAG: "stable"
```

Notes:
- `is_reading_valid` is a simple bounds/rate-of-change check (e.g. moisture can't legitimately jump >X% between consecutive readings without a logged rain event) — this is the "correctness" gate from the architecture discussion, and it should run before any trend logic.
- Thresholds (0.20, 0.90, slope cutoffs) are starting points from Section 8's citations — refine them once the controlled trial data comes in.
- Each leaf should emit a **confidence score**, not just a label — derived from how far the reading sits from the threshold and how many consecutive readings agree.

---

## 7. Signal Output Schema (to Karbon Assure)

```json
{
  "node_id": "string",
  "pot_id": "A | B",
  "timestamp": "ISO8601",
  "flag": "drought_onset | waterlogged_risk | soil_health_degrading | moisture_low | stable | sensor_fault",
  "confidence": 0.0,
  "contributing_trend": "e.g. moisture↓ + light↑",
  "raw": {
    "rsmi": 0.0,
    "gas_index": 0,
    "temp_c": 0.0,
    "light_lux": 0
  }
}
```
Sending both the semantic flag and the raw/derived values gives the Karbon Assure dashboard traceability — a judge or an MRV auditor can see *why* a flag fired, not just that it did.

---

## 8. Citations (for abstract/report use — paraphrase, do not quote verbatim)

- Wilting point and field capacity ranges by soil texture: Cornell Nutrient Management Spear Program, "Know the relationship between soil water content, soil water potential and soil texture" — sandy ≈5–10% (WP) / 15–25% (FC); loam ≈10–15% / 35–45%; clay ≈15–20% / 45–55%.
- Global critical soil moisture threshold for plant water stress: Nature Communications (2024), average θcrit ≈0.19 m³/m³, ranging 0.12 (arid) to 0.26 (humid) across ecosystems.
- Soil moisture as dominant driver of soil respiration: Ecological Processes (2020), semi-arid Delhi study — soil moisture explained ~90% of soil respiration variance.
- Soil-moisture correlation with global soil heterotrophic respiration inter-annual variability: Global Biogeochemical Cycles (2021) — correlation coefficient up to ~0.9–0.94 depending on the driver used.
- Public multi-year field sensor data for pipeline testing (not gas/VOC): USDA Ag Data Commons, "field-scale sensor network dataset" — hourly/daily volumetric water content, temperature, and EC from 42 monitoring locations, Cook Agronomy Farm.
- BME688 gas/IAQ inference: Bosch Sensortec BSEC library documentation (on-chip pretrained gas-index output — use directly rather than training a custom VOC model).

---

## 9. Contest Alignment

- **Creativity/innovation**: the differentiator is not the sensors (proxies are known) but wiring a continuous edge-inference node directly into an insurable MRV pipeline, with an explainable on-device decision layer rather than raw telemetry forwarding.
- **BOM sourced from DigiKey**: UNO Q, capacitive soil sensor, BME688, light sensor — confirm all are DigiKey-listed SKUs in the final BOM table.
- **Documentation quality**: this PRD, plus explicit label-provenance notes (Section 5) and the non-claim statement (Section 1).
- **Real-world applicability**: tie every flag type back to an MRV/insurance use case (e.g. "drought_onset" → early warning for a carbon-credit project at risk of reversal).
- **Video demonstration**: show live sensor readings → on-device flag firing → flag appearing on the Karbon Assure dashboard, in one continuous take if possible.

---

## 10. Build Plan

1. Wire sensors to UNO Q (capacitive → analog/STM32, BME688 → I2C, light sensor → analog or I2C depending on part).
2. Bring up STM32-side polling loop; confirm Bridge data reaches the Linux side.
3. Implement feature engineering (RSMI conversion, gas index passthrough, light_delta) in Python on the Linux side.
4. Implement the validity-check gate first — get "sensor_fault" detection working before any trend logic.
5. Run the controlled two-pot trial (1–2 weeks) while steps 1–4 are being finished, so labeled data is ready when the model layer starts.
6. Train and hand-verify the decision tree against the trial data; adjust thresholds.
7. Implement the signal schema (Section 7) and WiFi push to Karbon Assure's ingestion endpoint.
8. Build the dashboard view (trend line + flag history) on the Karbon Assure side.
9. Record the demo video once flags are firing reliably end-to-end.

---

## 11. Open Items / Risks

- Texture table for RSMI needs a real value at deployment site (or default to loam with a documented assumption).
- Threshold values in Section 6 are literature-seeded starting points, not calibrated — the controlled trial is what should adjust them before submission.
- BME688 placement (headspace above soil vs. buried) affects what the gas index actually reflects — document the chosen placement and the reasoning.
- No lab-verified SOC ground truth exists for this prototype — this is stated explicitly rather than implied away, per Section 1.
