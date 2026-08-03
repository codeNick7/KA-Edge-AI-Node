# Execution Plan — Soil Health Edge Node
### Covers both: DigiKey/EFY AIoT Design Challenge 2026 (abstract submitted; final project due 1–15 Oct 2026) and Arduino Physical AI Challenge India 2026 (Robu.in; full project due 23 Aug 2026)

**Plan date: 30 July 2026.** Board/sensors ordered 29 Jul via DHL WW Express — dates below assume arrival around 4–6 Aug; adjust everything downstream if the shipment lands earlier or later.

---

## Week 1 — 30 Jul to 5 Aug (board in transit)

| Date | Task |
|---|---|
| 30 Jul | Sow both fenugreek pots — same soil mix, same pot type/size, window-side placement. Water both identically. **Record this date as the trial's day-0.** |
| 30 Jul – 4 Aug | Both pots stay on identical watering (germination/establishment phase — do not split conditions yet). Optional: daily manual moisture check (finger test or cheap meter) and a phone photo, just as a rough qualitative backup log. |
| ~4–6 Aug | Board and sensors expected to arrive (adjust on actual tracking). |

## Week 2 — 5 Aug to 11 Aug (bring-up + trial split)

| Date | Task |
|---|---|
| Board arrival day | Unbox, flash/verify UNO Q boots Debian; confirm Bridge communication between STM32 and Linux side works with a trivial test (blink + print). |
| +1 day | Wire capacitive sensor, BME688 (I2C), light sensor. Confirm raw readings on all three from the STM32 side. |
| +1 day | **Split the two pots**: stop watering Pot B (drought condition), keep Pot A on normal watering (control). Record the exact date/time — this is your ground-truth trial start. Begin sensor logging on both pots from this point if you have two sensor sets, or alternate/log sequentially if you only have one set built so far. |
| Remaining days this week | Implement feature engineering in Python on the Linux side: RSMI conversion (using loam thresholds as default), gas-index passthrough from BSEC, light_delta. Implement the validity-check gate first, before any trend logic (per PRD Section 6). |

## Week 3 — 12 Aug to 18 Aug (data collection + model)

| Date | Task |
|---|---|
| 12–16 Aug | Continue daily logging on both pots — this is your main data collection window. Watch for the drought pot's RSMI crossing your seeded thresholds (Section 8 of the PRD); note when visible wilting starts as a real-world cross-check against the sensor data. |
| 16–17 Aug | Label the collected data (literature-seeded thresholds + your own observed dry-down transition) and train the decision tree. Hand-verify a few predictions against what you actually observed in the pots. |
| 17–18 Aug | Implement the signal schema (flag + confidence + contributing_trend) and wire the WiFi push to the Karbon Assure ingestion endpoint. Get one full end-to-end cycle firing correctly: sensor → feature → validity check → flag → cloud → dashboard. |

## Week 4 — 19 Aug to 23 Aug (submission)

| Date | Task |
|---|---|
| 19–20 Aug | Stabilize the end-to-end loop; fix any flaky readings or false flags surfaced during the data collection window. |
| 20–21 Aug | Record the demo video for Robu: 5–10 min, continuous/unedited, **open with a newspaper or Google search result showing the current date** before any project demo starts, publicly shareable link. |
| 21–22 Aug | Assemble the PDF report (team details, abstract, components, system workflow, circuit diagram, source code, images, conclusion, GitHub link, references) — reuse the PRD's architecture, feature engineering, and decision-tree sections directly. |
| 22 Aug | Buffer day — leave this genuinely free for anything that slipped. |
| **23 Aug** | **Submit to Arduino Physical AI Challenge India 2026 (Robu.in).** |

---

## EFY/AIoT Design Challenge track (confirmed dates — runs well past Robu's deadline)

| Date | Stage |
|---|---|
| 15 Jun onwards | Registration opens (done) |
| Closed 29 Jul | Abstract submission (done) |
| By 14 Aug | Selected abstracts announced |
| 15 Aug – 30 Sep | Board purchase & project submission window — top 50 selected, contacted, guided to purchase board via DigiKey |
| 1 – 15 Oct | **Final project submission and judging** — completed project, written article, video proof of concept |
| 15 – 21 Oct | Public voting on ElectronicsForU.com |
| By 21 Oct | Winners announced |

Since EFY's real deadline (1–15 Oct) lands roughly seven weeks after Robu's (23 Aug), there's no need to rush the two in parallel. Practically: everything built for the 23 Aug Robu submission — working prototype, PRD, trial data, decision tree, demo video — becomes the *starting point* for EFY, not a separate build. Between 24 Aug and mid-October there's real room to extend it: a longer/second trial run for more robust training data, refined thresholds, a better article writeup in EFY's specific format, and a possibly stronger demo video, all before the Oct 1–15 window. Watch for the "selected abstracts" notification by 14 Aug — that confirms whether this track is still active before investing further writeup time in it specifically.

---

## Notes
- If the board arrives later than 6 Aug, shift Week 2 onward by the same number of days — Week 4's submission tasks (video + report) are the least compressible and should keep their full ~3 days regardless of what gets squeezed earlier.
- If Pot B hasn't shown a clear moisture/wilting signal by ~16 Aug, that's a signal to speed up the dry-down (skip a watering cycle harder, or note the mismatch) rather than pushing the whole plan later.
