# Literature-Seeded Thresholds — Verified Citations

Reference document for the Karbon Assure edge node decision tree (Section 8 of the system architecture doc). Each entry below was independently verified against the published paper — not just recalled — with the exact numbers used to seed the initial thresholds.

---

## 1. Critical soil moisture threshold (drought-onset threshold)

**Fu, Z., Ciais, P., Wigneron, J.P., Gentine, P., Feldman, A.F., et al. (2024).** Global critical soil moisture thresholds of plant water stress. *Nature Communications*, 15, Article 4826.
DOI: [10.1038/s41467-024-49244-7](https://doi.org/10.1038/s41467-024-49244-7)

- **Global average θcrit = 0.19 m³/m³**, ranging from **0.12 m³/m³ in arid ecosystems** to **0.26 m³/m³ in humid ecosystems**.
- θcrit is the soil moisture level at which evapotranspiration becomes moisture-limited — i.e., the biophysical drought-onset point.
- Derived using satellite land-surface-temperature/soil-moisture dry-down observations, cross-validated against flux tower data.
- **Use in the tree**: this is the empirical basis for your RSMI < 0.20 "drought onset" branch — note the paper's threshold is in absolute volumetric water content (m³/m³), so your RSMI (a relative/normalized index) should be checked for unit consistency against this before treating 0.20 as a literal match rather than an analogous reference point.

---

## 2. Soil moisture as the dominant driver of soil respiration (semi-arid conditions)

**Meena, A., Hanief, M., Dinakaran, J., Rao, K.S. (2020).** Soil moisture controls the spatio-temporal pattern of soil respiration under different land use systems in a semi-arid ecosystem of Delhi, India. *Ecological Processes*, 9, 15.
DOI: [10.1186/s13717-020-0218-0](https://ecologicalprocesses.springeropen.com/articles/10.1186/s13717-020-0218-0)

- **Soil moisture explained ~90% of the variation in soil respiration** across four land-use types (mixed forest, *Prosopis juliflora* forest, agricultural field, vegetable field) over a full year.
- Measured directly via infrared gas analyzer + soil respiration chamber, paired with gravimetric soil moisture.
- **Use in the tree**: supports linking a falling gas/VOC index to a falling RSMI as the "activity degrading" branch — moisture, not temperature, is the dominant driver in semi-arid systems specifically (relevant since your trial is indoor/pot-based, not field soil, so treat this as directional support rather than a directly transferable coefficient).

---

## 3. Soil moisture and global interannual variability in soil respiration

**Yao, Y., et al. (2021).** A data-driven global soil heterotrophic respiration dataset and the drivers of its inter-annual variability. *Global Biogeochemical Cycles*, 35(8).
DOI: [10.1029/2020GB006918](https://doi.org/10.1029/2020GB006918)

- **Water availability (soil moisture, not temperature) dominates inter-annual variability in soil heterotrophic respiration at the global scale**, particularly in extra-tropical forests and semi-arid regions (temperature dominates only in tropical forests).
- Built from a Random Forest model trained on 455 point observations from the Global Soil Respiration Database (SRDB 4.0).
- **Confirmed**: the correlation between reconstructed and detrended soil-respiration anomalies reaches **r = 0.90 when precipitation is used as the water proxy, and r = 0.94 when soil moisture itself is used** (Figure 5 of the paper). The earlier PRD figure of ~0.9–0.94 is accurate and can now be cited directly.

---

## 4. Wilting point / field capacity by soil texture — CITATION CORRECTED

**Correction**: I re-checked Cornell NMSP Fact Sheet 29 directly — it's a soil-texture *classification* factsheet (the sand/silt/clay triangle), and does **not** contain a field capacity / wilting point table. It was the wrong source for these numbers; using it in your report would be an unverifiable citation. The actual standard peer-reviewed source for texture-based FC/WP estimates is:

**Saxton, K.E., Rawls, W.J. (2006).** Soil Water Characteristic Estimates by Texture and Organic Matter for Hydrologic Solutions. *Soil Science Society of America Journal*, 70(5), 1569–1578.
DOI: [10.2136/sssaj2005.0117](https://doi.org/10.2136/sssaj2005.0117)

- Provides pedotransfer functions estimating field capacity and permanent wilting point directly from sand/clay percentage and organic matter — the standard method used across soil-water literature when direct lab measurement isn't available.
- **The PRD's specific numeric ranges (sandy ≈5–10%/15–25%, loam ≈10–15%/35–45%, clay ≈15–20%/45–55%) do not match published values I could find and should not be cited as-is.** For comparison, field-measured values reported elsewhere in the literature: sandy loam ≈ 23% FC / 11% WP; clay loam ≈ 35% FC / 17% WP (Irrigation Scheduling Based on Wireless Sensors Output, PMC7085626). These are noticeably different from the PRD's ranges, particularly for loam.
- **Recommendation — in progress**: rather than borrowing a generic literature range, the plan is to run a jar/sedimentation test on the actual potting mix (60% red soil, 20% clay, 20% manure) to get a real, measured sand/silt/clay split specific to this trial's soil — not a generic "red soil" assumption, since Indian red soil texture varies too widely by region/depth to cite responsibly as a single number. Once that measurement is in hand, the Saxton-Rawls (2006) equations will be run against it (verified against the published paper, not recalled) to produce the final FC/WP thresholds for the decision tree.

---

## 5. Capacitive soil moisture sensor reading-to-reading variability (validity gate)

**Domínguez-Niño, J.M., Oliver-Manera, J., Girona, J., Casadesús, J. (2020).** Analysis of the Variability in Soil Moisture Measurements by Capacitance Sensors in a Drip-Irrigated Orchard. *Sensors*, 20(18), 5100.
DOI: [10.3390/s20185100](https://doi.org/10.3390/s20185100)

- **Coefficient of variation (CV) of 10–16%** at soil moisture content above 30%; **CV of 6.5–10.3%** at lower moisture content — this is sensor-to-sensor variability under otherwise-constant conditions, i.e. normal noise floor, not drift over time.
- **Use in the validity gate**: sets the floor for `MAX_MOISTURE_JUMP_PCT` (edge node's rate-of-change fault check) — the implausible-jump threshold must sit clearly above the ~16% CV ceiling this paper measured, or the gate will fire on normal sensor noise rather than genuine faults. Chosen as 25 percentage points: comfortably above the measured noise ceiling, not itself a published "fault threshold" (no paper defines one for this use case) — refine against the trial's own dry-down data once collected.

## 6. Light rate-of-change and gas-index trend slope — no citable numeric threshold found

Searched specifically for a published numeric threshold for (a) lux rate-of-change distinguishing "direct sun hit the pot" from ambient drift, and (b) a BSEC gas-index trend slope cutoff for "activity degrading." Neither exists in a form usable here:

- Light: sources found (grow-light/plant-care guides, not peer-reviewed) confirm shade is <100 lux and full sun exceeds 100,000 lux — a large qualitative gap, but no paper specifies a numeric lux-delta-per-reading-interval threshold, and PPFD (µmol/m²/s) figures found aren't unit-convertible to lux without a spectrum assumption this project can't make responsibly.
- Gas index: BSEC's IAQ output is a proprietary, unitless index — no published paper ties a numeric slope of it to biological soil activity change, since the citations in items 2–3 above use lab respiration-chamber measurements (mg CO2/m2/hr), not BSEC's index.
- **Conclusion**: `light_delta_bright` and the gas-index trend slope stay documented engineering placeholders in the code, not literature-backed — flagged explicitly rather than presented as cited, consistent with how item 4 above was handled. Calibrate both against the trial's own data (12–16 Aug).

---

## What this means for your training/thresholds work

- **Confirmed, safe to cite directly**: #1 (θcrit = 0.19, range 0.12–0.26), #2 (~90% variance explained), and #3 (r = 0.90 precipitation / 0.94 soil moisture) — all three numbers check out exactly against the published papers, and training can proceed with these as-is.
- **Pending**: #4 (FC/WP thresholds) is the one open item before finalizing the seed thresholds — a jar/sedimentation test on the actual potting mix is underway to get a real measured sand/silt/clay split (rather than an unverifiable "red soil" generic assumption), which will then be run through the verified Saxton & Rawls (2006) equations. This is the last input needed before the decision tree's initial threshold set is complete.
- Once the jar test result is in, this document should be considered final for the "literature-seeded thresholds" input to training — the controlled fenugreek trial's own data is what refines these further from there (pot soil, not field soil; BME688 gas index, not a lab respiration chamber).
